from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from typing import Dict, Set, Any, Optional
import json
import secrets
import time
import copy
import random

from map_data import get_map

app = FastAPI()

# --- Multiplayer rooms (connections) ---
ROOMS: Dict[str, Set[WebSocket]] = {}

# --- Game state per room ---
GAMES: Dict[str, Dict[str, Any]] = {}

# --- Session tokens (MAI nel game state: verrebbero broadcastati a tutti) ---
# room -> playerId -> token segreto per riconnettersi
TOKENS: Dict[str, Dict[str, str]] = {}

# --- Socket attivo per giocatore (per rimpiazzare connessioni stantie) ---
# room -> playerId -> WebSocket
PLAYER_WS: Dict[str, Dict[str, WebSocket]] = {}

PHASES = [
    "LOBBY",
    "SETUP",
    "SCORE",
    "REINFORCE_LAND",
    "REINFORCE_NAVAL",
    "NAVAL_MOVE",
    "NAVAL_COMBAT",
    "SEA_ATTACKS",
    "LAND_ATTACKS",
    "STRATEGIC_MOVE",
    "DRAW_CARD",
]

PLAYER_COLORS = ["RED", "BLUE", "YELLOW", "GREEN", "BLACK"]


def now_ms() -> int:
    return int(time.time() * 1000)


def new_game_state(room: str) -> Dict[str, Any]:
    base_map = get_map()
    game_map = copy.deepcopy(base_map)  # IMPORTANT: per-room copy

    return {
        "room": room,
        "createdAt": now_ms(),
        "players": [],
        "settings": {
            "targetScore": 15,
        },
        "turn": {
            "turnIndex": 0,
            "round": 1,
            "phase": "LOBBY",
            # flags utili già da ora
            "conqueredThisTurn": False,
            "usedStrategicMove": False,
            # tracking navale per-turno
            "navalCombats": {},          # sea_id -> {"target": color, "closed": bool} (§11.4/11.6)
            "seaAttackedProvinces": [],  # §12.6: non si può ri-attaccare via mare la stessa provincia
            "seaConqueredProvinces": [], # §12.7: da qui niente nuovo attacco via mare nel turno
            "trisPlayed": False,         # §4.4.1: una combinazione di carte per turno
        },
        "setup": {
            "claimedByPlayers": 0,
            "neutralPool": [],
            "neutralFixed": [],
        },
        # azioni multi-step / conteggi fase
        "pending": {
            "landReinforceRemaining": 0,
            # dopo una conquista: {"from": ..., "to": ...} finché il giocatore
            # può ancora spostare legioni extra nella provincia conquistata
            "occupation": None,
        },
        "deck": [],
        "discard": [],
        "map": game_map,
        "log": [],
        "winner": None,
    }

async def broadcast_json_async(room: str, payload: Dict[str, Any]) -> None:
    if room not in ROOMS:
        return
    for peer in list(ROOMS[room]):
        await peer.send_json(payload)


def add_log(room: str, text: str) -> None:
    gs = GAMES[room]
    gs["log"].append({"t": now_ms(), "text": text})
    if len(gs["log"]) > 80:
        gs["log"] = gs["log"][-80:]


def assign_colors(gs: Dict[str, Any]) -> None:
    # assegna colori in ordine di join (MVP)
    for i, p in enumerate(gs["players"]):
        p["color"] = PLAYER_COLORS[i % len(PLAYER_COLORS)]


def init_neutrals(gs: Dict[str, Any]) -> None:
    """
    Neutrali come da regolamento:
    - si usano sempre 5 colori; quelli non usati diventano neutrali
    - 3 giocatori: 2 colori neutrali -> per ciascuno 9 gruppi da 3 (tot 18 gruppi)
    - 4 giocatori: 1 colore neutrale -> 3 in ITALIA + 8 gruppi da 3
    """
    used = {p["color"] for p in gs["players"]}
    unused = [c for c in PLAYER_COLORS if c not in used]

    gs["setup"]["neutralPool"] = []
    gs["setup"]["neutralFixed"] = []

    n_players = len(gs["players"])

    if n_players == 3:
        for c in unused:
            for _ in range(9):
                gs["setup"]["neutralPool"].append({"color": c, "size": 3})

    elif n_players == 4:
        if len(unused) == 1:
            c = unused[0]
            gs["setup"]["neutralFixed"].append({"provinceId": "ITALIA", "color": c, "size": 3})
            for _ in range(8):
                gs["setup"]["neutralPool"].append({"color": c, "size": 3})

def get_player_by_id(gs: Dict[str, Any], player_id: str) -> Optional[Dict[str, Any]]:
    for p in gs["players"]:
        if p["id"] == player_id:
            return p
    return None

def normalize_prov_id(raw) -> str:
    """
    Normalizza un provinceId dal payload:
    - None -> ""
    - strip
    - upper
    """
    if raw is None:
        return ""
    return str(raw).strip().upper()

def roll_dice(n: int) -> list[int]:
    """
    Lancia n dadi a 6 facce e ritorna la lista ordinata in modo decrescente.
    """
    return sorted([random.randint(1, 6) for _ in range(n)], reverse=True)


def resolve_risk_roll(att: list[int], deff: list[int]) -> tuple[int, int]:
    """
    Risolve un singolo lancio di dadi stile Risiko/SPQRisiKo.
    Ritorna: (perdite_attaccante, perdite_difensore)
    - i dadi sono già ordinati in modo decrescente
    - a parità vince il difensore
    """
    losses_att = 0
    losses_def = 0

    n = min(len(att), len(deff))
    for i in range(n):
        if att[i] > deff[i]:
            losses_def += 1
        else:
            losses_att += 1

    return losses_att, losses_def


def resolve_naval_roll(att: list[int], deff: list[int]) -> tuple[int, int]:
    """
    §11.5: nel combattimento navale, in caso di parità non perde nessuno.
    Ritorna: (perdite_attaccante, perdite_difensore)
    """
    losses_att = 0
    losses_def = 0
    for i in range(min(len(att), len(deff))):
        if att[i] > deff[i]:
            losses_def += 1
        elif deff[i] > att[i]:
            losses_att += 1
    return losses_att, losses_def


def tri_count(sea: Dict[str, Any], color: str) -> int:
    return int(sea.get("triremes", {}).get(color, 0))


def remove_triremes(sea: Dict[str, Any], color: str, n: int) -> None:
    tri = sea.setdefault("triremes", {})
    tri[color] = tri.get(color, 0) - n
    if tri[color] <= 0:
        tri.pop(color, None)


def has_sea_superiority(sea: Dict[str, Any], color: str) -> bool:
    """§15.2: strettamente più triremi di ogni altro giocatore presente nel mare."""
    tri = sea.get("triremes", {})
    mine = tri.get(color, 0)
    return mine > 0 and all(v < mine for c, v in tri.items() if c != color)


def reset_turn_tracking(turn: Dict[str, Any]) -> None:
    turn["conqueredThisTurn"] = False
    turn["usedStrategicMove"] = False
    turn["navalCombats"] = {}
    turn["seaAttackedProvinces"] = []
    turn["seaConqueredProvinces"] = []
    turn["trisPlayed"] = False


MAX_POWER_CENTERS = 12   # §1.3
MAX_PLAYERS = 5           # §2.6: si gioca sempre con 5 eserciti
NO_ELIMINATION_ROUNDS = 4  # §18.4


def min_garrison(gs: Dict[str, Any], prov_id: str, color: str) -> int:
    """
    §14.2: dopo uno spostamento volontario devono restare almeno 2 legioni
    in una provincia confinante via terra con una provincia nemica
    (contiamo anche i neutrali: non attaccano, ma il testo non li esclude).
    Altrimenti basta la guarnigione minima di 1 (§14.1).
    """
    provs = gs["map"]["provinces"]
    for nb in provs[prov_id].get("adj_land", []):
        owner = provs[nb].get("owner")
        if owner is not None and owner != color:
            return 2
    return 1


def is_last_province_protected(gs: Dict[str, Any], defender_color: Optional[str]) -> bool:
    """§18.4: nessuno può essere eliminato prima della fine del 4° round."""
    if not defender_color or str(defender_color).startswith("NEUTRAL_"):
        return False
    if gs["turn"]["round"] > NO_ELIMINATION_ROUNDS:
        return False
    return count_owned_provinces(gs, defender_color) == 1


def total_power_centers(gs: Dict[str, Any]) -> int:
    return sum(1 for p in gs["map"]["provinces"].values() if p.get("power_center"))


def can_place_power_center(gs: Dict[str, Any], color: str, prov_id: str) -> tuple[bool, str]:
    """§16.4: provincia propria, senza centro, senza centri nelle confinanti via terra."""
    provs = gs["map"]["provinces"]
    if prov_id not in provs:
        return False, f"Invalid provinceId: {prov_id}"
    prov = provs[prov_id]
    if prov.get("owner") != color:
        return False, f"You do not own {prov_id}"
    if prov.get("power_center"):
        return False, f"{prov_id} already has a power center"
    for nb in prov.get("adj_land", []):
        if provs[nb].get("power_center"):
            return False, f"Adjacent province {nb} has a power center (§16.4)"
    if total_power_centers(gs) >= MAX_POWER_CENTERS:
        return False, "All 12 power centers are already in play"
    return True, ""


def count_owned_provinces(gs: Dict[str, Any], owner_color: str) -> int:
    n = 0
    for prov in gs["map"]["provinces"].values():
        if prov.get("owner") == owner_color:
            n += 1
    return n

def calc_land_reinforcements(gs: Dict[str, Any], owner_color: str) -> int:
    """
    Regole base regolamento §6:
    - < 3 province -> 1
    - 3..11 -> 3
    - > 11 -> floor(province/3)
    (Centri di Potere: verranno aggiunti più avanti)
    """
    nprov = count_owned_provinces(gs, owner_color)
    if nprov < 3:
        return 1
    if 3 <= nprov <= 11:
        return 3
    return nprov // 3


def build_deck() -> list[dict]:
    """
    Mazzo di 55 carte (§1.6). La distribuzione esatta dei simboli non è
    indicata nel regolamento: usiamo 14/14/14/13 come approssimazione.
    """
    symbols = (
        ["LEGIONARIO"] * 14
        + ["TRIREME"] * 14
        + ["VESSILLO"] * 14
        + ["ARENA"] * 13
    )
    random.shuffle(symbols)
    return [{"symbol": s} for s in symbols]


def draw_card(gs: Dict[str, Any]) -> Optional[dict]:
    if not gs["deck"] and gs["discard"]:
        random.shuffle(gs["discard"])
        gs["deck"] = gs["discard"]
        gs["discard"] = []
    if not gs["deck"]:
        return None
    return gs["deck"].pop()


def largest_empire_size(gs: Dict[str, Any], color: str) -> int:
    """
    §5.6.1: dimensione del più grande insieme di province del giocatore
    collegate fra loro via terra (le isole non contano come collegate).
    """
    provs = gs["map"]["provinces"]
    owned = {pid for pid, p in provs.items() if p.get("owner") == color}
    best = 0
    seen: set = set()
    for start in owned:
        if start in seen:
            continue
        size = 0
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            size += 1
            for nb in provs[cur]["adj_land"]:
                if nb in owned and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        best = max(best, size)
    return best


def count_controlled_seas(gs: Dict[str, Any], color: str) -> int:
    """§5.6.3: un mare è controllato da chi vi ha strettamente più triremi."""
    n = 0
    for sea in gs["map"]["seas"].values():
        tri = sea.get("triremes", {})
        mine = tri.get(color, 0)
        if mine > 0 and all(v < mine for c, v in tri.items() if c != color):
            n += 1
    return n


def count_power_centers(gs: Dict[str, Any], color: str) -> int:
    return sum(
        1
        for p in gs["map"]["provinces"].values()
        if p.get("owner") == color and p.get("power_center")
    )


def compute_score_awards(gs: Dict[str, Any], color: str) -> tuple[int, list[str]]:
    """
    §5.6: calcola i Punti Vittoria spettanti al giocatore di turno.
    Ritorna (punti, dettagli) — i pareggi non assegnano punti.
    """
    points = 0
    details: list[str] = []
    other_colors = [p["color"] for p in gs["players"] if p["color"] != color]

    # 5.6.1 impero maggiore (>= 4 province, strettamente il più grande)
    my_empire = largest_empire_size(gs, color)
    best_other_empire = max(
        (largest_empire_size(gs, c) for c in other_colors), default=0
    )
    if my_empire >= 4 and my_empire > best_other_empire:
        points += 1
        details.append(f"largest empire ({my_empire})")

    # 5.6.2 maggior numero di province (strettamente)
    my_provs = count_owned_provinces(gs, color)
    best_other_provs = max(
        (count_owned_provinces(gs, c) for c in other_colors), default=0
    )
    if my_provs > best_other_provs:
        points += 1
        details.append(f"most provinces ({my_provs})")

    # 5.6.3 controllo dei mari (strettamente)
    my_seas = count_controlled_seas(gs, color)
    best_other_seas = max(
        (count_controlled_seas(gs, c) for c in other_colors), default=0
    )
    if my_seas > 0 and my_seas > best_other_seas:
        points += 1
        details.append(f"sea control ({my_seas})")

    # 5.6.4 centri di potere (1 VP ciascuno)
    pc = count_power_centers(gs, color)
    if pc > 0:
        points += pc
        details.append(f"power centers ({pc})")

    return points, details


def check_elimination(room: str, color: str, conqueror: Dict[str, Any]) -> None:
    """
    §18: se un giocatore perde l'ultima provincia viene eliminato;
    chi lo elimina ne acquisisce le carte, le sue triremi escono dal gioco.
    """
    gs = GAMES[room]
    victim = next((p for p in gs["players"] if p.get("color") == color), None)
    if victim is None or victim.get("eliminated"):
        return
    if count_owned_provinces(gs, color) > 0:
        return

    victim["eliminated"] = True
    conqueror["cards"].extend(victim["cards"])
    victim["cards"] = []
    for sea in gs["map"]["seas"].values():
        sea.get("triremes", {}).pop(color, None)
    add_log(room, f"{victim['name']} ELIMINATED by {conqueror['name']}")

    alive = [p for p in gs["players"] if not p.get("eliminated")]
    if len(alive) == 1:
        gs["turn"]["phase"] = "GAME_OVER"
        gs["winner"] = alive[0]["name"]
        add_log(room, f"GAME OVER: {alive[0]['name']} wins by total conquest")


def finish_turn(room: str) -> None:
    """
    Chiude il turno del giocatore corrente: pesca carta se ha conquistato
    (§17.1-17.4), poi passa al prossimo giocatore non eliminato (fase SCORE).
    """
    gs = GAMES[room]
    turn = gs["turn"]
    p = gs["players"][turn["turnIndex"]]

    if turn["conqueredThisTurn"]:
        card = draw_card(gs)
        if card is not None:
            p["cards"].append(card)
            add_log(room, f"{p['name']} draws a card ({len(p['cards'])} in hand)")

    gs["pending"]["occupation"] = None
    gs["pending"]["landReinforceRemaining"] = 0

    n = len(gs["players"])
    for _ in range(n):
        turn["turnIndex"] = (turn["turnIndex"] + 1) % n
        if turn["turnIndex"] == 0:
            turn["round"] += 1
        if not gs["players"][turn["turnIndex"]].get("eliminated"):
            break

    turn["phase"] = "SCORE"
    reset_turn_tracking(turn)
    nxt = gs["players"][turn["turnIndex"]]
    add_log(room, f"Turn -> {nxt['name']} (round {turn['round']})")


@app.get("/health")
def health():
    return {"ok": True, "service": "spqrisiko-mvp"}

@app.get("/map")
def get_map_debug():
    return get_map()


@app.websocket("/ws/{room}/{player_name}")
async def ws_endpoint(ws: WebSocket, room: str, player_name: str):
    await ws.accept()
    room = room.upper()

    # create game state if missing
    if room not in GAMES:
        GAMES[room] = new_game_state(room)
    gs = GAMES[room]

    # --- resume di una sessione esistente (riconnessione) ---
    req_pid = ws.query_params.get("playerId")
    req_token = ws.query_params.get("token")

    player = None
    if req_pid and req_token and TOKENS.get(room, {}).get(req_pid) == req_token:
        player = get_player_by_id(gs, req_pid)

    if player is None:
        # nuova registrazione: possibile solo in LOBBY
        if gs["turn"]["phase"] != "LOBBY":
            await ws.send_json({"type": "error", "error": "Game already started: cannot join without a valid session"})
            await ws.close(code=4001)
            return

        player_id = secrets.token_hex(4)
        token = secrets.token_hex(16)
        TOKENS.setdefault(room, {})[player_id] = token
        player = {
            "id": player_id,
            "name": player_name,
            "ready": False,
            "color": None,
            "score": 0,
            "cards": [],
            "eliminated": False,
            "connected": True,
        }
        gs["players"].append(player)
        add_log(room, f"{player_name} joined")
    else:
        # riattacco: chiudi l'eventuale socket precedente rimasto appeso
        player_id = player["id"]
        token = TOKENS[room][player_id]
        old_ws = PLAYER_WS.get(room, {}).get(player_id)
        if old_ws is not None and old_ws is not ws:
            ROOMS.get(room, set()).discard(old_ws)
            try:
                await old_ws.close(code=4002)
            except Exception:
                pass
        player["connected"] = True
        add_log(room, f"{player['name']} reconnected")

    if room not in ROOMS:
        ROOMS[room] = set()
    ROOMS[room].add(ws)
    PLAYER_WS.setdefault(room, {})[player_id] = ws

    # welcome to this client (il token serve al client per riconnettersi)
    await ws.send_json({"type": "welcome", "room": room, "playerId": player_id, "token": token})

    # notify everyone + send state snapshot
    await broadcast_json_async(room, {"type": "join", "room": room, "player": player["name"]})
    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})

    try:
        while True:
            raw = await ws.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await broadcast_json_async(room, {"type": "msg", "room": room, "player": player_name, "text": raw})
                continue

            if msg.get("type") != "cmd":
                await ws.send_json({"type": "error", "error": "Expected type=cmd"})
                continue

            cmd = msg.get("cmd")
            pid = msg.get("playerId")
            payload = msg.get("payload", {})

            if pid != player_id:
                await ws.send_json({"type": "error", "error": "playerId mismatch"})
                continue

            # --- protect the websocket loop from crashing ---
            try:
                # READY
                if cmd == "ready":
                    for p in GAMES[room]["players"]:
                        if p["id"] == player_id:
                            p["ready"] = not p["ready"]
                            add_log(room, f"{p['name']} ready={p['ready']}")
                            break
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": GAMES[room]})
                    continue

                # RESET GAME (keep connected players, reset game state)
                if cmd == "reset_game":
                    old_players = GAMES[room]["players"]

                    GAMES[room] = new_game_state(room)
                    for p in old_players:
                        GAMES[room]["players"].append({
                            "id": p["id"],
                            "name": p["name"],
                            "ready": False,
                            "color": None,
                            "score": 0,
                            "cards": [],
                            "eliminated": False,
                            "connected": p.get("connected", True),
                        })

                    add_log(room, "Game reset -> LOBBY")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": GAMES[room]})
                    continue

                # START GAME -> SETUP
                if cmd == "start_game":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "LOBBY":
                        await ws.send_json({"type": "error", "error": "Game already started (phase != LOBBY). Use Reset."})
                        continue
                    if len(gs["players"]) < 3:
                        await ws.send_json({"type": "error", "error": "Min 3 players"})
                        continue
                    if len(gs["players"]) > MAX_PLAYERS:
                        await ws.send_json({"type": "error", "error": f"Max {MAX_PLAYERS} players (§2.6)"})
                        continue
                    if not all(p["ready"] for p in gs["players"]):
                        await ws.send_json({"type": "error", "error": "All players must be ready"})
                        continue

                    # punteggio di vittoria configurabile (§3.1)
                    try:
                        target = int(payload.get("targetScore", gs["settings"]["targetScore"]))
                        if target > 0:
                            gs["settings"]["targetScore"] = target
                    except Exception:
                        pass

                    assign_colors(gs)
                    init_neutrals(gs)
                    gs["deck"] = build_deck()

                    # fixed neutral placements (4 players: ITALIA)
                    for nf in gs["setup"]["neutralFixed"]:
                        pid_ = str(nf["provinceId"]).strip().upper()
                        if pid_ in gs["map"]["provinces"]:
                            prov = gs["map"]["provinces"][pid_]
                            prov["owner"] = f"NEUTRAL_{nf['color']}"
                            prov["legions"] = nf["size"]
                            add_log(room, f"Neutral fixed {nf['color']} -> {pid_} (3)")
                        else:
                            add_log(room, f"WARNING: neutral fixed province missing in map: {pid_}")

                    gs["turn"]["phase"] = "SETUP"
                    gs["turn"]["turnIndex"] = 0
                    gs["setup"]["claimedByPlayers"] = 0
                    add_log(room, "Game started -> SETUP")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # SETUP CLAIM
                if cmd == "setup_claim":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "SETUP":
                        await ws.send_json({"type": "error", "error": "Not in SETUP"})
                        continue

                    # only current player can claim
                    if len(gs["players"]) == 0:
                        await ws.send_json({"type": "error", "error": "No players in room"})
                        continue

                    turn_player = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    # normalize ids: now IDs are map keys (e.g. PROV_1), so just uppercase/strip
                    raw_pid = payload.get("provinceId")
                    province_id = str(raw_pid).strip().upper() if raw_pid is not None else ""
                    if not province_id:
                        await ws.send_json({"type": "error", "error": "provinceId is required"})
                        continue

                    if province_id not in gs["map"]["provinces"]:
                        await ws.send_json({"type": "error", "error": f"Invalid provinceId: {province_id}"})
                        continue

                    prov = gs["map"]["provinces"][province_id]
                    if prov.get("owner") is not None:
                        await ws.send_json({"type": "error", "error": "Province already owned"})
                        continue

                    # claim for player: 2 legions
                    p = next(x for x in gs["players"] if x["id"] == player_id)
                    prov["owner"] = p["color"]
                    prov["legions"] = 2
                    gs["setup"]["claimedByPlayers"] += 1
                    add_log(room, f"{p['name']} claimed {province_id} (2)")

                    # neutral placement mandatory while pool not empty
                    if len(gs["setup"]["neutralPool"]) > 0:
                        raw_npid = payload.get("neutralProvinceId")
                        neutral_prov_id = str(raw_npid).strip().upper() if raw_npid is not None else ""

                        if not neutral_prov_id:
                            await ws.send_json({"type": "error", "error": "neutralProvinceId is required in this turn"})
                            continue

                        if neutral_prov_id not in gs["map"]["provinces"]:
                            await ws.send_json({"type": "error", "error": f"Invalid neutralProvinceId: {neutral_prov_id}"})
                            continue

                        nprov = gs["map"]["provinces"][neutral_prov_id]
                        if nprov.get("owner") is not None:
                            await ws.send_json({"type": "error", "error": "Neutral province already owned"})
                            continue

                        grp = gs["setup"]["neutralPool"].pop(0)
                        nprov["owner"] = f"NEUTRAL_{grp['color']}"
                        nprov["legions"] = grp["size"]
                        add_log(room, f"Neutral {grp['color']} claimed {neutral_prov_id} (3)")

                    # next player in setup
                    gs["turn"]["turnIndex"] = (gs["turn"]["turnIndex"] + 1) % len(gs["players"])



                    # end setup when each player has 9 provinces (player-claims counted)
                    if gs["setup"]["claimedByPlayers"] >= 9 * len(gs["players"]):
                        gs["turn"]["phase"] = "SCORE"
                        add_log(room, "SETUP complete -> SCORE")

                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # REINFORCE LAND: BEGIN (from SCORE)
                if cmd == "reinforce_land_begin":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "SCORE":
                        await ws.send_json({"type": "error", "error": "Not in SCORE"})
                        continue

                    # solo player di turno
                    if len(gs["players"]) == 0:
                        await ws.send_json({"type": "error", "error": "No players"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    if not p or not p.get("color"):
                        await ws.send_json({"type": "error", "error": "Player/color not initialized"})
                        continue

                    # §5: aggiornamento punteggio come prima azione del turno
                    pts, details = compute_score_awards(gs, p["color"])
                    if pts > 0:
                        p["score"] += pts
                        add_log(room, f"{p['name']} +{pts} VP ({', '.join(details)}) -> {p['score']}")

                    # §4.3.1: vittoria istantanea al raggiungimento del punteggio
                    if p["score"] >= gs["settings"]["targetScore"]:
                        gs["turn"]["phase"] = "GAME_OVER"
                        gs["winner"] = p["name"]
                        add_log(room, f"GAME OVER: {p['name']} wins with {p['score']} VP")
                        await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                        continue

                    # reset tracking turno
                    reset_turn_tracking(gs["turn"])

                    r = calc_land_reinforcements(gs, p["color"])
                    gs["pending"]["landReinforceRemaining"] = r
                    gs["turn"]["phase"] = "REINFORCE_LAND"
                    add_log(room, f"{p['name']} REINFORCE_LAND begin: +{r} legions")

                    # §6.5/§16.5: +1 legione per ogni provincia propria con centro di
                    # potere, piazzata obbligatoriamente lì (quindi auto-piazzata)
                    pc_bonus = []
                    for prov_id, prov in gs["map"]["provinces"].items():
                        if prov.get("owner") == p["color"] and prov.get("power_center"):
                            prov["legions"] += 1
                            pc_bonus.append(prov_id)
                    if pc_bonus:
                        add_log(room, f"{p['name']} +1 legion on each power center: {', '.join(pc_bonus)} (§6.5)")

                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                                # REINFORCE LAND: PLACE
                if cmd == "reinforce_land_place":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "REINFORCE_LAND":
                        await ws.send_json({"type": "error", "error": "Not in REINFORCE_LAND"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    if not p or not p.get("color"):
                        await ws.send_json({"type": "error", "error": "Player/color not initialized"})
                        continue

                    remaining = int(gs.get("pending", {}).get("landReinforceRemaining", 0))
                    if remaining <= 0:
                        await ws.send_json({"type": "error", "error": "No reinforcements remaining (call reinforce_land_begin?)"})
                        continue

                    placements = payload.get("placements")
                    if not isinstance(placements, dict) or len(placements) == 0:
                        await ws.send_json({"type": "error", "error": "placements must be a non-empty object"})
                        continue

                    # valida somme + ownership
                    total = 0
                    for raw_prov_id, raw_count in placements.items():
                        prov_id = str(raw_prov_id).strip().upper()
                        try:
                            cnt = int(raw_count)
                        except Exception:
                            await ws.send_json({"type": "error", "error": f"Invalid count for {prov_id}"})
                            break
                        if cnt <= 0:
                            await ws.send_json({"type": "error", "error": f"Count must be > 0 for {prov_id}"})
                            break
                        if prov_id not in gs["map"]["provinces"]:
                            await ws.send_json({"type": "error", "error": f"Invalid provinceId: {prov_id}"})
                            break
                        prov = gs["map"]["provinces"][prov_id]
                        if prov.get("owner") != p["color"]:
                            await ws.send_json({"type": "error", "error": f"You do not own {prov_id}"})
                            break
                        total += cnt
                    else:
                        # esegue solo se il loop NON è uscito via break
                        if total != remaining:
                            await ws.send_json({"type": "error", "error": f"Placements sum {total} != remaining {remaining}"})
                            continue

                        # applica
                        for raw_prov_id, raw_count in placements.items():
                            prov_id = str(raw_prov_id).strip().upper()
                            cnt = int(raw_count)
                            gs["map"]["provinces"][prov_id]["legions"] += cnt

                        gs["pending"]["landReinforceRemaining"] = 0

                        gs["turn"]["phase"] = "REINFORCE_NAVAL"
                        add_log(room, f"{p['name']} placed +{total} legions. Phase -> REINFORCE_NAVAL")

                        await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                        continue

                    # se siamo usciti con break, abbiamo già mandato l'error sopra
                    continue

                # PLAY TRIS (§8): scambia 3 carte per rinforzi supplementari
                if cmd == "play_tris":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "REINFORCE_LAND":
                        await ws.send_json({"type": "error", "error": "Tris can only be played in REINFORCE_LAND (§8.2)"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)

                    if gs["turn"].get("trisPlayed"):
                        await ws.send_json({"type": "error", "error": "Already played a tris this turn (§4.4.1)"})
                        continue

                    idxs = payload.get("cards")
                    if (not isinstance(idxs, list) or len(idxs) != 3
                            or len(set(idxs)) != 3
                            or not all(isinstance(i, int) for i in idxs)
                            or not all(0 <= i < len(p["cards"]) for i in idxs)):
                        await ws.send_json({"type": "error", "error": f"cards must be 3 distinct indexes 0..{len(p['cards']) - 1}"})
                        continue

                    cards = [p["cards"][i] for i in idxs]
                    symbols = [c["symbol"] for c in cards]

                    # §8.4-8.6: 3 uguali = 8 legioni, 3 diverse = 10
                    if len(set(symbols)) == 1:
                        base = 8
                    elif len(set(symbols)) == 3:
                        base = 10
                    else:
                        await ws.send_json({"type": "error", "error": "Invalid tris: need 3 equal or 3 different symbols (§8.4)"})
                        continue

                    # §8.7.1: +2 legioni per ogni vessillo
                    bonus_legions = 2 * symbols.count("VESSILLO")

                    # §8.7.2: +1 trireme per ogni carta trireme, in un mare adiacente
                    # a una propria provincia (beneficio facoltativo, §8.8)
                    tri_cards = symbols.count("TRIREME")
                    tri_seas_raw = payload.get("triremeSeas") or []
                    if not isinstance(tri_seas_raw, list) or len(tri_seas_raw) > tri_cards:
                        await ws.send_json({"type": "error", "error": f"triremeSeas must be a list of at most {tri_cards} seas"})
                        continue

                    tri_seas = []
                    tri_error = None
                    for raw in tri_seas_raw:
                        sid = normalize_prov_id(raw)
                        if sid not in gs["map"]["seas"]:
                            tri_error = f"Invalid seaId: {sid}"
                            break
                        sea = gs["map"]["seas"][sid]
                        touches_mine = any(
                            gs["map"]["provinces"][pid].get("owner") == p["color"]
                            for pid in sea.get("adj_land", [])
                        )
                        if not touches_mine:
                            tri_error = f"{sid} is not adjacent to any of your provinces (§8.7.2)"
                            break
                        tri_seas.append(sid)
                    if tri_error:
                        await ws.send_json({"type": "error", "error": tri_error})
                        continue

                    # §8.7.3: centro di potere se il tris contiene un'arena (facoltativo)
                    pc_prov = normalize_prov_id(payload.get("powerCenterProvince"))
                    if pc_prov:
                        if "ARENA" not in symbols:
                            await ws.send_json({"type": "error", "error": "No arena card in this tris (§8.7.3)"})
                            continue
                        ok, reason = can_place_power_center(gs, p["color"], pc_prov)
                        if not ok:
                            await ws.send_json({"type": "error", "error": reason})
                            continue

                    # --- tutto valido: applica ---
                    for i in sorted(idxs, reverse=True):
                        gs["discard"].append(p["cards"].pop(i))

                    gs["pending"]["landReinforceRemaining"] += base + bonus_legions
                    for sid in tri_seas:
                        sea = gs["map"]["seas"][sid]
                        sea.setdefault("triremes", {})
                        sea["triremes"][p["color"]] = sea["triremes"].get(p["color"], 0) + 1
                    if pc_prov:
                        gs["map"]["provinces"][pc_prov]["power_center"] = True

                    gs["turn"]["trisPlayed"] = True

                    parts = [f"+{base + bonus_legions} legions"]
                    if tri_seas:
                        parts.append(f"+{len(tri_seas)} triremes ({', '.join(tri_seas)})")
                    if pc_prov:
                        parts.append(f"power center in {pc_prov}")
                    add_log(room, f"{p['name']} TRIS [{', '.join(symbols)}]: {' | '.join(parts)}")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # TRIREME -> LEGIONS (§6.6): durante i rinforzi terrestri, una trireme
                # può essere convertita in 2 legioni in una provincia adiacente al mare
                if cmd == "trireme_to_legions":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "REINFORCE_LAND":
                        await ws.send_json({"type": "error", "error": "Not in REINFORCE_LAND"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    sea_id = normalize_prov_id(payload.get("seaId"))
                    prov_id = normalize_prov_id(payload.get("provinceId"))

                    if sea_id not in gs["map"]["seas"]:
                        await ws.send_json({"type": "error", "error": f"Invalid seaId: {sea_id}"})
                        continue
                    if prov_id not in gs["map"]["provinces"]:
                        await ws.send_json({"type": "error", "error": f"Invalid provinceId: {prov_id}"})
                        continue

                    sea = gs["map"]["seas"][sea_id]
                    prov = gs["map"]["provinces"][prov_id]

                    if tri_count(sea, p["color"]) < 1:
                        await ws.send_json({"type": "error", "error": f"No trireme in {sea_id}"})
                        continue
                    if prov.get("owner") != p["color"]:
                        await ws.send_json({"type": "error", "error": f"You do not own {prov_id}"})
                        continue
                    if sea_id not in prov.get("adj_sea", []):
                        await ws.send_json({"type": "error", "error": f"{prov_id} is not adjacent to {sea_id}"})
                        continue

                    remove_triremes(sea, p["color"], 1)
                    prov["legions"] += 2
                    add_log(room, f"{p['name']} trireme in {sea_id} -> +2 legions in {prov_id}")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # BUY TRIREME (§7.1-7.3): 3 legioni da una provincia costiera -> 1 trireme
                if cmd == "buy_trireme":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "REINFORCE_NAVAL":
                        await ws.send_json({"type": "error", "error": "Not in REINFORCE_NAVAL"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    prov_id = normalize_prov_id(payload.get("provinceId"))
                    sea_id = normalize_prov_id(payload.get("seaId"))

                    if prov_id not in gs["map"]["provinces"]:
                        await ws.send_json({"type": "error", "error": f"Invalid provinceId: {prov_id}"})
                        continue
                    if sea_id not in gs["map"]["seas"]:
                        await ws.send_json({"type": "error", "error": f"Invalid seaId: {sea_id}"})
                        continue

                    prov = gs["map"]["provinces"][prov_id]
                    sea = gs["map"]["seas"][sea_id]

                    if prov.get("owner") != p["color"]:
                        await ws.send_json({"type": "error", "error": f"You do not own {prov_id}"})
                        continue
                    if sea_id not in prov.get("adj_sea", []):
                        await ws.send_json({"type": "error", "error": f"{prov_id} is not adjacent to {sea_id}"})
                        continue
                    # 3 legioni da convertire + 1 di guarnigione (§14.1)
                    if int(prov.get("legions", 0)) < 4:
                        await ws.send_json({"type": "error", "error": "Need at least 4 legions (3 to convert + 1 garrison)"})
                        continue

                    prov["legions"] -= 3
                    sea.setdefault("triremes", {})
                    sea["triremes"][p["color"]] = sea["triremes"].get(p["color"], 0) + 1
                    add_log(room, f"{p['name']} -3 legions in {prov_id} -> +1 trireme in {sea_id}")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # END PHASE: avanza fra le fasi navali (tutte facoltative)
                if cmd == "end_phase":
                    gs = GAMES[room]
                    NEXT_PHASE = {
                        "REINFORCE_NAVAL": "NAVAL_MOVE",
                        "NAVAL_MOVE": "NAVAL_COMBAT",
                        "NAVAL_COMBAT": "SEA_ATTACKS",
                        "SEA_ATTACKS": "LAND_ATTACKS",
                    }
                    current = gs["turn"]["phase"]
                    if current not in NEXT_PHASE:
                        await ws.send_json({"type": "error", "error": f"Cannot end phase from {current}"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    gs["turn"]["phase"] = NEXT_PHASE[current]
                    add_log(room, f"Phase -> {gs['turn']['phase']}")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # NAVAL MOVE (§9.3-9.4): un solo movimento di triremi fra mari adiacenti
                if cmd == "naval_move":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "NAVAL_MOVE":
                        await ws.send_json({"type": "error", "error": "Not in NAVAL_MOVE"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    from_sea_id = normalize_prov_id(payload.get("fromSea"))
                    to_sea_id = normalize_prov_id(payload.get("toSea"))
                    try:
                        count = int(payload.get("count", 0))
                    except Exception:
                        count = 0

                    if from_sea_id not in gs["map"]["seas"] or to_sea_id not in gs["map"]["seas"]:
                        await ws.send_json({"type": "error", "error": "Invalid sea id"})
                        continue
                    if from_sea_id == to_sea_id:
                        await ws.send_json({"type": "error", "error": "fromSea and toSea must be different"})
                        continue

                    from_sea = gs["map"]["seas"][from_sea_id]
                    to_sea = gs["map"]["seas"][to_sea_id]

                    if to_sea_id not in from_sea.get("adj_sea", []):
                        await ws.send_json({"type": "error", "error": f"{from_sea_id} is not adjacent to {to_sea_id}"})
                        continue
                    if count < 1:
                        await ws.send_json({"type": "error", "error": "count must be >= 1"})
                        continue
                    if tri_count(from_sea, p["color"]) < count:
                        await ws.send_json({"type": "error", "error": f"Only {tri_count(from_sea, p['color'])} triremes in {from_sea_id}"})
                        continue

                    remove_triremes(from_sea, p["color"], count)
                    to_sea.setdefault("triremes", {})
                    to_sea["triremes"][p["color"]] = to_sea["triremes"].get(p["color"], 0) + count
                    add_log(room, f"{p['name']} NAVAL MOVE {from_sea_id}->{to_sea_id} ({count})")

                    # §9.4: un solo movimento per turno -> avanti
                    gs["turn"]["phase"] = "NAVAL_COMBAT"
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # NAVAL ATTACK (§11): combattimento fra triremi nella stessa area di mare
                if cmd == "naval_attack_roll":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "NAVAL_COMBAT":
                        await ws.send_json({"type": "error", "error": "Not in NAVAL_COMBAT"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    sea_id = normalize_prov_id(payload.get("seaId"))
                    target = normalize_prov_id(payload.get("targetColor"))
                    try:
                        requested_dice = int(payload.get("attackDice", 0))
                    except Exception:
                        requested_dice = 0

                    if sea_id not in gs["map"]["seas"]:
                        await ws.send_json({"type": "error", "error": f"Invalid seaId: {sea_id}"})
                        continue
                    if not target or target == p["color"]:
                        await ws.send_json({"type": "error", "error": "targetColor must be another player"})
                        continue

                    sea = gs["map"]["seas"][sea_id]
                    mine = tri_count(sea, p["color"])
                    theirs = tri_count(sea, target)

                    if mine < 1:
                        await ws.send_json({"type": "error", "error": f"You have no triremes in {sea_id}"})
                        continue
                    if theirs < 1:
                        await ws.send_json({"type": "error", "error": f"{target} has no triremes in {sea_id}"})
                        continue

                    # §11.4: un solo combattimento per area di mare a turno
                    combat = gs["turn"]["navalCombats"].get(sea_id)
                    if combat and combat.get("closed"):
                        await ws.send_json({"type": "error", "error": f"Combat in {sea_id} already closed this turn"})
                        continue
                    if combat and combat.get("target") != target:
                        await ws.send_json({"type": "error", "error": f"Combat in {sea_id} already declared vs {combat['target']}"})
                        continue

                    def_dice = min(3, theirs)  # §10.5: il difensore usa sempre il massimo
                    max_att_dice = min(3, mine)
                    if requested_dice <= 0:
                        requested_dice = max_att_dice
                    att_dice = min(max_att_dice, requested_dice)

                    if att_dice < def_dice:
                        await ws.send_json({"type": "error", "error": f"Cannot attack with fewer dice than defender (att={att_dice} def={def_dice})"})
                        continue

                    att_roll = roll_dice(att_dice)
                    def_roll = roll_dice(def_dice)
                    a_loss, d_loss = resolve_naval_roll(att_roll, def_roll)

                    if a_loss > 0:
                        remove_triremes(sea, p["color"], a_loss)
                    if d_loss > 0:
                        remove_triremes(sea, target, d_loss)

                    # §11.6: se il lancio non provoca perdite, il combattimento cessa
                    closed = (a_loss + d_loss == 0)
                    gs["turn"]["navalCombats"][sea_id] = {"target": target, "closed": closed}

                    add_log(room, f"NAVAL {p['name']} vs {target} in {sea_id} A{att_roll} D{def_roll} | losses A-{a_loss} D-{d_loss}{' | combat closed' if closed else ''}")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # SEA ATTACK (§12): attacco fra province adiacenti allo stesso mare,
                # combattimento ad oltranza con forza dichiarata in anticipo
                if cmd == "sea_attack":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "SEA_ATTACKS":
                        await ws.send_json({"type": "error", "error": "Not in SEA_ATTACKS"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    from_id = normalize_prov_id(payload.get("from"))
                    to_id = normalize_prov_id(payload.get("to"))
                    sea_id = normalize_prov_id(payload.get("seaId"))
                    try:
                        legions = int(payload.get("legions", 0))
                    except Exception:
                        legions = 0

                    if from_id not in gs["map"]["provinces"] or to_id not in gs["map"]["provinces"]:
                        await ws.send_json({"type": "error", "error": "Invalid province id"})
                        continue
                    if sea_id not in gs["map"]["seas"]:
                        await ws.send_json({"type": "error", "error": f"Invalid seaId: {sea_id}"})
                        continue

                    prov_from = gs["map"]["provinces"][from_id]
                    prov_to = gs["map"]["provinces"][to_id]
                    sea = gs["map"]["seas"][sea_id]

                    if prov_from.get("owner") != p["color"]:
                        await ws.send_json({"type": "error", "error": f"You do not own {from_id}"})
                        continue
                    if prov_to.get("owner") == p["color"]:
                        await ws.send_json({"type": "error", "error": f"Target {to_id} is already yours"})
                        continue
                    if sea_id not in prov_from.get("adj_sea", []) or sea_id not in prov_to.get("adj_sea", []):
                        await ws.send_json({"type": "error", "error": f"Both provinces must border {sea_id}"})
                        continue

                    # §12.7: da una provincia conquistata via mare non si ri-attacca via mare
                    if from_id in gs["turn"]["seaConqueredProvinces"]:
                        await ws.send_json({"type": "error", "error": f"{from_id} was conquered by sea this turn: no new sea attack from it"})
                        continue
                    # §12.6: la stessa provincia non può subire due attacchi via mare nel turno
                    if to_id in gs["turn"]["seaAttackedProvinces"]:
                        await ws.send_json({"type": "error", "error": f"{to_id} was already attacked by sea this turn"})
                        continue

                    # §12.2: servono strettamente più triremi del difensore in quel mare
                    defender_color = prov_to.get("owner")
                    my_tri = tri_count(sea, p["color"])
                    def_tri = 0
                    if defender_color and not str(defender_color).startswith("NEUTRAL_"):
                        def_tri = tri_count(sea, defender_color)
                    if my_tri < 1 or my_tri <= def_tri:
                        await ws.send_json({"type": "error", "error": f"Need more triremes than defender in {sea_id} (you={my_tri} def={def_tri})"})
                        continue

                    # §14.3: la guarnigione minima limita anche l'attacco via mare
                    garrison = min_garrison(gs, from_id, p["color"])
                    available = int(prov_from.get("legions", 0)) - garrison
                    if legions < 1 or legions > available:
                        await ws.send_json({"type": "error", "error": f"legions must be 1..{max(available, 0)} (garrison {garrison}, §14.3)"})
                        continue

                    def_legions = int(prov_to.get("legions", 0))
                    if def_legions <= 0:
                        await ws.send_json({"type": "error", "error": "Target has no legions (invalid state)"})
                        continue

                    # §18.4: niente eliminazioni prima della fine del 4° round
                    if is_last_province_protected(gs, defender_color):
                        await ws.send_json({"type": "error", "error": f"Cannot attack {defender_color}'s last province before the end of round {NO_ELIMINATION_ROUNDS} (§18.4)"})
                        continue

                    # §12.3: forza dichiarata, combattimento ad oltranza
                    prov_from["legions"] -= legions
                    att_force = legions
                    rolls = 0
                    add_log(room, f"SEA ATTACK {p['name']} {from_id}->{to_id} via {sea_id} with {att_force} vs {def_legions}")

                    while att_force > 0 and def_legions > 0 and rolls < 1000:
                        att_dice = min(3, att_force)
                        def_dice = min(3, def_legions)
                        att_roll = roll_dice(att_dice)
                        def_roll = roll_dice(def_dice)
                        a_loss, d_loss = resolve_risk_roll(att_roll, def_roll)
                        att_force -= a_loss
                        def_legions -= d_loss
                        rolls += 1
                        add_log(room, f"  roll A{att_roll} D{def_roll} | losses A-{a_loss} D-{d_loss} -> {max(att_force,0)} vs {max(def_legions,0)}")

                    gs["turn"]["seaAttackedProvinces"].append(to_id)

                    if def_legions <= 0 and att_force > 0:
                        prev_owner = prov_to.get("owner")
                        prov_to["owner"] = p["color"]
                        prov_to["legions"] = att_force
                        gs["turn"]["conqueredThisTurn"] = True
                        gs["turn"]["seaConqueredProvinces"].append(to_id)
                        add_log(room, f"CONQUERED {to_id} by sea ({att_force} legions landed)")

                        if prev_owner and not str(prev_owner).startswith("NEUTRAL_"):
                            check_elimination(room, prev_owner, p)
                    else:
                        prov_to["legions"] = max(1, def_legions)
                        add_log(room, f"SEA ATTACK failed: attacking force destroyed, {to_id} holds")

                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # LAND ATTACKS: one roll per command (server-side dice)
                if cmd == "land_attack_roll":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "LAND_ATTACKS":
                        await ws.send_json({"type": "error", "error": "Not in LAND_ATTACKS"})
                        continue

                    if len(gs["players"]) == 0:
                        await ws.send_json({"type": "error", "error": "No players"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    if not p or not p.get("color"):
                        await ws.send_json({"type": "error", "error": "Player/color not initialized"})
                        continue

                    from_id = normalize_prov_id(payload.get("from"))
                    to_id = normalize_prov_id(payload.get("to"))
                    try:
                        requested_dice = int(payload.get("attackDice", 0))
                    except Exception:
                        requested_dice = 0

                    if not from_id or not to_id:
                        await ws.send_json({"type": "error", "error": "from and to are required"})
                        continue
                    if from_id == to_id:
                        await ws.send_json({"type": "error", "error": "from and to must be different"})
                        continue
                    if from_id not in gs["map"]["provinces"] or to_id not in gs["map"]["provinces"]:
                        await ws.send_json({"type": "error", "error": "Invalid province id"})
                        continue

                    prov_from = gs["map"]["provinces"][from_id]
                    prov_to = gs["map"]["provinces"][to_id]

                    if prov_from.get("owner") != p["color"]:
                        await ws.send_json({"type": "error", "error": f"You do not own {from_id}"})
                        continue
                    if prov_to.get("owner") == p["color"]:
                        await ws.send_json({"type": "error", "error": f"Target {to_id} is already yours"})
                        continue

                    # adjacency (land only)
                    if to_id not in prov_from.get("adj_land", []):
                        await ws.send_json({"type": "error", "error": f"{from_id} is not adjacent by land to {to_id}"})
                        continue

                    from_leg = int(prov_from.get("legions", 0))
                    to_leg = int(prov_to.get("legions", 0))

                    if from_leg <= 1:
                        await ws.send_json({"type": "error", "error": "Not enough legions to attack (need >= 2)"})
                        continue
                    if to_leg <= 0:
                        await ws.send_json({"type": "error", "error": "Target has no legions (invalid state)"})
                        continue

                    # §18.4: niente eliminazioni prima della fine del 4° round
                    if is_last_province_protected(gs, prov_to.get("owner")):
                        await ws.send_json({"type": "error", "error": f"Cannot attack {prov_to.get('owner')}'s last province before the end of round {NO_ELIMINATION_ROUNDS} (§18.4)"})
                        continue

                    # dice counts
                    def_dice = min(3, to_leg)  # defender must roll max
                    max_att_dice = min(3, from_leg - 1)
                    if requested_dice <= 0:
                        requested_dice = max_att_dice
                    att_dice = min(max_att_dice, requested_dice)

                    # rule: cannot attack with fewer dice than defender
                    if att_dice < def_dice:
                        await ws.send_json({"type": "error", "error": f"Cannot attack with fewer dice than defender (att={att_dice} def={def_dice})"})
                        continue

                    # un nuovo attacco chiude la finestra di occupazione precedente
                    gs["pending"]["occupation"] = None

                    att_roll = roll_dice(att_dice)
                    def_roll = roll_dice(def_dice)
                    a_loss, d_loss = resolve_risk_roll(att_roll, def_roll)

                    # apply losses
                    prov_from["legions"] = max(0, from_leg - a_loss)
                    prov_to["legions"] = max(0, to_leg - d_loss)

                    add_log(room, f"LAND ATTACK {p['name']} {from_id}->{to_id} A{sorted(att_roll, reverse=True)} D{sorted(def_roll, reverse=True)} | losses A-{a_loss} D-{d_loss}")

                    # conquest check
                    if prov_to["legions"] <= 0:
                        prev_owner = prov_to.get("owner")

                        # must occupy with at least the number of dice used in last attack roll
                        move_min = att_dice
                        # safety: cannot move more than available - 1 (keep 1 behind)
                        available_to_move = max(0, prov_from["legions"] - 1)
                        if move_min > available_to_move:
                            # this should be rare but keep state valid
                            move_min = available_to_move

                        prov_to["owner"] = p["color"]
                        prov_to["legions"] = move_min
                        prov_from["legions"] = prov_from["legions"] - move_min

                        gs["turn"]["conqueredThisTurn"] = True
                        # §13.4: finché non fa altro, può spostare legioni extra
                        gs["pending"]["occupation"] = {"from": from_id, "to": to_id}
                        add_log(room, f"CONQUERED {to_id} by {p['name']} (moved {move_min})")

                        # §18: eliminazione se il difensore era un giocatore
                        if prev_owner and not str(prev_owner).startswith("NEUTRAL_"):
                            check_elimination(room, prev_owner, p)

                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue


                # OCCUPY EXTRA: sposta legioni aggiuntive nella provincia appena conquistata (§13.4)
                if cmd == "occupy_extra":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "LAND_ATTACKS":
                        await ws.send_json({"type": "error", "error": "Not in LAND_ATTACKS"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    occ = gs["pending"].get("occupation")
                    if not occ:
                        await ws.send_json({"type": "error", "error": "No pending occupation (conquer first)"})
                        continue

                    try:
                        count = int(payload.get("count", 0))
                    except Exception:
                        count = 0
                    if count < 1:
                        await ws.send_json({"type": "error", "error": "count must be >= 1"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    prov_from = gs["map"]["provinces"][occ["from"]]
                    prov_to = gs["map"]["provinces"][occ["to"]]
                    # §14.2: spostamento volontario -> guarnigione minima (2 se confina col nemico)
                    garrison = min_garrison(gs, occ["from"], p["color"])
                    available = int(prov_from.get("legions", 0)) - garrison
                    if count > available:
                        await ws.send_json({"type": "error", "error": f"Only {max(available, 0)} legions available to move (garrison {garrison}, §14.2)"})
                        continue

                    prov_from["legions"] -= count
                    prov_to["legions"] += count
                    gs["pending"]["occupation"] = None

                    add_log(room, f"{p['name']} moved +{count} into {occ['to']}")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # END ATTACKS: chiude la fase di attacchi terrestri
                if cmd == "end_attacks":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "LAND_ATTACKS":
                        await ws.send_json({"type": "error", "error": "Not in LAND_ATTACKS"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    gs["pending"]["occupation"] = None
                    gs["turn"]["phase"] = "STRATEGIC_MOVE"
                    p = get_player_by_id(gs, player_id)
                    add_log(room, f"{p['name']} ends attacks -> STRATEGIC_MOVE")
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # STRATEGIC MOVE (§15): singolo spostamento fra province proprie adiacenti,
                # poi il turno termina (§15.6)
                if cmd == "strategic_move":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] != "STRATEGIC_MOVE":
                        await ws.send_json({"type": "error", "error": "Not in STRATEGIC_MOVE"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    if not p or not p.get("color"):
                        await ws.send_json({"type": "error", "error": "Player/color not initialized"})
                        continue

                    from_id = normalize_prov_id(payload.get("from"))
                    to_id = normalize_prov_id(payload.get("to"))
                    try:
                        count = int(payload.get("count", 0))
                    except Exception:
                        count = 0

                    if not from_id or not to_id or from_id == to_id:
                        await ws.send_json({"type": "error", "error": "from and to must be different provinces"})
                        continue
                    if from_id not in gs["map"]["provinces"] or to_id not in gs["map"]["provinces"]:
                        await ws.send_json({"type": "error", "error": "Invalid province id"})
                        continue

                    prov_from = gs["map"]["provinces"][from_id]
                    prov_to = gs["map"]["provinces"][to_id]

                    if prov_from.get("owner") != p["color"] or prov_to.get("owner") != p["color"]:
                        await ws.send_json({"type": "error", "error": "You must own both provinces"})
                        continue
                    # adiacenza via terra, oppure via una singola area di mare (§15.2-15.4)
                    # in cui il giocatore ha la superiorità navale
                    route_ok = to_id in prov_from.get("adj_land", [])
                    if not route_ok:
                        for sid in prov_from.get("adj_sea", []):
                            if sid in prov_to.get("adj_sea", []):
                                if has_sea_superiority(gs["map"]["seas"][sid], p["color"]):
                                    route_ok = True
                                    break
                    if not route_ok:
                        await ws.send_json({"type": "error", "error": f"No route {from_id}->{to_id} (land adjacency or sea with naval superiority required)"})
                        continue
                    if count < 1:
                        await ws.send_json({"type": "error", "error": "count must be >= 1"})
                        continue
                    # §14.2: guarnigione minima dopo spostamento volontario
                    garrison = min_garrison(gs, from_id, p["color"])
                    if count > int(prov_from.get("legions", 0)) - garrison:
                        await ws.send_json({"type": "error", "error": f"Must leave at least {garrison} legions in {from_id} (§14.2)"})
                        continue

                    prov_from["legions"] -= count
                    prov_to["legions"] += count
                    gs["turn"]["usedStrategicMove"] = True
                    add_log(room, f"{p['name']} STRATEGIC MOVE {from_id}->{to_id} ({count})")

                    # §15.6: lo spostamento strategico chiude il turno
                    finish_turn(room)
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # END TURN: chiude il turno senza (o dopo) lo spostamento strategico
                if cmd == "end_turn":
                    gs = GAMES[room]
                    if gs["turn"]["phase"] not in ("LAND_ATTACKS", "STRATEGIC_MOVE"):
                        await ws.send_json({"type": "error", "error": "Cannot end turn in this phase"})
                        continue

                    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
                    if turn_player_id != player_id:
                        await ws.send_json({"type": "error", "error": "Not your turn"})
                        continue

                    p = get_player_by_id(gs, player_id)
                    add_log(room, f"{p['name']} ends turn")
                    finish_turn(room)
                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                # DEBUG: advance phase (manual)
                if cmd == "advance_phase":
                    gs = GAMES[room]
                    turn = gs["turn"]
                    current = turn["phase"]

                    if current not in PHASES:
                        turn["phase"] = "LOBBY"
                        add_log(room, f"Phase was invalid -> reset to LOBBY")
                        await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                        continue

                    idx = PHASES.index(current)
                    if idx == len(PHASES) - 1:
                        # end of turn -> next player, next round maybe
                        turn["phase"] = PHASES[0]
                        turn["turnIndex"] = (turn["turnIndex"] + 1) % max(1, len(gs["players"]))
                        if turn["turnIndex"] == 0:
                            turn["round"] += 1
                        add_log(room, f"Turn ended. Next player index={turn['turnIndex']} round={turn['round']}")
                    else:
                        turn["phase"] = PHASES[idx + 1]
                        add_log(room, f"Phase -> {turn['phase']}")

                    await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                    continue

                await ws.send_json({"type": "error", "error": f"Unknown cmd: {cmd}"})

            except Exception as e:
                add_log(room, f"SERVER EXCEPTION: {type(e).__name__}: {e}")
                await ws.send_json({"type": "error", "error": f"Server exception: {type(e).__name__}: {e}"})
                await broadcast_json_async(room, {"type": "state", "room": room, "state": GAMES[room]})
                continue

    except WebSocketDisconnect:
        ROOMS.get(room, set()).discard(ws)

        # se questo socket è stato rimpiazzato da una riconnessione più
        # recente, non toccare lo stato del giocatore
        if PLAYER_WS.get(room, {}).get(player_id) is not ws:
            return
        PLAYER_WS[room].pop(player_id, None)

        gs = GAMES.get(room)
        if gs is None:
            return

        p = get_player_by_id(gs, player_id)
        pname = p["name"] if p else player_name

        if gs["turn"]["phase"] == "LOBBY":
            # in lobby chi esce, esce davvero
            gs["players"] = [x for x in gs["players"] if x["id"] != player_id]
            TOKENS.get(room, {}).pop(player_id, None)
            add_log(room, f"{pname} left")
        else:
            # a partita iniziata resta in gioco: può riconnettersi col token
            if p:
                p["connected"] = False
            add_log(room, f"{pname} disconnected (can rejoin)")

        await broadcast_json_async(room, {"type": "leave", "room": room, "player": pname})
        await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})

        # la room muore solo se vuota E senza una partita in corso
        if len(ROOMS.get(room, set())) == 0 and gs["turn"]["phase"] in ("LOBBY", "GAME_OVER"):
            ROOMS.pop(room, None)
            GAMES.pop(room, None)
            TOKENS.pop(room, None)
            PLAYER_WS.pop(room, None)


# web client
app.mount("/", StaticFiles(directory="static", html=True), name="static")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from typing import Dict, Set, Any, Optional
import json
import secrets
import time
import copy
import random
import random

from map_data import get_map

app = FastAPI()

# --- Multiplayer rooms (connections) ---
ROOMS: Dict[str, Set[WebSocket]] = {}

# --- Game state per room ---
GAMES: Dict[str, Dict[str, Any]] = {}

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
        "turn": {
            "turnIndex": 0,
            "round": 1,
            "phase": "LOBBY",
            # flags utili già da ora
            "conqueredThisTurn": False,
            "usedStrategicMove": False,
        },
        "setup": {
            "claimedByPlayers": 0,
            "neutralPool": [],
            "neutralFixed": [],
        },
        # azioni multi-step / conteggi fase
        "pending": {
            "landReinforceRemaining": 0
        },
        "map": game_map,
        "log": [],
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

    # create room connection set
    if room not in ROOMS:
        ROOMS[room] = set()
    ROOMS[room].add(ws)

    # create game state if missing
    if room not in GAMES:
        GAMES[room] = new_game_state(room)

    # register player
    player_id = secrets.token_hex(4)
    player = {"id": player_id, "name": player_name, "ready": False, "color": None}
    GAMES[room]["players"].append(player)
    add_log(room, f"{player_name} joined")

    # welcome to this client
    await ws.send_json({"type": "welcome", "room": room, "playerId": player_id})

    # notify everyone + send state snapshot
    await broadcast_json_async(room, {"type": "join", "room": room, "player": player_name})
    await broadcast_json_async(room, {"type": "state", "room": room, "state": GAMES[room]})

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
                            "color": None
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
                    if not all(p["ready"] for p in gs["players"]):
                        await ws.send_json({"type": "error", "error": "All players must be ready"})
                        continue

                    assign_colors(gs)
                    init_neutrals(gs)

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
                    

                    # reset flag turno (utile già per futuro)
                    gs["turn"]["conqueredThisTurn"] = False
                    gs["turn"]["usedStrategicMove"] = False

                    r = calc_land_reinforcements(gs, p["color"])
                    gs["pending"]["landReinforceRemaining"] = r
                    gs["turn"]["phase"] = "REINFORCE_LAND"
                    add_log(room, f"{p['name']} REINFORCE_LAND begin: +{r} legions")

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

                        # MVP terrestre: skip navale e vai subito ad attacchi terrestri
                        gs["turn"]["phase"] = "LAND_ATTACKS"
                        add_log(room, f"{p['name']} placed +{total} legions. Phase -> LAND_ATTACKS (naval skipped in MVP)")

                        await broadcast_json_async(room, {"type": "state", "room": room, "state": gs})
                        continue

                    # se siamo usciti con break, abbiamo già mandato l'error sopra
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

                    att_roll = roll_dice(att_dice)
                    def_roll = roll_dice(def_dice)
                    a_loss, d_loss = resolve_risk_roll(att_roll, def_roll)

                    # apply losses
                    prov_from["legions"] = max(0, from_leg - a_loss)
                    prov_to["legions"] = max(0, to_leg - d_loss)

                    add_log(room, f"LAND ATTACK {p['name']} {from_id}->{to_id} A{sorted(att_roll, reverse=True)} D{sorted(def_roll, reverse=True)} | losses A-{a_loss} D-{d_loss}")

                    # conquest check
                    if prov_to["legions"] <= 0:
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
                        add_log(room, f"CONQUERED {to_id} by {p['name']} (moved {move_min})")

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
        ROOMS[room].discard(ws)

        # remove player from game state by id
        gs_players = GAMES[room]["players"]
        GAMES[room]["players"] = [p for p in gs_players if p["id"] != player_id]
        add_log(room, f"{player_name} left")

        await broadcast_json_async(room, {"type": "leave", "room": room, "player": player_name})
        await broadcast_json_async(room, {"type": "state", "room": room, "state": GAMES[room]})

        if len(ROOMS[room]) == 0:
            del ROOMS[room]
            del GAMES[room]


# web client
app.mount("/", StaticFiles(directory="static", html=True), name="static")

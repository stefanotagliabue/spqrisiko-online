"""Gestori dei comandi di gioco ricevuti via WebSocket.

Ogni handler ha firma (room, player_id, payload) e ritorna:
- una stringa di errore da inviare al solo mittente, oppure
- None in caso di successo: il chiamante fa il broadcast dello stato.
"""
from typing import Any, Dict, Optional

from .state import (
    GAMES, PHASES, MAX_PLAYERS, NO_ELIMINATION_ROUNDS,
    add_log, assign_colors, get_player_by_id, init_neutrals,
    new_game_state, now_ms,
)
from .rules import (
    build_deck, calc_land_reinforcements, can_place_power_center,
    compute_score_awards, find_rebalance_move, garrison_deficits,
    has_sea_superiority, is_last_province_protected, min_garrison,
    normalize_prov_id, remove_triremes, reset_turn_tracking,
    resolve_naval_roll, resolve_risk_roll, roll_dice, tri_count,
)
from .engine import check_elimination, finish_turn

Handler = Any  # Callable[[str, str, dict], Optional[str]]


def handle_ready(room: str, player_id: str, payload: dict) -> Optional[str]:
    for p in GAMES[room]["players"]:
        if p["id"] == player_id:
            p["ready"] = not p["ready"]
            add_log(room, f"{p['name']} ready={p['ready']}")
            break
    return None


def handle_reset_game(room: str, player_id: str, payload: dict) -> Optional[str]:
    # mantiene i giocatori connessi, azzera lo stato di partita
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
    return None


def handle_start_game(room: str, player_id: str, payload: dict) -> Optional[str]:
    gs = GAMES[room]
    if gs["turn"]["phase"] != "LOBBY":
        return "Game already started (phase != LOBBY). Use Reset."
    if len(gs["players"]) < 3:
        return "Min 3 players"
    if len(gs["players"]) > MAX_PLAYERS:
        return f"Max {MAX_PLAYERS} players (§2.6)"
    if not all(p["ready"] for p in gs["players"]):
        return "All players must be ready"

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
    return None


def handle_setup_claim(room: str, player_id: str, payload: dict) -> Optional[str]:
    gs = GAMES[room]
    if gs["turn"]["phase"] != "SETUP":
        return "Not in SETUP"

    # only current player can claim
    if len(gs["players"]) == 0:
        return "No players in room"

    turn_player = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player != player_id:
        return "Not your turn"

    province_id = normalize_prov_id(payload.get("provinceId"))
    if not province_id:
        return "provinceId is required"

    if province_id not in gs["map"]["provinces"]:
        return f"Invalid provinceId: {province_id}"

    prov = gs["map"]["provinces"][province_id]
    if prov.get("owner") is not None:
        return "Province already owned"

    # claim for player: 2 legions
    p = next(x for x in gs["players"] if x["id"] == player_id)
    prov["owner"] = p["color"]
    prov["legions"] = 2
    gs["setup"]["claimedByPlayers"] += 1
    add_log(room, f"{p['name']} claimed {province_id} (2)")

    # neutral placement mandatory while pool not empty
    if len(gs["setup"]["neutralPool"]) > 0:
        neutral_prov_id = normalize_prov_id(payload.get("neutralProvinceId"))

        if not neutral_prov_id:
            return "neutralProvinceId is required in this turn"

        if neutral_prov_id not in gs["map"]["provinces"]:
            return f"Invalid neutralProvinceId: {neutral_prov_id}"

        nprov = gs["map"]["provinces"][neutral_prov_id]
        if nprov.get("owner") is not None:
            return "Neutral province already owned"

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

    return None


def handle_reinforce_land_begin(room: str, player_id: str, payload: dict) -> Optional[str]:
    gs = GAMES[room]
    if gs["turn"]["phase"] != "SCORE":
        return "Not in SCORE"

    # solo player di turno
    if len(gs["players"]) == 0:
        return "No players"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    if not p or not p.get("color"):
        return "Player/color not initialized"

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
        return None

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

    return None


def handle_reinforce_land_place(room: str, player_id: str, payload: dict) -> Optional[str]:
    gs = GAMES[room]
    if gs["turn"]["phase"] != "REINFORCE_LAND":
        return "Not in REINFORCE_LAND"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    if not p or not p.get("color"):
        return "Player/color not initialized"

    remaining = int(gs.get("pending", {}).get("landReinforceRemaining", 0))
    if remaining <= 0:
        return "No reinforcements remaining (call reinforce_land_begin?)"

    placements = payload.get("placements")
    if not isinstance(placements, dict) or len(placements) == 0:
        return "placements must be a non-empty object"

    # valida somme + ownership
    total = 0
    for raw_prov_id, raw_count in placements.items():
        prov_id = str(raw_prov_id).strip().upper()
        try:
            cnt = int(raw_count)
        except Exception:
            return f"Invalid count for {prov_id}"
        if cnt <= 0:
            return f"Count must be > 0 for {prov_id}"
        if prov_id not in gs["map"]["provinces"]:
            return f"Invalid provinceId: {prov_id}"
        prov = gs["map"]["provinces"][prov_id]
        if prov.get("owner") != p["color"]:
            return f"You do not own {prov_id}"
        total += cnt

    if total != remaining:
        return f"Placements sum {total} != remaining {remaining}"

    # Regola casa anti-farming ("garrison-first"): finché ci sono
    # province di frontiera a 1 legione, i rinforzi devono prima
    # riportarle a 2. I neutrali contano come nemici; vale anche
    # per i rinforzi extra da tris, che si sommano prima del
    # piazzamento unico.
    norm = {str(k).strip().upper(): int(v) for k, v in placements.items()}
    deficit = garrison_deficits(gs, p["color"])
    if deficit:
        if remaining >= len(deficit):
            missing = [pid for pid in deficit if norm.get(pid, 0) < 1]
            if missing:
                return f"Garrison-first: border provinces at 1 legion must be reinforced first: {', '.join(missing)}"
        else:
            bad = sorted(pid for pid, cnt in norm.items() if pid not in deficit or cnt > 1)
            if bad:
                return f"Garrison-first: only {remaining} reinforcements for {len(deficit)} border provinces at 1 legion; place 1 each on them only ({', '.join(deficit)})"

    # applica
    for raw_prov_id, raw_count in placements.items():
        prov_id = str(raw_prov_id).strip().upper()
        gs["map"]["provinces"][prov_id]["legions"] += int(raw_count)

    gs["pending"]["landReinforceRemaining"] = 0

    gs["turn"]["phase"] = "REINFORCE_NAVAL"
    add_log(room, f"{p['name']} placed +{total} legions. Phase -> REINFORCE_NAVAL")
    return None


def handle_play_tris(room: str, player_id: str, payload: dict) -> Optional[str]:
    """§8: scambia 3 carte per rinforzi supplementari."""
    gs = GAMES[room]
    if gs["turn"]["phase"] != "REINFORCE_LAND":
        return "Tris can only be played in REINFORCE_LAND (§8.2)"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)

    if gs["turn"].get("trisPlayed"):
        return "Already played a tris this turn (§4.4.1)"

    idxs = payload.get("cards")
    if (not isinstance(idxs, list) or len(idxs) != 3
            or len(set(idxs)) != 3
            or not all(isinstance(i, int) for i in idxs)
            or not all(0 <= i < len(p["cards"]) for i in idxs)):
        return f"cards must be 3 distinct indexes 0..{len(p['cards']) - 1}"

    cards = [p["cards"][i] for i in idxs]
    symbols = [c["symbol"] for c in cards]

    # §8.4-8.6: 3 uguali = 8 legioni, 3 diverse = 10
    if len(set(symbols)) == 1:
        base = 8
    elif len(set(symbols)) == 3:
        base = 10
    else:
        return "Invalid tris: need 3 equal or 3 different symbols (§8.4)"

    # §8.7.1: +2 legioni per ogni vessillo
    bonus_legions = 2 * symbols.count("VESSILLO")

    # §8.7.2: +1 trireme per ogni carta trireme, in un mare adiacente
    # a una propria provincia (beneficio facoltativo, §8.8)
    tri_cards = symbols.count("TRIREME")
    tri_seas_raw = payload.get("triremeSeas") or []
    if not isinstance(tri_seas_raw, list) or len(tri_seas_raw) > tri_cards:
        return f"triremeSeas must be a list of at most {tri_cards} seas"

    tri_seas = []
    for raw in tri_seas_raw:
        sid = normalize_prov_id(raw)
        if sid not in gs["map"]["seas"]:
            return f"Invalid seaId: {sid}"
        sea = gs["map"]["seas"][sid]
        touches_mine = any(
            gs["map"]["provinces"][pid].get("owner") == p["color"]
            for pid in sea.get("adj_land", [])
        )
        if not touches_mine:
            return f"{sid} is not adjacent to any of your provinces (§8.7.2)"
        tri_seas.append(sid)

    # §8.7.3: centro di potere se il tris contiene un'arena (facoltativo)
    pc_prov = normalize_prov_id(payload.get("powerCenterProvince"))
    if pc_prov:
        if "ARENA" not in symbols:
            return "No arena card in this tris (§8.7.3)"
        ok, reason = can_place_power_center(gs, p["color"], pc_prov)
        if not ok:
            return reason

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
    return None


def handle_trireme_to_legions(room: str, player_id: str, payload: dict) -> Optional[str]:
    """§6.6: durante i rinforzi, una trireme può essere convertita in
    2 legioni in una provincia adiacente al mare. Concesso in entrambe le
    fasi di rinforzo: la UI le presenta come un'unica fase."""
    gs = GAMES[room]
    if gs["turn"]["phase"] not in ("REINFORCE_LAND", "REINFORCE_NAVAL"):
        return "Not in a reinforcement phase"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    sea_id = normalize_prov_id(payload.get("seaId"))
    prov_id = normalize_prov_id(payload.get("provinceId"))

    if sea_id not in gs["map"]["seas"]:
        return f"Invalid seaId: {sea_id}"
    if prov_id not in gs["map"]["provinces"]:
        return f"Invalid provinceId: {prov_id}"

    sea = gs["map"]["seas"][sea_id]
    prov = gs["map"]["provinces"][prov_id]

    if tri_count(sea, p["color"]) < 1:
        return f"No trireme in {sea_id}"
    if prov.get("owner") != p["color"]:
        return f"You do not own {prov_id}"
    if sea_id not in prov.get("adj_sea", []):
        return f"{prov_id} is not adjacent to {sea_id}"

    remove_triremes(sea, p["color"], 1)
    prov["legions"] += 2
    add_log(room, f"{p['name']} trireme in {sea_id} -> +2 legions in {prov_id}")
    return None


def handle_buy_trireme(room: str, player_id: str, payload: dict) -> Optional[str]:
    """§7.1-7.3: 3 legioni da una provincia costiera -> 1 trireme.
    Concesso in entrambe le fasi di rinforzo: la UI le presenta come
    un'unica fase."""
    gs = GAMES[room]
    if gs["turn"]["phase"] not in ("REINFORCE_LAND", "REINFORCE_NAVAL"):
        return "Not in a reinforcement phase"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    prov_id = normalize_prov_id(payload.get("provinceId"))
    sea_id = normalize_prov_id(payload.get("seaId"))

    if prov_id not in gs["map"]["provinces"]:
        return f"Invalid provinceId: {prov_id}"
    if sea_id not in gs["map"]["seas"]:
        return f"Invalid seaId: {sea_id}"

    prov = gs["map"]["provinces"][prov_id]
    sea = gs["map"]["seas"][sea_id]

    if prov.get("owner") != p["color"]:
        return f"You do not own {prov_id}"
    if sea_id not in prov.get("adj_sea", []):
        return f"{prov_id} is not adjacent to {sea_id}"
    # 3 legioni da convertire + 1 di guarnigione (§14.1)
    if int(prov.get("legions", 0)) < 4:
        return "Need at least 4 legions (3 to convert + 1 garrison)"

    prov["legions"] -= 3
    sea.setdefault("triremes", {})
    sea["triremes"][p["color"]] = sea["triremes"].get(p["color"], 0) + 1
    add_log(room, f"{p['name']} -3 legions in {prov_id} -> +1 trireme in {sea_id}")
    return None


def handle_end_phase(room: str, player_id: str, payload: dict) -> Optional[str]:
    """Avanza fra le fasi navali (tutte facoltative)."""
    gs = GAMES[room]
    NEXT_PHASE = {
        "REINFORCE_NAVAL": "NAVAL_MOVE",
        "NAVAL_MOVE": "NAVAL_COMBAT",
        "NAVAL_COMBAT": "SEA_ATTACKS",
        "SEA_ATTACKS": "LAND_ATTACKS",
    }
    current = gs["turn"]["phase"]
    if current not in NEXT_PHASE:
        return f"Cannot end phase from {current}"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    gs["turn"]["phase"] = NEXT_PHASE[current]
    add_log(room, f"Phase -> {gs['turn']['phase']}")
    return None


def handle_naval_move(room: str, player_id: str, payload: dict) -> Optional[str]:
    """§9.3-9.4: un solo movimento di triremi fra mari adiacenti."""
    gs = GAMES[room]
    if gs["turn"]["phase"] != "NAVAL_MOVE":
        return "Not in NAVAL_MOVE"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    from_sea_id = normalize_prov_id(payload.get("fromSea"))
    to_sea_id = normalize_prov_id(payload.get("toSea"))
    try:
        count = int(payload.get("count", 0))
    except Exception:
        count = 0

    if from_sea_id not in gs["map"]["seas"] or to_sea_id not in gs["map"]["seas"]:
        return "Invalid sea id"
    if from_sea_id == to_sea_id:
        return "fromSea and toSea must be different"

    from_sea = gs["map"]["seas"][from_sea_id]
    to_sea = gs["map"]["seas"][to_sea_id]

    if to_sea_id not in from_sea.get("adj_sea", []):
        return f"{from_sea_id} is not adjacent to {to_sea_id}"
    if count < 1:
        return "count must be >= 1"
    if tri_count(from_sea, p["color"]) < count:
        return f"Only {tri_count(from_sea, p['color'])} triremes in {from_sea_id}"

    remove_triremes(from_sea, p["color"], count)
    to_sea.setdefault("triremes", {})
    to_sea["triremes"][p["color"]] = to_sea["triremes"].get(p["color"], 0) + count
    add_log(room, f"{p['name']} NAVAL MOVE {from_sea_id}->{to_sea_id} ({count})")

    # §9.4: un solo movimento per turno -> avanti
    gs["turn"]["phase"] = "NAVAL_COMBAT"
    return None


def handle_naval_attack_roll(room: str, player_id: str, payload: dict) -> Optional[str]:
    """§11: combattimento fra triremi nella stessa area di mare."""
    gs = GAMES[room]
    if gs["turn"]["phase"] != "NAVAL_COMBAT":
        return "Not in NAVAL_COMBAT"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    sea_id = normalize_prov_id(payload.get("seaId"))
    target = normalize_prov_id(payload.get("targetColor"))
    try:
        requested_dice = int(payload.get("attackDice", 0))
    except Exception:
        requested_dice = 0

    if sea_id not in gs["map"]["seas"]:
        return f"Invalid seaId: {sea_id}"
    if not target or target == p["color"]:
        return "targetColor must be another player"

    sea = gs["map"]["seas"][sea_id]
    mine = tri_count(sea, p["color"])
    theirs = tri_count(sea, target)

    if mine < 1:
        return f"You have no triremes in {sea_id}"
    if theirs < 1:
        return f"{target} has no triremes in {sea_id}"

    # §11.4: un solo combattimento per area di mare a turno
    combat = gs["turn"]["navalCombats"].get(sea_id)
    if combat and combat.get("closed"):
        return f"Combat in {sea_id} already closed this turn"
    if combat and combat.get("target") != target:
        return f"Combat in {sea_id} already declared vs {combat['target']}"

    def_dice = min(3, theirs)  # §10.5: il difensore usa sempre il massimo
    max_att_dice = min(3, mine)
    if requested_dice <= 0:
        requested_dice = max_att_dice
    att_dice = min(max_att_dice, requested_dice)

    if att_dice < def_dice:
        return f"Cannot attack with fewer dice than defender (att={att_dice} def={def_dice})"

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

    gs["lastCombat"] = {
        "kind": "naval",
        "attacker": p["color"],
        "defender": target,
        "sea": sea_id,
        "rolls": [{
            "att": sorted(att_roll, reverse=True),
            "def": sorted(def_roll, reverse=True),
            "attLoss": a_loss,
            "defLoss": d_loss,
        }],
        "conquered": False,
        "t": now_ms(),
    }
    return None


def handle_sea_attack(room: str, player_id: str, payload: dict) -> Optional[str]:
    """§12: attacco fra province adiacenti allo stesso mare,
    combattimento ad oltranza con forza dichiarata in anticipo."""
    gs = GAMES[room]
    if gs["turn"]["phase"] != "SEA_ATTACKS":
        return "Not in SEA_ATTACKS"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    from_id = normalize_prov_id(payload.get("from"))
    to_id = normalize_prov_id(payload.get("to"))
    sea_id = normalize_prov_id(payload.get("seaId"))
    try:
        legions = int(payload.get("legions", 0))
    except Exception:
        legions = 0

    if from_id not in gs["map"]["provinces"] or to_id not in gs["map"]["provinces"]:
        return "Invalid province id"
    if sea_id not in gs["map"]["seas"]:
        return f"Invalid seaId: {sea_id}"

    prov_from = gs["map"]["provinces"][from_id]
    prov_to = gs["map"]["provinces"][to_id]
    sea = gs["map"]["seas"][sea_id]

    if prov_from.get("owner") != p["color"]:
        return f"You do not own {from_id}"
    if prov_to.get("owner") == p["color"]:
        return f"Target {to_id} is already yours"
    if sea_id not in prov_from.get("adj_sea", []) or sea_id not in prov_to.get("adj_sea", []):
        return f"Both provinces must border {sea_id}"

    # §12.7: da una provincia conquistata via mare non si ri-attacca via mare
    if from_id in gs["turn"]["seaConqueredProvinces"]:
        return f"{from_id} was conquered by sea this turn: no new sea attack from it"
    # §12.6: la stessa provincia non può subire due attacchi via mare nel turno
    if to_id in gs["turn"]["seaAttackedProvinces"]:
        return f"{to_id} was already attacked by sea this turn"

    # §12.2: servono strettamente più triremi del difensore in quel mare
    defender_color = prov_to.get("owner")
    my_tri = tri_count(sea, p["color"])
    def_tri = 0
    if defender_color and not str(defender_color).startswith("NEUTRAL_"):
        def_tri = tri_count(sea, defender_color)
    if my_tri < 1 or my_tri <= def_tri:
        return f"Need more triremes than defender in {sea_id} (you={my_tri} def={def_tri})"

    # §14.3: la guarnigione minima limita anche l'attacco via mare
    garrison = min_garrison(gs, from_id, p["color"])
    available = int(prov_from.get("legions", 0)) - garrison
    if legions < 1 or legions > available:
        return f"legions must be 1..{max(available, 0)} (garrison {garrison}, §14.3)"

    def_legions = int(prov_to.get("legions", 0))
    if def_legions <= 0:
        return "Target has no legions (invalid state)"

    # §18.4: niente eliminazioni prima della fine del 4° round
    if is_last_province_protected(gs, defender_color):
        return f"Cannot attack {defender_color}'s last province before the end of round {NO_ELIMINATION_ROUNDS} (§18.4)"

    # §12.3: forza dichiarata, combattimento ad oltranza
    prov_from["legions"] -= legions
    att_force = legions
    rolls = 0
    roll_history = []
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
        roll_history.append({
            "att": sorted(att_roll, reverse=True),
            "def": sorted(def_roll, reverse=True),
            "attLoss": a_loss,
            "defLoss": d_loss,
        })
        add_log(room, f"  roll A{att_roll} D{def_roll} | losses A-{a_loss} D-{d_loss} -> {max(att_force,0)} vs {max(def_legions,0)}")

    gs["turn"]["seaAttackedProvinces"].append(to_id)
    gs["lastCombat"] = {
        "kind": "sea",
        "attacker": p["color"],
        "defender": defender_color,
        "from": from_id,
        "to": to_id,
        "sea": sea_id,
        "rolls": roll_history,
        "conquered": False,
        "t": now_ms(),
    }

    if def_legions <= 0 and att_force > 0:
        prev_owner = prov_to.get("owner")
        prov_to["owner"] = p["color"]
        prov_to["legions"] = att_force
        gs["turn"]["conqueredThisTurn"] = True
        gs["turn"]["seaConqueredProvinces"].append(to_id)
        gs["lastCombat"]["conquered"] = True
        add_log(room, f"CONQUERED {to_id} by sea ({att_force} legions landed)")

        if prev_owner and not str(prev_owner).startswith("NEUTRAL_"):
            check_elimination(room, prev_owner, p)
    else:
        prov_to["legions"] = max(1, def_legions)
        add_log(room, f"SEA ATTACK failed: attacking force destroyed, {to_id} holds")

    return None


def handle_land_attack_roll(room: str, player_id: str, payload: dict) -> Optional[str]:
    """Attacchi terrestri: un lancio per comando (dadi lato server)."""
    gs = GAMES[room]
    if gs["turn"]["phase"] != "LAND_ATTACKS":
        return "Not in LAND_ATTACKS"

    if len(gs["players"]) == 0:
        return "No players"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    if not p or not p.get("color"):
        return "Player/color not initialized"

    from_id = normalize_prov_id(payload.get("from"))
    to_id = normalize_prov_id(payload.get("to"))
    try:
        requested_dice = int(payload.get("attackDice", 0))
    except Exception:
        requested_dice = 0

    if not from_id or not to_id:
        return "from and to are required"
    if from_id == to_id:
        return "from and to must be different"
    if from_id not in gs["map"]["provinces"] or to_id not in gs["map"]["provinces"]:
        return "Invalid province id"

    prov_from = gs["map"]["provinces"][from_id]
    prov_to = gs["map"]["provinces"][to_id]

    if prov_from.get("owner") != p["color"]:
        return f"You do not own {from_id}"
    if prov_to.get("owner") == p["color"]:
        return f"Target {to_id} is already yours"

    # adjacency (land only)
    if to_id not in prov_from.get("adj_land", []):
        return f"{from_id} is not adjacent by land to {to_id}"

    from_leg = int(prov_from.get("legions", 0))
    to_leg = int(prov_to.get("legions", 0))

    if from_leg <= 1:
        return "Not enough legions to attack (need >= 2)"
    if to_leg <= 0:
        return "Target has no legions (invalid state)"

    # §18.4: niente eliminazioni prima della fine del 4° round
    if is_last_province_protected(gs, prov_to.get("owner")):
        return f"Cannot attack {prov_to.get('owner')}'s last province before the end of round {NO_ELIMINATION_ROUNDS} (§18.4)"

    # dice counts
    def_dice = min(3, to_leg)  # defender must roll max
    max_att_dice = min(3, from_leg - 1)
    if requested_dice <= 0:
        requested_dice = max_att_dice
    att_dice = min(max_att_dice, requested_dice)

    # rule: cannot attack with fewer dice than defender
    if att_dice < def_dice:
        return f"Cannot attack with fewer dice than defender (att={att_dice} def={def_dice})"

    # un nuovo attacco chiude la finestra di occupazione precedente
    gs["pending"]["occupation"] = None

    att_roll = roll_dice(att_dice)
    def_roll = roll_dice(def_dice)
    a_loss, d_loss = resolve_risk_roll(att_roll, def_roll)

    # apply losses
    prov_from["legions"] = max(0, from_leg - a_loss)
    prov_to["legions"] = max(0, to_leg - d_loss)

    add_log(room, f"LAND ATTACK {p['name']} {from_id}->{to_id} A{sorted(att_roll, reverse=True)} D{sorted(def_roll, reverse=True)} | losses A-{a_loss} D-{d_loss}")

    gs["lastCombat"] = {
        "kind": "land",
        "attacker": p["color"],
        "defender": prov_to.get("owner"),
        "from": from_id,
        "to": to_id,
        "rolls": [{
            "att": sorted(att_roll, reverse=True),
            "def": sorted(def_roll, reverse=True),
            "attLoss": a_loss,
            "defLoss": d_loss,
        }],
        "conquered": False,
        "t": now_ms(),
    }

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
        gs["lastCombat"]["conquered"] = True
        # §13.4: finché non fa altro, può spostare legioni extra
        gs["pending"]["occupation"] = {"from": from_id, "to": to_id}
        add_log(room, f"CONQUERED {to_id} by {p['name']} (moved {move_min})")

        # §18: eliminazione se il difensore era un giocatore
        if prev_owner and not str(prev_owner).startswith("NEUTRAL_"):
            check_elimination(room, prev_owner, p)

    return None


def handle_occupy_extra(room: str, player_id: str, payload: dict) -> Optional[str]:
    """§13.4: sposta legioni aggiuntive nella provincia appena conquistata."""
    gs = GAMES[room]
    if gs["turn"]["phase"] != "LAND_ATTACKS":
        return "Not in LAND_ATTACKS"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    occ = gs["pending"].get("occupation")
    if not occ:
        return "No pending occupation (conquer first)"

    try:
        count = int(payload.get("count", 0))
    except Exception:
        count = 0
    if count < 1:
        return "count must be >= 1"

    p = get_player_by_id(gs, player_id)
    prov_from = gs["map"]["provinces"][occ["from"]]
    prov_to = gs["map"]["provinces"][occ["to"]]
    # §14.2: spostamento volontario -> guarnigione minima (2 se confina col nemico)
    garrison = min_garrison(gs, occ["from"], p["color"])
    available = int(prov_from.get("legions", 0)) - garrison
    if count > available:
        return f"Only {max(available, 0)} legions available to move (garrison {garrison}, §14.2)"

    prov_from["legions"] -= count
    prov_to["legions"] += count
    gs["pending"]["occupation"] = None

    add_log(room, f"{p['name']} moved +{count} into {occ['to']}")
    return None


def handle_end_attacks(room: str, player_id: str, payload: dict) -> Optional[str]:
    """Chiude la fase di attacchi terrestri."""
    gs = GAMES[room]
    if gs["turn"]["phase"] != "LAND_ATTACKS":
        return "Not in LAND_ATTACKS"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    gs["pending"]["occupation"] = None
    gs["turn"]["phase"] = "STRATEGIC_MOVE"
    p = get_player_by_id(gs, player_id)
    add_log(room, f"{p['name']} ends attacks -> STRATEGIC_MOVE")
    return None


def handle_strategic_move(room: str, player_id: str, payload: dict) -> Optional[str]:
    """§15: singolo spostamento fra province proprie adiacenti,
    poi il turno termina (§15.6)."""
    gs = GAMES[room]
    if gs["turn"]["phase"] != "STRATEGIC_MOVE":
        return "Not in STRATEGIC_MOVE"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)
    if not p or not p.get("color"):
        return "Player/color not initialized"

    from_id = normalize_prov_id(payload.get("from"))
    to_id = normalize_prov_id(payload.get("to"))
    try:
        count = int(payload.get("count", 0))
    except Exception:
        count = 0

    if not from_id or not to_id or from_id == to_id:
        return "from and to must be different provinces"
    if from_id not in gs["map"]["provinces"] or to_id not in gs["map"]["provinces"]:
        return "Invalid province id"

    prov_from = gs["map"]["provinces"][from_id]
    prov_to = gs["map"]["provinces"][to_id]

    if prov_from.get("owner") != p["color"] or prov_to.get("owner") != p["color"]:
        return "You must own both provinces"
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
        return f"No route {from_id}->{to_id} (land adjacency or sea with naval superiority required)"
    if count < 1:
        return "count must be >= 1"
    # §14.2: guarnigione minima dopo spostamento volontario
    garrison = min_garrison(gs, from_id, p["color"])
    if count > int(prov_from.get("legions", 0)) - garrison:
        return f"Must leave at least {garrison} legions in {from_id} (§14.2)"

    # Regola casa "garrison-first": se esiste una mossa che sana una
    # frontiera a 1, lo spostamento deve ridurre le carenze
    deficits = garrison_deficits(gs, p["color"])
    if deficits and to_id not in deficits:
        rb = find_rebalance_move(gs, p["color"], deficits)
        if rb:
            return f"Garrison-first: the strategic move must reinforce a border province at 1 legion ({', '.join(deficits)}), e.g. {rb['from']} -> {rb['to']}"

    prov_from["legions"] -= count
    prov_to["legions"] += count
    gs["turn"]["usedStrategicMove"] = True
    add_log(room, f"{p['name']} STRATEGIC MOVE {from_id}->{to_id} ({count})")

    # §15.6: lo spostamento strategico chiude il turno
    finish_turn(room)
    return None


def handle_end_turn(room: str, player_id: str, payload: dict) -> Optional[str]:
    """Chiude il turno senza (o dopo) lo spostamento strategico."""
    gs = GAMES[room]
    if gs["turn"]["phase"] not in ("LAND_ATTACKS", "STRATEGIC_MOVE"):
        return "Cannot end turn in this phase"

    turn_player_id = gs["players"][gs["turn"]["turnIndex"]]["id"]
    if turn_player_id != player_id:
        return "Not your turn"

    p = get_player_by_id(gs, player_id)

    # Regola casa "garrison-first": non si chiude il turno se una
    # frontiera a 1 legione può ancora essere sanata con lo
    # spostamento strategico
    deficits = garrison_deficits(gs, p["color"])
    rb = find_rebalance_move(gs, p["color"], deficits) if deficits else None
    if rb:
        return f"Garrison-first: border provinces at 1 legion ({', '.join(deficits)}): use the strategic move to rebalance, e.g. {rb['from']} -> {rb['to']}"

    add_log(room, f"{p['name']} ends turn")
    finish_turn(room)
    return None


def handle_advance_phase(room: str, player_id: str, payload: dict) -> Optional[str]:
    """DEBUG: avanza la fase manualmente."""
    gs = GAMES[room]
    turn = gs["turn"]
    current = turn["phase"]

    if current not in PHASES:
        turn["phase"] = "LOBBY"
        add_log(room, "Phase was invalid -> reset to LOBBY")
        return None

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

    return None


HANDLERS: Dict[str, Handler] = {
    "ready": handle_ready,
    "reset_game": handle_reset_game,
    "start_game": handle_start_game,
    "setup_claim": handle_setup_claim,
    "reinforce_land_begin": handle_reinforce_land_begin,
    "reinforce_land_place": handle_reinforce_land_place,
    "play_tris": handle_play_tris,
    "trireme_to_legions": handle_trireme_to_legions,
    "buy_trireme": handle_buy_trireme,
    "end_phase": handle_end_phase,
    "naval_move": handle_naval_move,
    "naval_attack_roll": handle_naval_attack_roll,
    "sea_attack": handle_sea_attack,
    "land_attack_roll": handle_land_attack_roll,
    "occupy_extra": handle_occupy_extra,
    "end_attacks": handle_end_attacks,
    "strategic_move": handle_strategic_move,
    "end_turn": handle_end_turn,
    "advance_phase": handle_advance_phase,
}

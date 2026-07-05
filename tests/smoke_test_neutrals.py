"""
Smoke test Blocco H: numero e piazzamento dei neutrali.
Regola casa: tutti i neutrali usano un unico colore (NEUTRAL_COLOR),
le quantita' restano quelle del regolamento:
- 3 giocatori: 18 gruppi da 3 (54 legioni)
- 4 giocatori: 3 legioni fisse in ITALIA + 8 gruppi da 3
- a fine setup tutte le 45 province sono occupate (27 dei giocatori + 18 neutrali
  con 3 giocatori; 36 dei giocatori + 9 neutrali con 4)

Uso:  python tests/smoke_test_neutrals.py
"""
import sys
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import tempfile  # noqa: E402
os.environ["SPQR_DATA_DIR"] = tempfile.mkdtemp(prefix="spqr-rooms-")

from fastapi.testclient import TestClient  # noqa: E402
import server  # noqa: E402

random.seed(11)

client = TestClient(server.app)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


class Player:
    def __init__(self, ws, name):
        self.ws = ws
        self.name = name
        self.id = None
        self.color = None


def send_cmd(sender, all_players, cmd, payload=None):
    sender.ws.send_json({"type": "cmd", "cmd": cmd, "playerId": sender.id, "payload": payload or {}})
    msg = sender.ws.receive_json()
    if msg["type"] == "error":
        raise RuntimeError(f"[{sender.name}] {cmd} -> ERROR: {msg['error']}")
    for p in all_players:
        if p is not sender:
            p.ws.receive_json()
    return msg["state"]


def current_player(state, players):
    idx = state["turn"]["turnIndex"]
    pid = state["players"][idx]["id"]
    return next(p for p in players if p.id == pid)


def free_provinces(state):
    return sorted(pid for pid, pr in state["map"]["provinces"].items() if pr["owner"] is None)


def open_players(room, names):
    """Connette i giocatori e drena i messaggi di join/state iniziali."""
    players = []
    for i, name in enumerate(names):
        ws = client.websocket_connect(f"/ws/{room}/{name}").__enter__()
        p = Player(ws, name)
        m = ws.receive_json()
        p.id = m["playerId"]
        players.append(p)
        # join+state per ogni client già connesso (incluso il nuovo)
        for q in players:
            q.ws.receive_json()
            q.ws.receive_json()
    return players


def neutral_provinces(state):
    return [pr for pr in state["map"]["provinces"].values()
            if pr.get("owner") and pr["owner"].startswith("NEUTRAL_")]


def run_setup(state, players):
    """Completa tutto il setup piazzando su province libere."""
    while state["turn"]["phase"] == "SETUP":
        actor = current_player(state, players)
        free = free_provinces(state)
        payload = {"provinceId": free[0]}
        if len(state["setup"]["neutralPool"]) > 0:
            payload["neutralProvinceId"] = free[1]
        state = send_cmd(actor, players, "setup_claim", payload)
    return state


NEUTRAL_OWNER = f"NEUTRAL_{server.NEUTRAL_COLOR}"


def test_3_players():
    print("== 3 GIOCATORI: 18 GRUPPI DA 3, COLORE UNICO ==")
    room = "TESTN3"
    players = open_players(room, ["Anna", "Bruno", "Carla"])
    for p in players:
        send_cmd(p, players, "ready")
    state = send_cmd(players[0], players, "start_game", {"targetScore": 999})

    pool = state["setup"]["neutralPool"]
    check("neutral pool has 18 groups", len(pool) == 18, f"got {len(pool)}")
    check("all groups size 3, single color",
          all(g["size"] == 3 and g["color"] == server.NEUTRAL_COLOR for g in pool))
    check("neutral color is not a player color",
          server.NEUTRAL_COLOR not in {pl["color"] for pl in state["players"]})
    check("no fixed neutrals with 3 players", state["setup"]["neutralFixed"] == [])

    state = run_setup(state, players)
    check("setup complete -> SCORE", state["turn"]["phase"] == "SCORE")
    check("no free provinces left", len(free_provinces(state)) == 0)

    neutrals = neutral_provinces(state)
    check("18 neutral provinces on map", len(neutrals) == 18, f"got {len(neutrals)}")
    check("all neutral provinces share the owner",
          all(pr["owner"] == NEUTRAL_OWNER for pr in neutrals))
    check("54 neutral legions total", sum(pr["legions"] for pr in neutrals) == 54)

    used = {pl["color"] for pl in state["players"]}
    total_players = sum(1 for pr in state["map"]["provinces"].values() if pr.get("owner") in used)
    check("27 player + 18 neutral = 45 provinces",
          total_players == 27 and len(neutrals) == 18,
          f"players={total_players} neutrals={len(neutrals)}")

    for p in players:
        p.ws.__exit__(None, None, None)


def test_4_players():
    print("== 4 GIOCATORI: ITALIA + 8 GRUPPI DA 3, COLORE UNICO ==")
    room = "TESTN4"
    players = open_players(room, ["Anna", "Bruno", "Carla", "Dino"])
    for p in players:
        send_cmd(p, players, "ready")
    state = send_cmd(players[0], players, "start_game", {"targetScore": 999})

    check("ITALIA is fixed neutral with 3 legions",
          state["map"]["provinces"]["ITALIA"]["owner"] == NEUTRAL_OWNER
          and state["map"]["provinces"]["ITALIA"]["legions"] == 3)

    pool = state["setup"]["neutralPool"]
    check("neutral pool has 8 groups", len(pool) == 8, f"got {len(pool)}")
    check("all groups size 3, single color",
          all(g["size"] == 3 and g["color"] == server.NEUTRAL_COLOR for g in pool))

    state = run_setup(state, players)
    check("setup complete -> SCORE", state["turn"]["phase"] == "SCORE")
    check("no free provinces left", len(free_provinces(state)) == 0)

    neutrals = neutral_provinces(state)
    check("9 neutral provinces (ITALIA + 8)", len(neutrals) == 9, f"got {len(neutrals)}")
    check("27 neutral legions total", sum(pr["legions"] for pr in neutrals) == 27)

    used = {pl["color"] for pl in state["players"]}
    total_players = sum(1 for pr in state["map"]["provinces"].values() if pr.get("owner") in used)
    check("36 player + 9 neutral = 45 provinces",
          total_players == 36 and len(neutrals) == 9,
          f"players={total_players} neutrals={len(neutrals)}")

    for p in players:
        p.ws.__exit__(None, None, None)


def main():
    test_3_players()
    test_4_players()
    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

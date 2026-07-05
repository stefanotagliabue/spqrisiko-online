"""
Smoke test Blocco I: fase rinforzi unificata (UI mobile).
buy_trireme e trireme_to_legions sono concessi in entrambe le fasi di
rinforzo (REINFORCE_LAND e REINFORCE_NAVAL), perche' la UI le presenta
come un'unica schermata.

Uso:  python tests/smoke_test_unified_reinforce.py
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

random.seed(21)

client = TestClient(server.app)
ROOM = "TESTUR"

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


def send_cmd(sender, all_players, cmd, payload=None):
    sender.ws.send_json({"type": "cmd", "cmd": cmd, "playerId": sender.id, "payload": payload or {}})
    msg = sender.ws.receive_json()
    if msg["type"] == "error":
        raise RuntimeError(f"[{sender.name}] {cmd} -> ERROR: {msg['error']}")
    for p in all_players:
        if p is not sender:
            p.ws.receive_json()
    return msg["state"]


def expect_error(sender, cmd, payload, contains, name):
    sender.ws.send_json({"type": "cmd", "cmd": cmd, "playerId": sender.id, "payload": payload or {}})
    msg = sender.ws.receive_json()
    ok = msg["type"] == "error" and contains.lower() in msg.get("error", "").lower()
    check(name, ok, f"got {msg}")


def current_player(state, players):
    idx = state["turn"]["turnIndex"]
    pid = state["players"][idx]["id"]
    return next(p for p in players if p.id == pid)


def free_provinces(state):
    return sorted(pid for pid, pr in state["map"]["provinces"].items() if pr["owner"] is None)


def open_players(names):
    players = []
    for name in names:
        ws = client.websocket_connect(f"/ws/{ROOM}/{name}").__enter__()
        p = Player(ws, name)
        m = ws.receive_json()
        p.id = m["playerId"]
        players.append(p)
        for q in players:
            q.ws.receive_json()
            q.ws.receive_json()
    return players


def main():
    players = open_players(["Anna", "Bruno", "Carla"])
    for p in players:
        send_cmd(p, players, "ready")
    state = send_cmd(players[0], players, "start_game", {"targetScore": 999})

    while state["turn"]["phase"] == "SETUP":
        actor = current_player(state, players)
        free = free_provinces(state)
        payload = {"provinceId": free[0]}
        if len(state["setup"]["neutralPool"]) > 0:
            payload["neutralProvinceId"] = free[1]
        state = send_cmd(actor, players, "setup_claim", payload)

    actor = current_player(state, players)
    color = next(pl["color"] for pl in state["players"] if pl["id"] == actor.id)
    state = send_cmd(actor, players, "reinforce_land_begin")
    check("phase REINFORCE_LAND", state["turn"]["phase"] == "REINFORCE_LAND")
    remaining = state["pending"]["landReinforceRemaining"]

    # provincia costiera dell'attore, gonfiata a 5 legioni per poter comprare
    provs = state["map"]["provinces"]
    coastal = next(pid for pid, pr in provs.items()
                   if pr["owner"] == color and pr["adj_sea"])
    sea_id = provs[coastal]["adj_sea"][0]
    server.GAMES[ROOM]["map"]["provinces"][coastal]["legions"] = 5

    print("== COMPRA TRIREME DURANTE I RINFORZI TERRESTRI ==")
    state = send_cmd(actor, players, "buy_trireme", {"provinceId": coastal, "seaId": sea_id})
    check("still in REINFORCE_LAND", state["turn"]["phase"] == "REINFORCE_LAND")
    check("trireme placed", state["map"]["seas"][sea_id]["triremes"].get(color) == 1)
    check("3 legions spent", state["map"]["provinces"][coastal]["legions"] == 2)

    state = send_cmd(actor, players, "reinforce_land_place", {"placements": {coastal: remaining}})
    check("phase REINFORCE_NAVAL after placement", state["turn"]["phase"] == "REINFORCE_NAVAL")

    print("== CONVERTI TRIREME DURANTE I RINFORZI NAVALI ==")
    legions_before = state["map"]["provinces"][coastal]["legions"]
    state = send_cmd(actor, players, "trireme_to_legions", {"seaId": sea_id, "provinceId": coastal})
    check("still in REINFORCE_NAVAL", state["turn"]["phase"] == "REINFORCE_NAVAL")
    check("+2 legions from conversion",
          state["map"]["provinces"][coastal]["legions"] == legions_before + 2)
    check("trireme removed", state["map"]["seas"][sea_id]["triremes"].get(color) is None)

    print("== FUORI DALLE FASI DI RINFORZO: RIFIUTATI ==")
    state = send_cmd(actor, players, "end_phase")
    check("phase NAVAL_MOVE", state["turn"]["phase"] == "NAVAL_MOVE")
    expect_error(actor, "buy_trireme", {"provinceId": coastal, "seaId": sea_id},
                 "reinforcement", "buy_trireme refused in NAVAL_MOVE")
    expect_error(actor, "trireme_to_legions", {"seaId": sea_id, "provinceId": coastal},
                 "reinforcement", "trireme_to_legions refused in NAVAL_MOVE")

    for p in players:
        p.ws.__exit__(None, None, None)

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

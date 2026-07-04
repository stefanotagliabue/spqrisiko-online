"""
Smoke test Blocco C: tris di carte (Â§8) e Centri di Potere (Â§16, Â§6.5).
- tris valido/invalido, 3 diverse (10) + vessillo (+2) + trireme piazzata
- tris di 3 uguali (8), un solo tris per turno
- piazzamento centro di potere: proprieta', adiacenza (Â§16.4), limite 12
- rinforzo extra +1 sul centro di potere (Â§6.5)
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

random.seed(99)

client = TestClient(server.app)
ROOM = "TEST03"

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


def owned_by(state, color):
    return sorted(pid for pid, pr in state["map"]["provinces"].items() if pr["owner"] == color)


def gs_live():
    return server.GAMES[ROOM]


def live_player(actor):
    return next(pl for pl in gs_live()["players"] if pl["id"] == actor.id)


def set_hand(actor, symbols):
    live_player(actor)["cards"][:] = [{"symbol": s} for s in symbols]


def quick_turn(actor, players, state):
    """Turno minimo: begin, piazza tutto, salta navale, fine turno."""
    state = send_cmd(actor, players, "reinforce_land_begin")
    rem = state["pending"]["landReinforceRemaining"]
    state = send_cmd(actor, players, "reinforce_land_place",
                     {"placements": {owned_by(state, actor.color)[0]: rem}})
    for _ in range(4):
        state = send_cmd(actor, players, "end_phase")
    return send_cmd(actor, players, "end_turn")


def main():
    with client.websocket_connect(f"/ws/{ROOM}/Anna") as ws1, \
         client.websocket_connect(f"/ws/{ROOM}/Bruno") as ws2, \
         client.websocket_connect(f"/ws/{ROOM}/Carla") as ws3:

        p1, p2, p3 = Player(ws1, "Anna"), Player(ws2, "Bruno"), Player(ws3, "Carla")
        players = [p1, p2, p3]

        m = ws1.receive_json(); p1.id = m["playerId"]
        for _ in range(6):
            ws1.receive_json()
        m = ws2.receive_json(); p2.id = m["playerId"]
        for _ in range(4):
            ws2.receive_json()
        m = ws3.receive_json(); p3.id = m["playerId"]
        for _ in range(2):
            ws3.receive_json()

        for p in players:
            send_cmd(p, players, "ready")
        state = send_cmd(p1, players, "start_game", {"targetScore": 999})
        for pl, obj in zip(state["players"], players):
            obj.color = pl["color"]

        while state["turn"]["phase"] == "SETUP":
            actor = current_player(state, players)
            free = free_provinces(state)
            payload = {"provinceId": free[0]}
            if len(state["setup"]["neutralPool"]) > 0:
                payload["neutralProvinceId"] = free[1]
            state = send_cmd(actor, players, "setup_claim", payload)

        print("== TURNO A: tris di 3 diverse + trireme + vessillo ==")
        actorA = current_player(state, players)
        state = send_cmd(actorA, players, "reinforce_land_begin")
        check("base reinforcements 3", state["pending"]["landReinforceRemaining"] == 3)

        # tris invalido: 2 uguali + 1 diversa
        set_hand(actorA, ["LEGIONARIO", "LEGIONARIO", "TRIREME"])
        expect_error(actorA, "play_tris", {"cards": [0, 1, 2]},
                     "invalid tris", "invalid tris refused (2+1)")

        # tris valido: 3 diverse con vessillo e trireme
        set_hand(actorA, ["LEGIONARIO", "TRIREME", "VESSILLO"])
        provs = state["map"]["provinces"]
        coastal = next(pid for pid in owned_by(state, actorA.color) if provs[pid]["adj_sea"])
        sea_a = provs[coastal]["adj_sea"][0]

        discard_before = len(state["discard"])
        state = send_cmd(actorA, players, "play_tris",
                         {"cards": [0, 1, 2], "triremeSeas": [sea_a]})
        # 3 base + 10 (3 diverse) + 2 (vessillo) = 15
        check("reinforcements 3+10+2=15", state["pending"]["landReinforceRemaining"] == 15,
              f"got {state['pending']['landReinforceRemaining']}")
        check("trireme from tris placed", state["map"]["seas"][sea_a]["triremes"].get(actorA.color) == 1)
        check("hand empty after tris",
              len(next(pl["cards"] for pl in state["players"] if pl["id"] == actorA.id)) == 0)
        check("cards to discard pile", len(state["discard"]) == discard_before + 3)

        # un solo tris per turno
        set_hand(actorA, ["ARENA", "ARENA", "ARENA"])
        expect_error(actorA, "play_tris", {"cards": [0, 1, 2]},
                     "already played", "one tris per turn (Â§4.4.1)")
        set_hand(actorA, [])

        state = send_cmd(actorA, players, "reinforce_land_place",
                         {"placements": {owned_by(state, actorA.color)[0]: 15}})
        for _ in range(4):
            state = send_cmd(actorA, players, "end_phase")
        state = send_cmd(actorA, players, "end_turn")

        print("== TURNO B: tris di 3 uguali + centro di potere ==")
        actorB = current_player(state, players)
        state = send_cmd(actorB, players, "reinforce_land_begin")

        set_hand(actorB, ["ARENA", "ARENA", "ARENA"])
        pc_prov_b = owned_by(state, actorB.color)[0]
        state = send_cmd(actorB, players, "play_tris",
                         {"cards": [0, 1, 2], "powerCenterProvince": pc_prov_b})
        check("reinforcements 3+8=11", state["pending"]["landReinforceRemaining"] == 11,
              f"got {state['pending']['landReinforceRemaining']}")
        check("power center placed", state["map"]["provinces"][pc_prov_b]["power_center"] is True)

        state = send_cmd(actorB, players, "reinforce_land_place",
                         {"placements": {pc_prov_b: 11}})
        legions_pc_b = state["map"]["provinces"][pc_prov_b]["legions"]
        for _ in range(4):
            state = send_cmd(actorB, players, "end_phase")
        state = send_cmd(actorB, players, "end_turn")

        print("== TURNO C: regole piazzamento centri (Â§16.4) ==")
        actorC = current_player(state, players)
        state = send_cmd(actorC, players, "reinforce_land_begin")
        set_hand(actorC, ["ARENA", "ARENA", "ARENA"])

        # non posso piazzare in provincia non mia
        expect_error(actorC, "play_tris",
                     {"cards": [0, 1, 2], "powerCenterProvince": pc_prov_b},
                     "do not own", "PC refused on non-owned province")

        # non adiacente a un altro centro (Â§16.4) - se serve, prestiamo a C
        # una provincia confinante col centro di B
        provs = state["map"]["provinces"]
        adj_owned = next((pid for pid in provs[pc_prov_b]["adj_land"]
                          if provs[pid]["owner"] == actorC.color), None)
        borrowed = None
        if not adj_owned:
            borrowed = provs[pc_prov_b]["adj_land"][0]
            prev_owner = gs_live()["map"]["provinces"][borrowed]["owner"]
            gs_live()["map"]["provinces"][borrowed]["owner"] = actorC.color
            adj_owned = borrowed
        expect_error(actorC, "play_tris",
                     {"cards": [0, 1, 2], "powerCenterProvince": adj_owned},
                     "adjacent", "PC refused next to another PC (Â§16.4)")
        if borrowed:
            gs_live()["map"]["provinces"][borrowed]["owner"] = prev_owner

        # limite 12 centri (Â§1.3): iniettiamo 11 centri fittizi lontani dal bersaglio
        target = next(pid for pid in owned_by(state, actorC.color)
                      if not provs[pid]["power_center"]
                      and not any(provs[nb]["power_center"] for nb in provs[pid]["adj_land"]))
        forbidden = {target, pc_prov_b} | set(provs[target]["adj_land"])
        fillers = [pid for pid in provs if pid not in forbidden and not provs[pid]["power_center"]][:11]
        live = gs_live()
        for pid in fillers:
            live["map"]["provinces"][pid]["power_center"] = True
        expect_error(actorC, "play_tris",
                     {"cards": [0, 1, 2], "powerCenterProvince": target},
                     "12 power centers", "PC refused when all 12 in play")
        for pid in fillers:
            live["map"]["provinces"][pid]["power_center"] = False

        # piazzamento valido
        state = send_cmd(actorC, players, "play_tris",
                         {"cards": [0, 1, 2], "powerCenterProvince": target})
        check("PC placed for C", state["map"]["provinces"][target]["power_center"] is True)

        rem = state["pending"]["landReinforceRemaining"]
        state = send_cmd(actorC, players, "reinforce_land_place",
                         {"placements": {owned_by(state, actorC.color)[0]: rem}})
        for _ in range(4):
            state = send_cmd(actorC, players, "end_phase")
        state = send_cmd(actorC, players, "end_turn")

        print("== ROUND 2: rinforzo +1 sul centro di potere (Â§6.5) ==")
        # turno di A (veloce), poi tocca a B che ha il centro
        state = quick_turn(current_player(state, players), players, state)

        actorB2 = current_player(state, players)
        check("it is B's turn again", actorB2.id == actorB.id)
        state = send_cmd(actorB2, players, "reinforce_land_begin")
        check("PC bonus legion auto-placed (Â§6.5)",
              state["map"]["provinces"][pc_prov_b]["legions"] == legions_pc_b + 1,
              f"expected {legions_pc_b + 1}, got {state['map']['provinces'][pc_prov_b]['legions']}")
        check("base reinforcements unchanged", state["pending"]["landReinforceRemaining"] == 3)

        # e il punteggio: B ha 1 centro -> almeno 1 VP da power centers
        score_b = next(pl["score"] for pl in state["players"] if pl["id"] == actorB2.id)
        check("B scored VP for power center (Â§5.6.4)", score_b >= 1, f"score={score_b}")

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()


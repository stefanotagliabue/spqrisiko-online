"""
Smoke test Blocco D:
- Â§14.2 guarnigione minima 2 ai confini nemici (strategic_move, occupy_extra)
- Â§14.3 guarnigione minima limita l'attacco via mare
- Â§18.4 nessuna eliminazione prima della fine del 4Â° round
- Â§2.6 massimo 5 giocatori
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

random.seed(7)

client = TestClient(server.app)
ROOM = "TEST04"

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


def is_enemy(provs, pid, color):
    o = provs[pid].get("owner")
    return o is not None and o != color


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

        print("== TURNO 1: guarnigione Â§14.2 (strategic move) ==")
        a1 = current_player(state, players)
        state = send_cmd(a1, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]
        state = send_cmd(a1, players, "reinforce_land_place",
                         {"placements": {owned_by(state, a1.color)[0]: rem}})
        for _ in range(4):
            state = send_cmd(a1, players, "end_phase")
        state = send_cmd(a1, players, "end_attacks")

        # F: mia, con vicino nemico e vicino mio
        provs = state["map"]["provinces"]
        F = T = None
        for pid in owned_by(state, a1.color):
            has_enemy = any(is_enemy(provs, nb, a1.color) for nb in provs[pid]["adj_land"])
            own_nb = next((nb for nb in provs[pid]["adj_land"] if provs[nb]["owner"] == a1.color), None)
            if has_enemy and own_nb:
                F, T = pid, own_nb
                break
        check("border province found", F is not None)
        gs_live()["map"]["provinces"][F]["legions"] = 5

        # lasciando 1 sola legione al confine: rifiutato
        expect_error(a1, "strategic_move", {"from": F, "to": T, "count": 4},
                     "14.2", "strategic move refused leaving 1 at enemy border (Â§14.2)")
        # lasciandone 2: ok
        state = send_cmd(a1, players, "strategic_move", {"from": F, "to": T, "count": 3})
        check("strategic move ok leaving 2", state["map"]["provinces"][F]["legions"] == 2)
        check("turn 1 ended", state["turn"]["phase"] == "SCORE")

        print("== TURNO 2: guarnigione Â§14.2 (occupy_extra) ==")
        a2 = current_player(state, players)
        state = send_cmd(a2, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]

        # Fb: mia con almeno 2 vicini nemici (dopo la conquista ne resta almeno 1)
        provs = state["map"]["provinces"]
        Fb = Tb = None
        for pid in owned_by(state, a2.color):
            enemies = [nb for nb in provs[pid]["adj_land"] if is_enemy(provs, nb, a2.color)]
            if len(enemies) >= 2:
                Fb, Tb = pid, enemies[0]
                break
        check("multi-border province found", Fb is not None)

        state = send_cmd(a2, players, "reinforce_land_place", {"placements": {Fb: rem}})
        live = gs_live()
        live["map"]["provinces"][Fb]["legions"] = 10
        live["map"]["provinces"][Tb]["legions"] = 1

        for _ in range(4):
            state = send_cmd(a2, players, "end_phase")

        conquered = False
        for _ in range(15):
            if state["map"]["provinces"][Tb]["owner"] == a2.color:
                conquered = True
                break
            state = send_cmd(a2, players, "land_attack_roll", {"from": Fb, "to": Tb, "attackDice": 3})
        check("conquest for occupy test", conquered)

        provs = state["map"]["provinces"]
        garrison = 2 if any(is_enemy(provs, nb, a2.color) for nb in provs[Fb]["adj_land"]) else 1
        available = provs[Fb]["legions"] - garrison
        check("garrison is 2 (still borders enemy)", garrison == 2)

        expect_error(a2, "occupy_extra", {"count": available + 1},
                     "14.2", "occupy_extra refused beyond garrison (Â§14.2)")
        if available >= 1:
            state = send_cmd(a2, players, "occupy_extra", {"count": available})
            check("occupy_extra ok up to garrison limit",
                  state["map"]["provinces"][Fb]["legions"] == garrison)
        state = send_cmd(a2, players, "end_attacks")
        state = send_cmd(a2, players, "end_turn")

        print("== TURNO 3: guarnigione Â§14.3 (attacco via mare) ==")
        a3 = current_player(state, players)
        state = send_cmd(a3, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]

        # Fc: mia costiera con vicino nemico via terra; Tc nemica sullo stesso mare
        provs = state["map"]["provinces"]
        seas = state["map"]["seas"]
        Fc = Tc = sc = None
        for pid in owned_by(state, a3.color):
            if not provs[pid]["adj_sea"]:
                continue
            if not any(is_enemy(provs, nb, a3.color) for nb in provs[pid]["adj_land"]):
                continue
            for sid in provs[pid]["adj_sea"]:
                target = next((o for o in seas[sid]["adj_land"]
                               if provs[o]["owner"] != a3.color), None)
                if target:
                    Fc, Tc, sc = pid, target, sid
                    break
            if Fc:
                break
        check("coastal border province found", Fc is not None)

        state = send_cmd(a3, players, "reinforce_land_place",
                         {"placements": {owned_by(state, a3.color)[0]: rem}})
        live = gs_live()
        live["map"]["provinces"][Fc]["legions"] = 5
        live["map"]["provinces"][Tc]["legions"] = 1
        live["map"]["seas"][sc]["triremes"] = {a3.color: 1}

        for _ in range(3):  # -> SEA_ATTACKS
            state = send_cmd(a3, players, "end_phase")
        check("phase SEA_ATTACKS", state["turn"]["phase"] == "SEA_ATTACKS")

        # 5 legioni al confine nemico: max 3 attaccanti (ne devono restare 2)
        expect_error(a3, "sea_attack", {"from": Fc, "to": Tc, "seaId": sc, "legions": 4},
                     "14.3", "sea attack refused beyond garrison (Â§14.3)")
        state = send_cmd(a3, players, "sea_attack", {"from": Fc, "to": Tc, "seaId": sc, "legions": 3})
        check("sea attack ok within garrison", state["map"]["provinces"][Fc]["legions"] == 2)

        state = send_cmd(a3, players, "end_phase")
        state = send_cmd(a3, players, "end_turn")

        print("== TURNO 4: protezione Â§18.4 (round <= 4) ==")
        a4 = current_player(state, players)
        state = send_cmd(a4, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]

        # vittima: un altro giocatore, ridotto a 1 provincia adiacente a una mia
        victim = next(p for p in players if p is not a4)
        provs = state["map"]["provinces"]
        F4 = Bp = None
        for pid in owned_by(state, victim.color):
            attacker_nb = next((nb for nb in provs[pid]["adj_land"]
                                if provs[nb]["owner"] == a4.color), None)
            if attacker_nb:
                F4, Bp = attacker_nb, pid
                break
        check("victim scenario found", Bp is not None)

        state = send_cmd(a4, players, "reinforce_land_place", {"placements": {F4: rem}})
        live = gs_live()
        for pid in owned_by(state, victim.color):
            if pid != Bp:
                live["map"]["provinces"][pid]["owner"] = "NEUTRAL_BLACK"
        live["map"]["provinces"][Bp]["legions"] = 1
        live["map"]["provinces"][F4]["legions"] = 5

        for _ in range(4):
            state = send_cmd(a4, players, "end_phase")

        rnd = state["turn"]["round"]
        check("still in protected rounds", rnd <= 4, f"round={rnd}")
        expect_error(a4, "land_attack_roll", {"from": F4, "to": Bp, "attackDice": 3},
                     "18.4", "attack on last province refused in early rounds (Â§18.4)")

        # dal round 5 la protezione decade
        live["turn"]["round"] = 5
        state = send_cmd(a4, players, "land_attack_roll", {"from": F4, "to": Bp, "attackDice": 3})
        check("attack allowed from round 5", True)

    print("== MAX 5 GIOCATORI (Â§2.6) ==")
    ROOM6 = "TEST05"
    N = 6
    ctxs = [client.websocket_connect(f"/ws/{ROOM6}/G{i}") for i in range(N)]
    socks = [c.__enter__() for c in ctxs]
    plist = []
    try:
        for i, ws in enumerate(socks):
            m = ws.receive_json()
            p = Player(ws, f"G{i}")
            p.id = m["playerId"]
            plist.append(p)
            for _ in range(2 * (N - i)):
                ws.receive_json()
        for p in plist:
            send_cmd(p, plist, "ready")
        expect_error(plist[0], "start_game", {}, "max 5", "start refused with 6 players (Â§2.6)")
    finally:
        for c in ctxs:
            c.__exit__(None, None, None)

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()


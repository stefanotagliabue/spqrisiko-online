"""
Smoke test regola casa "garrison-first" (anti-farming):
- durante i rinforzi terrestri, le province di frontiera (vicino terrestre
  nemico, neutrali inclusi) con 1 sola legione vanno riportate a 2 prima
  di piazzare liberamente
- se i rinforzi non bastano, vanno tutti sulle province in deficit, 1 ciascuna
- le province interne (senza vicini nemici via terra) a 1 legione sono esenti
- i rinforzi extra da tris rientrano nell'obbligo (piazzamento unico)
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
ROOM = "TEST06"

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


def border_provinces(state, color):
    """Province di color con almeno un vicino terrestre nemico (neutrali inclusi)."""
    provs = state["map"]["provinces"]
    return [pid for pid in owned_by(state, color)
            if any(is_enemy(provs, nb, color) for nb in provs[pid]["adj_land"])]


def normalize_all_legions(color, n=3):
    """Porta tutte le province di color a n legioni (stato deterministico)."""
    for pr in gs_live()["map"]["provinces"].values():
        if pr.get("owner") == color:
            pr["legions"] = n


def end_turn_cleanly(actor, players, state):
    for _ in range(4):  # REINFORCE_NAVAL -> ... -> LAND_ATTACKS
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

        print("== TURNO 1: deficit coperto prima del piazzamento libero ==")
        a1 = current_player(state, players)
        state = send_cmd(a1, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]
        check("reinforcements >= 2", rem >= 2, f"rem={rem}")

        normalize_all_legions(a1.color)
        borders = border_provinces(state, a1.color)
        check("at least 2 border provinces", len(borders) >= 2)
        D1 = borders[0]
        X = next(pid for pid in owned_by(state, a1.color) if pid != D1)
        gs_live()["map"]["provinces"][D1]["legions"] = 1

        expect_error(a1, "reinforce_land_place", {"placements": {X: rem}},
                     "garrison-first", "free placement refused with open deficit")
        state = send_cmd(a1, players, "reinforce_land_place",
                         {"placements": {D1: 1, X: rem - 1}})
        check("deficit province restored to 2", state["map"]["provinces"][D1]["legions"] == 2)
        state = end_turn_cleanly(a1, players, state)

        print("== TURNO 2: tris aumenta l'obbligo; interna a 1 esente ==")
        a2 = current_player(state, players)
        live = gs_live()
        p2_live = next(pl for pl in live["players"] if pl["id"] == a2.id)
        p2_live["cards"] = [{"symbol": "LEGIONARIO"}] * 3

        state = send_cmd(a2, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]

        normalize_all_legions(a2.color)
        # P: interna (tutti i vicini diventano miei) e a 1 legione -> non in deficit
        provs = live["map"]["provinces"]
        P = owned_by(state, a2.color)[0]
        for nb in provs[P]["adj_land"]:
            provs[nb]["owner"] = a2.color
            provs[nb]["legions"] = 3
        provs[P]["legions"] = 1

        state = send_cmd(a2, players, "play_tris", {"cards": [0, 1, 2]})
        rem2 = state["pending"]["landReinforceRemaining"]
        check("tris added reinforcements", rem2 == rem + 8, f"rem2={rem2}")

        borders = [pid for pid in border_provinces(state, a2.color) if pid != P]
        check("border province found (turn 2)", len(borders) >= 1)
        F2 = borders[0]
        provs[F2]["legions"] = 1
        X2 = next(pid for pid in owned_by(state, a2.color) if pid not in (F2, P))

        expect_error(a2, "reinforce_land_place", {"placements": {X2: rem2}},
                     "garrison-first", "tris reinforcements also bound by deficit")
        state = send_cmd(a2, players, "reinforce_land_place",
                         {"placements": {F2: 1, X2: rem2 - 1}})
        check("border deficit restored to 2", state["map"]["provinces"][F2]["legions"] == 2)
        check("interior province at 1 exempt", state["map"]["provinces"][P]["legions"] == 1)
        state = end_turn_cleanly(a2, players, state)

        print("== TURNO 3: rinforzi insufficienti -> 1 ciascuna solo sui deficit ==")
        a3 = current_player(state, players)
        state = send_cmd(a3, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]

        normalize_all_legions(a3.color)
        borders = border_provinces(state, a3.color)
        need = rem + 1
        check(f"at least {need} border provinces", len(borders) >= need, f"found {len(borders)}")
        deficits = borders[:need]
        for pid in deficits:
            gs_live()["map"]["provinces"][pid]["legions"] = 1
        X3 = next(pid for pid in owned_by(state, a3.color) if pid not in deficits)

        expect_error(a3, "reinforce_land_place", {"placements": {X3: rem}},
                     "garrison-first", "non-deficit placement refused when short")
        expect_error(a3, "reinforce_land_place", {"placements": {deficits[0]: rem}},
                     "garrison-first", "piling on one deficit refused when short")
        state = send_cmd(a3, players, "reinforce_land_place",
                         {"placements": {pid: 1 for pid in deficits[:rem]}})
        for pid in deficits[:rem]:
            check(f"deficit {pid} restored", state["map"]["provinces"][pid]["legions"] == 2)
        check("uncovered deficit stays at 1",
              state["map"]["provinces"][deficits[rem]]["legions"] == 1)

        print("== TURNO 3b: riequilibrio obbligatorio a fine turno ==")
        # a3 e' in REINFORCE_NAVAL; costruisco: D4 a 1 con donatrice Q4 adiacente
        normalize_all_legions(a3.color)
        provs = gs_live()["map"]["provinces"]
        D4 = Q4 = None
        for pid in border_provinces(state, a3.color):
            own_nb = next((nb for nb in provs[pid]["adj_land"]
                           if provs[nb].get("owner") == a3.color), None)
            if own_nb:
                D4, Q4 = pid, own_nb
                break
        check("deficit with own neighbour found", D4 is not None)
        provs[D4]["legions"] = 1

        for _ in range(4):  # -> LAND_ATTACKS
            state = send_cmd(a3, players, "end_phase")
        expect_error(a3, "end_turn", {}, "garrison-first",
                     "end_turn refused from LAND_ATTACKS with fixable deficit")
        state = send_cmd(a3, players, "end_attacks")
        expect_error(a3, "end_turn", {}, "garrison-first",
                     "end_turn refused from STRATEGIC_MOVE with fixable deficit")

        # spostamento che non riduce le carenze: rifiutato
        A4 = B4 = None
        for pid in owned_by(state, a3.color):
            if pid == D4 or provs[pid]["legions"] < 3:
                continue
            nb = next((nb for nb in provs[pid]["adj_land"]
                       if provs[nb].get("owner") == a3.color and nb != D4), None)
            if nb:
                A4, B4 = pid, nb
                break
        check("non-deficit move pair found", A4 is not None)
        expect_error(a3, "strategic_move", {"from": A4, "to": B4, "count": 1},
                     "garrison-first", "strategic move to non-deficit refused")

        state = send_cmd(a3, players, "strategic_move", {"from": Q4, "to": D4, "count": 1})
        check("rebalance move accepted", state["map"]["provinces"][D4]["legions"] == 2)
        check("turn ended after rebalance", state["turn"]["phase"] == "SCORE")

        print("== TURNO 4: nessuna mossa fattibile -> end_turn permesso ==")
        a5 = current_player(state, players)
        state = send_cmd(a5, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]
        normalize_all_legions(a5.color)
        state = send_cmd(a5, players, "reinforce_land_place",
                         {"placements": {owned_by(state, a5.color)[0]: rem}})

        # nessuna donatrice possibile: frontiere a 2, interne a 1; D5 resta in deficit
        provs = gs_live()["map"]["provinces"]
        borders5 = set(border_provinces(state, a5.color))
        check("border provinces found (turn 4)", len(borders5) > 0)
        for pid in owned_by(state, a5.color):
            provs[pid]["legions"] = 2 if pid in borders5 else 1
        D5 = sorted(borders5)[0]
        provs[D5]["legions"] = 1
        for s in gs_live()["map"]["seas"].values():
            (s.get("triremes") or {}).pop(a5.color, None)

        for _ in range(4):
            state = send_cmd(a5, players, "end_phase")
        state = send_cmd(a5, players, "end_turn")
        check("end_turn allowed when no rebalance possible",
              state["turn"]["phase"] == "SCORE")

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()

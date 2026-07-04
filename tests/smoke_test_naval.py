"""
Smoke test Blocco B (layer navale): buy_trireme, trireme_to_legions, naval_move,
naval_attack_roll, sea_attack (Â§12.2/12.3/12.6/12.7), end_phase,
strategic move via mare (Â§15.2).

Usa il flusso WebSocket reale + iniezione diretta di stato (server.GAMES)
per costruire scenari deterministici.
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

random.seed(1234)  # dadi deterministici (server usa il modulo random globale)

client = TestClient(server.app)
ROOM = "TEST02"

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
    """Stato di gioco reale nel processo del server (per iniezioni)."""
    return server.GAMES[ROOM]


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

        print("== TURNO A: rinforzi navali ==")
        actor = current_player(state, players)
        state = send_cmd(actor, players, "reinforce_land_begin")
        remaining = state["pending"]["landReinforceRemaining"]

        # provincia costiera del giocatore
        provs = state["map"]["provinces"]
        coastal = next(pid for pid in owned_by(state, actor.color) if provs[pid]["adj_sea"])
        sea_a = provs[coastal]["adj_sea"][0]
        non_adj_sea = next(s for s in state["map"]["seas"] if s not in provs[coastal]["adj_sea"])

        state = send_cmd(actor, players, "reinforce_land_place", {"placements": {coastal: remaining}})
        check("phase REINFORCE_NAVAL after place", state["turn"]["phase"] == "REINFORCE_NAVAL")

        # compra trireme: 5 legioni -> 2, +1 trireme
        state = send_cmd(actor, players, "buy_trireme", {"provinceId": coastal, "seaId": sea_a})
        check("trireme bought", state["map"]["seas"][sea_a]["triremes"].get(actor.color) == 1)
        check("legions -3", state["map"]["provinces"][coastal]["legions"] == 2)

        expect_error(actor, "buy_trireme", {"provinceId": coastal, "seaId": sea_a},
                     "at least 4", "buy_trireme refused with < 4 legions")
        expect_error(actor, "buy_trireme", {"provinceId": coastal, "seaId": non_adj_sea},
                     "not adjacent", "buy_trireme refused for non-adjacent sea")

        # iniezione: piÃ¹ legioni per comprare una seconda trireme
        gs_live()["map"]["provinces"][coastal]["legions"] = 8
        state = send_cmd(actor, players, "buy_trireme", {"provinceId": coastal, "seaId": sea_a})
        check("second trireme", state["map"]["seas"][sea_a]["triremes"].get(actor.color) == 2)

        print("== MOVIMENTO NAVALE ==")
        state = send_cmd(actor, players, "end_phase")
        check("phase NAVAL_MOVE", state["turn"]["phase"] == "NAVAL_MOVE")

        sea_b = state["map"]["seas"][sea_a]["adj_sea"][0]
        state = send_cmd(actor, players, "naval_move", {"fromSea": sea_a, "toSea": sea_b, "count": 1})
        check("trireme moved", state["map"]["seas"][sea_b]["triremes"].get(actor.color) == 1)
        check("source updated", state["map"]["seas"][sea_a]["triremes"].get(actor.color) == 1)
        check("auto-advance to NAVAL_COMBAT (single move)", state["turn"]["phase"] == "NAVAL_COMBAT")

        print("== COMBATTIMENTO NAVALE ==")
        # iniezione: triremi nemiche (Bruno) nello stesso mare sea_a
        enemy = next(p for p in players if p is not actor)
        third = next(p for p in players if p is not actor and p is not enemy)
        gs_live()["map"]["seas"][sea_a]["triremes"][enemy.color] = 1
        gs_live()["map"]["seas"][sea_a]["triremes"][third.color] = 1

        state = send_cmd(actor, players, "naval_attack_roll",
                         {"seaId": sea_a, "targetColor": enemy.color, "attackDice": 1})
        combat = state["turn"]["navalCombats"].get(sea_a)
        check("naval combat recorded", combat is not None and combat["target"] == enemy.color, f"got {combat}")
        total = (state["map"]["seas"][sea_a]["triremes"].get(actor.color, 0)
                 + state["map"]["seas"][sea_a]["triremes"].get(enemy.color, 0))
        check("naval losses consistent", 0 <= total <= 2, f"total={total}")

        # Â§11.4: nello stesso mare non si puÃ² aprire un secondo combattimento vs altro colore
        # (reinietta triremi a entrambi nel caso il lancio le abbia eliminate)
        gs_live()["map"]["seas"][sea_a]["triremes"][actor.color] = 1
        gs_live()["map"]["seas"][sea_a]["triremes"][third.color] = 1
        expect_error(actor, "naval_attack_roll", {"seaId": sea_a, "targetColor": third.color, "attackDice": 1},
                     "already", "one combat per sea per turn (Â§11.4)")

        print("== ATTACCO VIA MARE ==")
        state = send_cmd(actor, players, "end_phase")
        check("phase SEA_ATTACKS", state["turn"]["phase"] == "SEA_ATTACKS")

        # scenario iniettato: F mia (12 legioni), T nemica (2 legioni), stesso mare, superioritÃ  mia
        provs = state["map"]["provinces"]
        sa_from, sa_to, sa_sea = None, None, None
        for pid in owned_by(state, actor.color):
            for sid in provs[pid]["adj_sea"]:
                for other in state["map"]["seas"][sid]["adj_land"]:
                    if provs[other]["owner"] != actor.color:
                        sa_from, sa_to, sa_sea = pid, other, sid
                        break
                if sa_from:
                    break
            if sa_from:
                break
        check("sea attack scenario found", sa_from is not None)

        live = gs_live()
        live["map"]["provinces"][sa_from]["legions"] = 12
        live["map"]["provinces"][sa_to]["owner"] = enemy.color
        live["map"]["provinces"][sa_to]["legions"] = 2
        live["map"]["seas"][sa_sea]["triremes"][actor.color] = 3
        live["map"]["seas"][sa_sea]["triremes"].pop(enemy.color, None)

        # Â§12.2: senza superioritÃ  niente attacco (iniettiamo paritÃ  momentanea)
        live["map"]["seas"][sa_sea]["triremes"][enemy.color] = 3
        expect_error(actor, "sea_attack",
                     {"from": sa_from, "to": sa_to, "seaId": sa_sea, "legions": 8},
                     "more triremes", "sea attack refused without superiority (Â§12.2)")
        live["map"]["seas"][sa_sea]["triremes"][enemy.color] = 1

        state = send_cmd(actor, players, "sea_attack",
                         {"from": sa_from, "to": sa_to, "seaId": sa_sea, "legions": 8})
        conquered = state["map"]["provinces"][sa_to]["owner"] == actor.color
        check("sea attack resolved to the death",
              conquered or state["map"]["provinces"][sa_to]["owner"] == enemy.color)
        print(f"  info: conquered={conquered}, {sa_to} legions={state['map']['provinces'][sa_to]['legions']}")
        check("attacking force left the province", state["map"]["provinces"][sa_from]["legions"] == 4)
        check("province marked sea-attacked (Â§12.6)", sa_to in state["turn"]["seaAttackedProvinces"])

        # per testare il Â§12.6 la provincia deve risultare nemica: se Ã¨ stata
        # conquistata la rimettiamo temporaneamente al difensore
        owner_now = gs_live()["map"]["provinces"][sa_to]["owner"]
        gs_live()["map"]["provinces"][sa_to]["owner"] = enemy.color
        expect_error(actor, "sea_attack",
                     {"from": sa_from, "to": sa_to, "seaId": sa_sea, "legions": 2},
                     "already attacked", "same province not sea-attackable twice (Â§12.6)")
        gs_live()["map"]["provinces"][sa_to]["owner"] = owner_now

        if conquered:
            check("conquered flag", state["turn"]["conqueredThisTurn"] is True)
            check("marked sea-conquered (Â§12.7)", sa_to in state["turn"]["seaConqueredProvinces"])
            # Â§12.7: da sa_to niente nuovo attacco via mare nello stesso turno
            target2 = next((o for o in state["map"]["seas"][sa_sea]["adj_land"]
                            if state["map"]["provinces"][o]["owner"] != actor.color), None)
            if target2:
                expect_error(actor, "sea_attack",
                             {"from": sa_to, "to": target2, "seaId": sa_sea, "legions": 1},
                             "conquered by sea", "no sea attack from sea-conquered province (Â§12.7)")

        state = send_cmd(actor, players, "end_phase")
        check("phase LAND_ATTACKS", state["turn"]["phase"] == "LAND_ATTACKS")
        state = send_cmd(actor, players, "end_turn")
        check("turn A ended", state["turn"]["phase"] == "SCORE")

        print("== TURNO B: trireme -> legioni (Â§6.6) ==")
        actor2 = current_player(state, players)
        state = send_cmd(actor2, players, "reinforce_land_begin")

        provs = state["map"]["provinces"]
        coastal2 = next(pid for pid in owned_by(state, actor2.color) if provs[pid]["adj_sea"])
        sea2 = provs[coastal2]["adj_sea"][0]
        gs_live()["map"]["seas"][sea2].setdefault("triremes", {})[actor2.color] = 1
        legions_before = provs[coastal2]["legions"]

        state = send_cmd(actor2, players, "trireme_to_legions", {"seaId": sea2, "provinceId": coastal2})
        check("trireme converted to +2 legions",
              state["map"]["provinces"][coastal2]["legions"] == legions_before + 2)
        check("trireme removed from sea",
              state["map"]["seas"][sea2]["triremes"].get(actor2.color) is None)

        rem = state["pending"]["landReinforceRemaining"]
        state = send_cmd(actor2, players, "reinforce_land_place",
                         {"placements": {owned_by(state, actor2.color)[0]: rem}})
        for _ in range(4):  # REINFORCE_NAVAL -> ... -> LAND_ATTACKS
            state = send_cmd(actor2, players, "end_phase")
        check("skipped naval phases to LAND_ATTACKS", state["turn"]["phase"] == "LAND_ATTACKS")
        state = send_cmd(actor2, players, "end_turn")

        print("== TURNO C: spostamento strategico via mare (Â§15.2) ==")
        actor3 = current_player(state, players)
        state = send_cmd(actor3, players, "reinforce_land_begin")
        rem = state["pending"]["landReinforceRemaining"]

        # due province di C sullo stesso mare ma NON adiacenti via terra
        provs = state["map"]["provinces"]
        mine3 = owned_by(state, actor3.color)
        sm_from, sm_to, sm_sea = None, None, None
        for a in mine3:
            for b in mine3:
                if a == b or b in provs[a]["adj_land"]:
                    continue
                shared = set(provs[a]["adj_sea"]) & set(provs[b]["adj_sea"])
                if shared:
                    sm_from, sm_to, sm_sea = a, b, sorted(shared)[0]
                    break
            if sm_from:
                break

        state = send_cmd(actor3, players, "reinforce_land_place",
                         {"placements": {(sm_from or mine3[0]): rem}})
        for _ in range(4):
            state = send_cmd(actor3, players, "end_phase")
        state = send_cmd(actor3, players, "end_attacks")
        check("phase STRATEGIC_MOVE", state["turn"]["phase"] == "STRATEGIC_MOVE")

        if sm_from:
            # senza superioritÃ : rifiutato
            gs_live()["map"]["seas"][sm_sea]["triremes"] = {}
            expect_error(actor3, "strategic_move", {"from": sm_from, "to": sm_to, "count": 1},
                         "no route", "strategic move refused without naval superiority")
            # con superioritÃ : ok
            gs_live()["map"]["seas"][sm_sea]["triremes"] = {actor3.color: 1}
            before = state["map"]["provinces"][sm_to]["legions"]
            state = send_cmd(actor3, players, "strategic_move", {"from": sm_from, "to": sm_to, "count": 1})
            check("strategic move via sea done",
                  state["map"]["provinces"][sm_to]["legions"] == before + 1)
            check("turn ended after strategic move", state["turn"]["phase"] == "SCORE")
        else:
            print("  info: no sea-only pair found for strategic move test, skipping")
            state = send_cmd(actor3, players, "end_turn")

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()


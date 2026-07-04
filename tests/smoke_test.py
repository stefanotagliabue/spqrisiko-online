"""
Smoke test Blocco A: simula una partita a 3 giocatori via WebSocket (TestClient).
Copre: lobby/ready/start, setup completo (45 province), punteggio a inizio turno,
rinforzi, attacchi terrestri, occupy_extra, end_attacks, strategic_move,
end_turn, pesca carta, rotazione turni.

Uso:  python smoke_test.py   (dalla cartella del progetto, o con PYTHONPATH impostato)
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import tempfile  # noqa: E402
os.environ["SPQR_DATA_DIR"] = tempfile.mkdtemp(prefix="spqr-rooms-")

from fastapi.testclient import TestClient  # noqa: E402
import server  # noqa: E402

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


def send_cmd(sender: Player, all_players, cmd, payload=None):
    """Invia un cmd; il sender legge la risposta (state o error),
    gli altri drenano il broadcast di stato. Ritorna lo stato aggiornato."""
    sender.ws.send_json({"type": "cmd", "cmd": cmd, "playerId": sender.id, "payload": payload or {}})
    msg = sender.ws.receive_json()
    if msg["type"] == "error":
        raise RuntimeError(f"[{sender.name}] {cmd} -> ERROR: {msg['error']}")
    assert msg["type"] == "state", f"unexpected msg type {msg['type']}"
    for p in all_players:
        if p is not sender:
            p.ws.receive_json()  # drain broadcast
    return msg["state"]


def current_player(state, players):
    idx = state["turn"]["turnIndex"]
    pid = state["players"][idx]["id"]
    return next(p for p in players if p.id == pid)


def free_provinces(state):
    return sorted(pid for pid, pr in state["map"]["provinces"].items() if pr["owner"] is None)


def owned_by(state, color):
    return sorted(pid for pid, pr in state["map"]["provinces"].items() if pr["owner"] == color)


def main():
    ROOM = "TEST01"
    with client.websocket_connect(f"/ws/{ROOM}/Anna") as ws1, \
         client.websocket_connect(f"/ws/{ROOM}/Bruno") as ws2, \
         client.websocket_connect(f"/ws/{ROOM}/Carla") as ws3:

        p1, p2, p3 = Player(ws1, "Anna"), Player(ws2, "Bruno"), Player(ws3, "Carla")
        players = [p1, p2, p3]

        # --- drain connection messages ---
        # p1: welcome, join, state | join,state (p2) | join,state (p3)  = 7
        # p2: welcome, join, state | join,state (p3)                    = 5
        # p3: welcome, join, state                                       = 3
        m = ws1.receive_json(); p1.id = m["playerId"]
        for _ in range(6):
            ws1.receive_json()
        m = ws2.receive_json(); p2.id = m["playerId"]
        for _ in range(4):
            ws2.receive_json()
        m = ws3.receive_json(); p3.id = m["playerId"]
        for _ in range(2):
            ws3.receive_json()

        print("== LOBBY ==")
        state = send_cmd(p1, players, "ready")
        state = send_cmd(p2, players, "ready")
        state = send_cmd(p3, players, "ready")
        check("all ready", all(pl["ready"] for pl in state["players"]))

        state = send_cmd(p1, players, "start_game", {"targetScore": 999})
        check("phase SETUP", state["turn"]["phase"] == "SETUP")
        check("deck 55 cards", len(state["deck"]) == 55, f"got {len(state['deck'])}")
        check("targetScore set", state["settings"]["targetScore"] == 999)
        check("neutral pool 18 (3 players)", len(state["setup"]["neutralPool"]) == 18)
        for pl, obj in zip(state["players"], players):
            obj.color = pl["color"]

        print("== SETUP (45 province) ==")
        claims = 0
        while state["turn"]["phase"] == "SETUP":
            actor = current_player(state, players)
            free = free_provinces(state)
            payload = {"provinceId": free[0]}
            if len(state["setup"]["neutralPool"]) > 0:
                payload["neutralProvinceId"] = free[1]
            state = send_cmd(actor, players, "setup_claim", payload)
            claims += 1
            if claims > 30:
                break
        check("setup done in 27 player claims", claims == 27, f"claims={claims}")
        check("phase SCORE after setup", state["turn"]["phase"] == "SCORE")
        check("all 45 provinces owned", len(free_provinces(state)) == 0)
        for obj in players:
            n = len(owned_by(state, obj.color))
            check(f"{obj.name} has 9 provinces", n == 9, f"got {n}")

        print("== TURNO 1: score + rinforzi ==")
        actor = current_player(state, players)
        score_before = next(pl["score"] for pl in state["players"] if pl["id"] == actor.id)
        state = send_cmd(actor, players, "reinforce_land_begin")
        check("phase REINFORCE_LAND", state["turn"]["phase"] == "REINFORCE_LAND")
        remaining = state["pending"]["landReinforceRemaining"]
        check("3 reinforcements (9 provinces)", remaining == 3, f"got {remaining}")
        score_after = next(pl["score"] for pl in state["players"] if pl["id"] == actor.id)
        print(f"  info: {actor.name} score {score_before} -> {score_after}")

        # piazza tutti i rinforzi su una provincia confinante con un nemico debole (2 legioni)
        provs = state["map"]["provinces"]
        target_from, target_to = None, None
        for pid in owned_by(state, actor.color):
            for nb in provs[pid]["adj_land"]:
                if provs[nb]["owner"] != actor.color and provs[nb]["legions"] == 2:
                    target_from, target_to = pid, nb
                    break
            if target_from:
                break
        check("found attack candidate", target_from is not None)

        state = send_cmd(actor, players, "reinforce_land_place",
                         {"placements": {target_from: remaining}})
        check("phase REINFORCE_NAVAL", state["turn"]["phase"] == "REINFORCE_NAVAL")
        check("legions placed", state["map"]["provinces"][target_from]["legions"] == 5)

        # salta le fasi navali (facoltative)
        for _ in range(4):
            state = send_cmd(actor, players, "end_phase")
        check("phase LAND_ATTACKS after naval skip", state["turn"]["phase"] == "LAND_ATTACKS")

        print(f"== ATTACCO {target_from} -> {target_to} ==")
        conquered = False
        for _ in range(15):
            provs = state["map"]["provinces"]
            fl = provs[target_from]["legions"]
            tl = provs[target_to]["legions"]
            if provs[target_to]["owner"] == actor.color:
                conquered = True
                break
            def_dice = min(3, tl)
            max_att = min(3, fl - 1)
            if max_att < def_dice:
                print(f"  info: attack no longer possible (att {max_att} < def {def_dice})")
                break
            state = send_cmd(actor, players, "land_attack_roll",
                             {"from": target_from, "to": target_to, "attackDice": max_att})
        print(f"  info: conquered={conquered}")

        if conquered:
            check("conqueredThisTurn flag", state["turn"]["conqueredThisTurn"] is True)
            occ = state["pending"]["occupation"]
            check("pending occupation set", occ == {"from": target_from, "to": target_to}, f"got {occ}")
            provs = state["map"]["provinces"]
            garrison = 2 if any(provs[nb]["owner"] not in (None, actor.color)
                                for nb in provs[target_from]["adj_land"]) else 1
            avail = provs[target_from]["legions"] - garrison
            if avail >= 1:
                before_to = state["map"]["provinces"][target_to]["legions"]
                state = send_cmd(actor, players, "occupy_extra", {"count": 1})
                check("occupy_extra moved 1",
                      state["map"]["provinces"][target_to]["legions"] == before_to + 1)
                check("occupation cleared", state["pending"]["occupation"] is None)
            else:
                print("  info: no extra legions available, skip occupy_extra")

        print("== FINE ATTACCHI / STRATEGIC MOVE ==")
        state = send_cmd(actor, players, "end_attacks")
        check("phase STRATEGIC_MOVE", state["turn"]["phase"] == "STRATEGIC_MOVE")

        # trova due province proprie adiacenti con legioni da spostare
        # (rispettando la guarnigione minima Â§14.2)
        provs = state["map"]["provinces"]
        sm_from, sm_to = None, None
        for pid in owned_by(state, actor.color):
            garrison = 2 if any(provs[nb]["owner"] not in (None, actor.color)
                                for nb in provs[pid]["adj_land"]) else 1
            if provs[pid]["legions"] >= garrison + 1:
                for nb in provs[pid]["adj_land"]:
                    if provs[nb]["owner"] == actor.color:
                        sm_from, sm_to = pid, nb
                        break
            if sm_from:
                break

        cards_before = next(len(pl["cards"]) for pl in state["players"] if pl["id"] == actor.id)
        idx_before = state["turn"]["turnIndex"]

        if sm_from:
            state = send_cmd(actor, players, "strategic_move", {"from": sm_from, "to": sm_to, "count": 1})
            print(f"  info: strategic move {sm_from} -> {sm_to}")
        else:
            state = send_cmd(actor, players, "end_turn")
            print("  info: no strategic move possible, end_turn")

        check("turn ended -> phase SCORE", state["turn"]["phase"] == "SCORE")
        check("turn rotated", state["turn"]["turnIndex"] != idx_before or len(players) == 1)

        cards_after = next(len(pl["cards"]) for pl in state["players"] if pl["id"] == actor.id)
        if conquered:
            check("card drawn after conquest", cards_after == cards_before + 1)
            check("deck decreased", len(state["deck"]) == 54, f"got {len(state['deck'])}")
        else:
            check("no card without conquest", cards_after == cards_before)

        print("== TURNO 2: rotazione ==")
        actor2 = current_player(state, players)
        check("different player", actor2.id != actor.id)
        state = send_cmd(actor2, players, "reinforce_land_begin")
        check("player 2 in REINFORCE_LAND", state["turn"]["phase"] == "REINFORCE_LAND")
        state = send_cmd(actor2, players, "reinforce_land_place",
                         {"placements": {owned_by(state, actor2.color)[0]: state["pending"]["landReinforceRemaining"]}})
        for _ in range(4):
            state = send_cmd(actor2, players, "end_phase")
        state = send_cmd(actor2, players, "end_turn")
        check("player 2 turn ended", state["turn"]["phase"] == "SCORE")
        actor3 = current_player(state, players)
        check("rotated to third player", actor3.id not in (actor.id, actor2.id))

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()


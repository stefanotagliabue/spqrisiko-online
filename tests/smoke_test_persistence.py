"""
Smoke test persistenza su disco:
- una partita in corso viene salvata in data/rooms/<ROOM>.json
- dopo un "riavvio" (clear + load_rooms) la partita torna com'era
- i giocatori rientrano con playerId+token e possono agire
- le stanze in LOBBY non lasciano file su disco
- nomi stanza non validi vengono rifiutati
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

random.seed(42)

client = TestClient(server.app)
ROOM = "TESTP1"
ROOM_LOBBY = "TESTP2"

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
    def __init__(self, name):
        self.name = name
        self.ctx = None
        self.ws = None
        self.id = None
        self.token = None

    def open(self, room, with_session=False):
        url = f"/ws/{room}/{self.name}"
        if with_session:
            url += f"?playerId={self.id}&token={self.token}"
        self.ctx = client.websocket_connect(url)
        self.ws = self.ctx.__enter__()
        return self.ws

    def close(self):
        if self.ctx:
            self.ctx.__exit__(None, None, None)
            self.ctx = None
            self.ws = None


def send_cmd(sender, connected_players, cmd, payload=None):
    sender.ws.send_json({"type": "cmd", "cmd": cmd, "playerId": sender.id, "payload": payload or {}})
    msg = sender.ws.receive_json()
    if msg["type"] == "error":
        raise RuntimeError(f"[{sender.name}] {cmd} -> ERROR: {msg['error']}")
    for p in connected_players:
        if p is not sender:
            p.ws.receive_json()
    return msg["state"]


def drain(p, n):
    return [p.ws.receive_json() for _ in range(n)]


def current_player(state, players):
    idx = state["turn"]["turnIndex"]
    pid = state["players"][idx]["id"]
    return next(p for p in players if p.id == pid)


def free_provinces(state):
    return sorted(pid for pid, pr in state["map"]["provinces"].items() if pr["owner"] is None)


def cleanup(room):
    server.GAMES.pop(room, None)
    server.TOKENS.pop(room, None)
    server.ROOMS.pop(room, None)
    server.PLAYER_WS.pop(room, None)
    server.delete_room_file(room)


def main():
    cleanup(ROOM)
    cleanup(ROOM_LOBBY)

    p1, p2, p3 = Player("Anna"), Player("Bruno"), Player("Carla")
    players = [p1, p2, p3]

    p1.open(ROOM)
    m = p1.ws.receive_json(); p1.id = m["playerId"]; p1.token = m["token"]
    drain(p1, 2)
    p2.open(ROOM)
    m = p2.ws.receive_json(); p2.id = m["playerId"]; p2.token = m["token"]
    drain(p2, 2)
    drain(p1, 2)
    p3.open(ROOM)
    m = p3.ws.receive_json(); p3.id = m["playerId"]; p3.token = m["token"]
    drain(p3, 2)
    drain(p1, 2)
    drain(p2, 2)

    print("== PARTITA IN CORSO -> FILE SU DISCO ==")
    check("no file while in LOBBY", not os.path.exists(server.room_file(ROOM)))

    for p in players:
        send_cmd(p, players, "ready")
    state = send_cmd(p1, players, "start_game", {"targetScore": 999})
    check("game started (SETUP)", state["turn"]["phase"] == "SETUP")
    check("file saved after start", os.path.exists(server.room_file(ROOM)))

    # qualche mossa di setup per avere stato da confrontare
    for _ in range(4):
        actor = current_player(state, players)
        free = free_provinces(state)
        payload = {"provinceId": free[0]}
        if len(state["setup"]["neutralPool"]) > 0:
            payload["neutralProvinceId"] = free[1]
        state = send_cmd(actor, players, "setup_claim", payload)

    owners_before = {pid: pr["owner"] for pid, pr in state["map"]["provinces"].items() if pr["owner"]}
    turn_idx_before = state["turn"]["turnIndex"]
    check("some provinces claimed", len(owners_before) > 0)

    for p in players:
        p.close()
    # drena i messaggi di leave/state fra le chiusure
    # (ogni close genera 2 messaggi per ciascun client ancora connesso)
    # p1 chiuso: p2 e p3 ricevono; p2 chiuso: p3 riceve — ma abbiamo
    # chiuso tutti in fila, quindi nessun drain necessario: sono già chiusi.

    check("game survives all-disconnect", ROOM in server.GAMES)
    check("file still on disk", os.path.exists(server.room_file(ROOM)))

    print("== RIAVVIO SIMULATO ==")
    server.GAMES.clear()
    server.TOKENS.clear()
    server.ROOMS.clear()
    server.PLAYER_WS.clear()
    check("memory empty after clear", ROOM not in server.GAMES)

    server.load_rooms()
    check("game restored from disk", ROOM in server.GAMES)
    gs = server.GAMES.get(ROOM)
    if gs:
        check("phase preserved", gs["turn"]["phase"] == "SETUP")
        check("turn index preserved", gs["turn"]["turnIndex"] == turn_idx_before)
        owners_after = {pid: pr["owner"] for pid, pr in gs["map"]["provinces"].items() if pr["owner"]}
        check("province owners preserved", owners_after == owners_before)
        check("players all marked disconnected",
              all(pl["connected"] is False for pl in gs["players"]))
        check("tokens restored", set(server.TOKENS.get(ROOM, {}).keys()) ==
              {p.id for p in players})

    print("== RIENTRO CON TOKEN DOPO IL RIAVVIO ==")
    actor = next(p for p in players if p.id == gs["players"][turn_idx_before]["id"])
    actor.open(ROOM, with_session=True)
    m = actor.ws.receive_json()
    check("same playerId after restart", m["playerId"] == actor.id)
    msgs = drain(actor, 2)
    state = next(mm["state"] for mm in msgs if mm["type"] == "state")

    free = free_provinces(state)
    payload = {"provinceId": free[0]}
    if len(state["setup"]["neutralPool"]) > 0:
        payload["neutralProvinceId"] = free[1]
    state = send_cmd(actor, [actor], "setup_claim", payload)
    check("restored game is playable", state["map"]["provinces"][free[0]]["owner"] is not None)
    actor.close()

    print("== LOBBY: NIENTE FILE ==")
    q = Player("Dino")
    q.open(ROOM_LOBBY)
    m = q.ws.receive_json(); q.id = m["playerId"]; q.token = m["token"]
    drain(q, 2)
    check("no file for LOBBY room", not os.path.exists(server.room_file(ROOM_LOBBY)))
    q.close()
    check("lobby room cleaned from memory", ROOM_LOBBY not in server.GAMES)

    print("== NOME STANZA NON VALIDO ==")
    with client.websocket_connect("/ws/TOOLONGROOMNAME123/Evo") as bad:
        m = bad.receive_json()
        check("invalid room rejected", m["type"] == "error" and "Invalid room" in m["error"])
    check("no file for invalid room", not os.path.exists(server.room_file("TOOLONGROOMNAME123")))

    cleanup(ROOM)

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()

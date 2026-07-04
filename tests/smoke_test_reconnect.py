"""
Smoke test riconnessione:
- disconnessione a partita iniziata: il giocatore resta (connected=False)
- riconnessione con playerId+token: stessa identita', colore preservato
- token errato / join senza sessione a partita iniziata: rifiutati
- doppia connessione: il socket vecchio viene rimpiazzato
- in LOBBY chi esce viene rimosso davvero
- la partita sopravvive alla disconnessione di tutti
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
    def __init__(self, name):
        self.name = name
        self.ctx = None
        self.ws = None
        self.id = None
        self.token = None
        self.color = None

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
    out = [p.ws.receive_json() for _ in range(n)]
    return out


def current_player(state, players):
    idx = state["turn"]["turnIndex"]
    pid = state["players"][idx]["id"]
    return next(p for p in players if p.id == pid)


def free_provinces(state):
    return sorted(pid for pid, pr in state["map"]["provinces"].items() if pr["owner"] is None)


def player_entry(state, p):
    return next(pl for pl in state["players"] if pl["id"] == p.id)


def main():
    p1, p2, p3 = Player("Anna"), Player("Bruno"), Player("Carla")

    # --- connessioni iniziali ---
    p1.open(ROOM)
    m = p1.ws.receive_json(); p1.id = m["playerId"]; p1.token = m["token"]
    drain(p1, 2)  # join + state

    p2.open(ROOM)
    m = p2.ws.receive_json(); p2.id = m["playerId"]; p2.token = m["token"]
    drain(p2, 2)
    drain(p1, 2)

    p3.open(ROOM)
    m = p3.ws.receive_json(); p3.id = m["playerId"]; p3.token = m["token"]
    drain(p3, 2)
    drain(p1, 2)
    drain(p2, 2)

    check("welcome includes token", all(x.token for x in (p1, p2, p3)))

    players = [p1, p2, p3]
    for p in players:
        send_cmd(p, players, "ready")
    state = send_cmd(p1, players, "start_game", {"targetScore": 999})
    for pl, obj in zip(state["players"], players):
        obj.color = pl["color"]
    check("game started (SETUP)", state["turn"]["phase"] == "SETUP")

    print("== DISCONNESSIONE A PARTITA INIZIATA ==")
    p3.close()
    msgs1 = drain(p1, 2)  # leave + state
    drain(p2, 2)
    state = next(m["state"] for m in msgs1 if m["type"] == "state")
    check("player kept after disconnect", len(state["players"]) == 3)
    check("marked as disconnected", player_entry(state, p3)["connected"] is False)
    check("phase unchanged", state["turn"]["phase"] == "SETUP")

    print("== RICONNESSIONE CON TOKEN ==")
    p3.open(ROOM, with_session=True)
    m = p3.ws.receive_json()
    check("same playerId on reconnect", m["playerId"] == p3.id)
    check("same token on reconnect", m["token"] == p3.token)
    msgs3 = drain(p3, 2)  # join + state
    drain(p1, 2)
    drain(p2, 2)
    state = next(m["state"] for m in msgs3 if m["type"] == "state")
    check("reconnected", player_entry(state, p3)["connected"] is True)
    check("color preserved", player_entry(state, p3)["color"] == p3.color)

    print("== ACCESSI RIFIUTATI A PARTITA INIZIATA ==")
    with client.websocket_connect(f"/ws/{ROOM}/Intruso?playerId={p1.id}&token=WRONG") as bad:
        m = bad.receive_json()
        check("wrong token rejected", m["type"] == "error" and "cannot join" in m["error"])
    with client.websocket_connect(f"/ws/{ROOM}/Intruso") as fresh:
        m = fresh.receive_json()
        check("fresh join rejected mid-game", m["type"] == "error" and "cannot join" in m["error"])

    print("== DOPPIA CONNESSIONE: il socket nuovo rimpiazza il vecchio ==")
    old_ctx, old_ws = p2.ctx, p2.ws
    p2.ctx, p2.ws = None, None
    p2.open(ROOM, with_session=True)
    m = p2.ws.receive_json()
    check("duplicate connect same identity", m["playerId"] == p2.id)
    msgs2 = drain(p2, 2)
    drain(p1, 2)
    drain(p3, 2)
    state = next(m["state"] for m in msgs2 if m["type"] == "state")
    check("still 3 players", len(state["players"]) == 3)
    check("p2 still connected", player_entry(state, p2)["connected"] is True)
    # chiudi il vecchio socket (era già stato scartato dal server: nessun broadcast)
    old_ctx.__exit__(None, None, None)

    print("== IL GIOCO PROSEGUE DOPO LE RICONNESSIONI ==")
    while state["turn"]["phase"] == "SETUP":
        actor = current_player(state, players)
        free = free_provinces(state)
        payload = {"provinceId": free[0]}
        if len(state["setup"]["neutralPool"]) > 0:
            payload["neutralProvinceId"] = free[1]
        state = send_cmd(actor, players, "setup_claim", payload)
    check("setup completed with reconnected sockets", state["turn"]["phase"] == "SCORE")

    print("== LA PARTITA SOPRAVVIVE ALLA DISCONNESSIONE DI TUTTI ==")
    p1.close()
    drain(p2, 2)
    drain(p3, 2)
    p2.close()
    drain(p3, 2)
    p3.close()

    check("game still in memory", ROOM in server.GAMES)
    check("game phase preserved", server.GAMES[ROOM]["turn"]["phase"] == "SCORE")
    check("all marked disconnected",
          all(pl["connected"] is False for pl in server.GAMES[ROOM]["players"]))

    p1.open(ROOM, with_session=True)
    m = p1.ws.receive_json()
    check("rejoin after total disconnect", m["playerId"] == p1.id)
    msgs1 = drain(p1, 2)
    state = next(m["state"] for m in msgs1 if m["type"] == "state")

    # p1 è il primo giocatore: tocca a lui, può agire subito
    if current_player(state, [p1, p2, p3]).id == p1.id:
        state = send_cmd(p1, [p1], "reinforce_land_begin")
        check("rejoined player can act", state["turn"]["phase"] == "REINFORCE_LAND")
    p1.close()

    print("== LOBBY: chi esce viene rimosso ==")
    ROOM2 = "TEST07"
    q1, q2 = Player("Dino"), Player("Elsa")
    q1.open(ROOM2)
    m = q1.ws.receive_json(); q1.id = m["playerId"]; q1.token = m["token"]
    drain(q1, 2)
    q2.open(ROOM2)
    m = q2.ws.receive_json(); q2.id = m["playerId"]; q2.token = m["token"]
    drain(q2, 2)
    drain(q1, 2)

    q2.close()
    msgs = drain(q1, 2)
    state = next(m["state"] for m in msgs if m["type"] == "state")
    check("lobby leaver removed", len(state["players"]) == 1)
    check("lobby leaver token revoked",
          q2.id not in server.TOKENS.get(ROOM2, {}))
    q1.close()
    check("empty lobby room cleaned up", ROOM2 not in server.GAMES)

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()

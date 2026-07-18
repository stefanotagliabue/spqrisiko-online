"""
Bot giocatori per playtest: si collegano a una stanza e giocano da soli.

Uso:  python bots.py STANZA NomeBot1 NomeBot2 ...

Strategia volutamente semplice ma legale:
- setup: espande vicino alle proprie province, neutrali a caso
- rinforzi: prima le guarnigioni di frontiera a 1 (regola casa), poi il fronte
- tris: giocato appena la mano arriva a 5 carte (senza benefici extra)
- triremi: ne compra una se una provincia costiera ha 6+ legioni
- attacchi: bersaglio più debole adiacente, massimo 6 lanci a turno
- fine turno: se il server impone il riequilibrio garrison-first, esegue
  la mossa suggerita nel messaggio di errore
"""
import asyncio
import json
import random
import re
import sys
import time

import websockets

SERVER = "ws://127.0.0.1:8000"


def log(name, text):
    print(f"[{name}] {text}", flush=True)


class Bot:
    def __init__(self, room, name):
        self.room = room
        self.name = name
        self.pid = None
        self.token = None
        self.last_state = None
        self.turn_key = None       # round|turnIndex: per azzerare i contatori di turno
        self.attacks = 0
        self.did_buy = False
        self.avoid = set()         # bersagli rifiutati dal server in questo turno
        self.last_attack_to = None
        self.errors = 0
        self.last_ready_ts = 0.0   # debounce del toggle "ready"

    # ---------- infrastruttura ----------

    async def run(self):
        while True:
            url = f"{SERVER}/ws/{self.room}/{self.name}"
            if self.pid and self.token:
                url += f"?playerId={self.pid}&token={self.token}"
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    await self.loop(ws)
            except Exception as e:
                log(self.name, f"connessione persa ({e!r}), riprovo fra 3s")
                await asyncio.sleep(3)

    async def cmd(self, ws, cmd, payload=None):
        await ws.send(json.dumps({
            "type": "cmd", "cmd": cmd, "playerId": self.pid, "payload": payload or {}}))

    async def ingest(self, ws, raw):
        """Elabora un messaggio; ritorna lo stato se il messaggio era uno stato."""
        try:
            msg = json.loads(raw)
        except ValueError:
            return None
        t = msg.get("type")
        if t == "welcome":
            self.pid = msg["playerId"]
            self.token = msg["token"]
            log(self.name, f"in stanza {msg['room']} (id {self.pid})")
        elif t == "error":
            await self.on_error(ws, msg.get("error", ""))
        elif t == "state":
            self.errors = 0
            return msg["state"]
        return None

    async def drain(self, ws, timeout=0.05):
        """Consuma i messaggi già in coda e ritorna l'ultimo stato ricevuto."""
        latest = None
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                return latest
            st = await self.ingest(ws, raw)
            if st is not None:
                latest = st

    def is_my_move(self, st):
        me = self.me(st)
        if me is None or me.get("eliminated"):
            return False
        ph = st["turn"]["phase"]
        if ph in ("LOBBY", "GAME_OVER"):
            return False
        return st["players"][st["turn"]["turnIndex"]]["id"] == self.pid

    async def loop(self, ws):
        while True:
            raw = await ws.recv()
            st = await self.ingest(ws, raw)
            newer = await self.drain(ws)
            st = newer or st
            if st is None:
                continue
            self.last_state = st
            if not self.is_my_move(st):
                await self.on_state(ws, st)  # lobby/attesa: gestisce il ready
                continue
            await asyncio.sleep(1.3)  # ritmo umano
            newer = await self.drain(ws)  # nel frattempo può essere cambiato tutto
            if newer is not None:
                st = newer
                self.last_state = st
            if self.is_my_move(st):
                await self.on_state(ws, st)

    # ---------- helper di stato ----------

    def me(self, st):
        return next((p for p in st["players"] if p["id"] == self.pid), None)

    def min_garrison(self, st, pid, color):
        provs = st["map"]["provinces"]
        for nb in provs[pid]["adj_land"]:
            o = provs[nb].get("owner")
            if o is not None and o != color:
                return 2
        return 1

    def deficits(self, st, color):
        provs = st["map"]["provinces"]
        return sorted(
            pid for pid, pr in provs.items()
            if pr.get("owner") == color and int(pr.get("legions", 0)) == 1
            and self.min_garrison(st, pid, color) == 2
        )

    # ---------- logica di gioco ----------

    async def on_state(self, ws, st):
        ph = st["turn"]["phase"]
        me = self.me(st)
        if me is None:
            return

        if ph == "LOBBY":
            # "ready" è un toggle: debounce per non reagire a stati vecchi
            if not me["ready"] and time.time() - self.last_ready_ts > 2:
                self.last_ready_ts = time.time()
                await self.cmd(ws, "ready")
                log(self.name, "pronto")
                return
            # partita di soli bot: il primo in lista fa partire quando sono
            # tutti pronti (con un umano in stanza sara' lui a premere prima)
            if (len(st["players"]) >= 3 and all(p["ready"] for p in st["players"])
                    and st["players"][0]["id"] == self.pid):
                await self.cmd(ws, "start_game")
                log(self.name, "avvio partita")
            return
        if ph == "GAME_OVER":
            if not getattr(self, "_go_logged", False):
                self._go_logged = True
                log(self.name, f"partita finita: vince {st.get('winner')}")
            return
        if me.get("eliminated"):
            return

        cur = st["players"][st["turn"]["turnIndex"]]
        if cur["id"] != self.pid:
            return

        key = f"{st['turn']['round']}|{st['turn']['turnIndex']}"
        if key != self.turn_key:
            self.turn_key = key
            self.attacks = 0
            self.did_buy = False
            self.avoid = set()

        await self.act(ws, st, me)  # la pausa "umana" è già nel loop

    async def act(self, ws, st, me):
        ph = st["turn"]["phase"]
        color = me["color"]
        provs = st["map"]["provinces"]
        mine = {pid: pr for pid, pr in provs.items() if pr.get("owner") == color}

        if ph == "SETUP":
            free = [pid for pid, pr in provs.items() if pr.get("owner") is None]
            if not free:
                return
            near = [f for f in free if any(nb in mine for nb in provs[f]["adj_land"])]
            pick = random.choice(near) if near else random.choice(free)
            payload = {"provinceId": pick}
            if st["setup"]["neutralPool"]:
                rest = [f for f in free if f != pick]
                if rest:
                    payload["neutralProvinceId"] = random.choice(rest)
            log(self.name, f"setup: prendo {pick}")
            await self.cmd(ws, "setup_claim", payload)

        elif ph == "SCORE":
            await self.cmd(ws, "reinforce_land_begin")

        elif ph == "REINFORCE_LAND":
            cards = me.get("cards") or []
            if not st["turn"].get("trisPlayed") and len(cards) >= 5:
                combo = self.find_tris(cards)
                if combo:
                    log(self.name, f"gioco un tris: {[cards[i]['symbol'] for i in combo]}")
                    await self.cmd(ws, "play_tris", {"cards": combo})
                    return
            n = int(st["pending"].get("landReinforceRemaining", 0))
            if n <= 0:
                return
            defs = self.deficits(st, color)
            placements = {}
            if defs and n < len(defs):
                for d in defs[:n]:
                    placements[d] = 1
            else:
                for d in defs:
                    placements[d] = 1
                rest = n - len(defs)
                if rest > 0:
                    front = [pid for pid in mine
                             if any(provs[nb].get("owner") not in (None, color)
                                    for nb in provs[pid]["adj_land"])]
                    target = random.choice(front) if front else random.choice(list(mine))
                    placements[target] = placements.get(target, 0) + rest
            log(self.name, f"rinforzi: {placements}")
            await self.cmd(ws, "reinforce_land_place", {"placements": placements})

        elif ph == "REINFORCE_NAVAL":
            if not self.did_buy:
                fat = next((pid for pid, pr in mine.items()
                            if int(pr.get("legions", 0)) >= 6 and pr.get("adj_sea")), None)
                if fat:
                    self.did_buy = True
                    sea = provs[fat]["adj_sea"][0]
                    log(self.name, f"compro trireme: {fat} -> {sea}")
                    await self.cmd(ws, "buy_trireme", {"provinceId": fat, "seaId": sea})
                    return
            await self.cmd(ws, "end_phase")

        elif ph in ("NAVAL_MOVE", "NAVAL_COMBAT", "SEA_ATTACKS"):
            await self.cmd(ws, "end_phase")

        elif ph == "LAND_ATTACKS":
            target = self.pick_attack(st, color, mine)
            if target and self.attacks < 6:
                f, t = target
                self.attacks += 1
                self.last_attack_to = t
                log(self.name, f"attacco {f} ({provs[f]['legions']}) -> {t} ({provs[t]['legions']})")
                await self.cmd(ws, "land_attack_roll", {"from": f, "to": t})
            else:
                await self.cmd(ws, "end_attacks")

        elif ph == "STRATEGIC_MOVE":
            await self.cmd(ws, "end_turn")

    def find_tris(self, cards):
        by_sym = {}
        for i, c in enumerate(cards):
            by_sym.setdefault(c["symbol"], []).append(i)
        for idxs in by_sym.values():
            if len(idxs) >= 3:
                return idxs[:3]
        if len(by_sym) >= 3:
            return [v[0] for v in list(by_sym.values())[:3]]
        return None

    def pick_attack(self, st, color, mine):
        provs = st["map"]["provinces"]
        best = None
        for pid, pr in mine.items():
            legions = int(pr.get("legions", 0))
            if legions < 4:
                continue
            for nb in pr["adj_land"]:
                o = provs[nb].get("owner")
                if o is None or o == color or nb in self.avoid:
                    continue
                enemy = int(provs[nb].get("legions", 0))
                if enemy < legions - 1:
                    score = legions - enemy
                    if best is None or score > best[0]:
                        best = (score, pid, nb)
        return (best[1], best[2]) if best else None

    # ---------- errori ----------

    async def on_error(self, ws, err):
        log(self.name, f"errore server: {err}")
        st = self.last_state
        if st is None:
            return
        self.errors += 1
        if self.errors > 5:
            await asyncio.sleep(5)
            self.errors = 0

        # garrison-first: il server suggerisce la mossa di riequilibrio
        m = re.search(r"e\.g\.? (\w+) -> (\w+)", err)
        if m:
            log(self.name, f"riequilibrio {m.group(1)} -> {m.group(2)}")
            await self.cmd(ws, "strategic_move",
                           {"from": m.group(1), "to": m.group(2), "count": 1})
            return
        # provincia protetta (§18.4) o attacco rifiutato: evita quel bersaglio
        if "last province" in err and self.last_attack_to:
            self.avoid.add(self.last_attack_to)
            await self.act(ws, st, self.me(st))
            return
        # fallback: chiudi la fase corrente
        ph = st["turn"]["phase"]
        await asyncio.sleep(1)
        if ph == "LAND_ATTACKS":
            await self.cmd(ws, "end_attacks")
        elif ph in ("REINFORCE_NAVAL", "NAVAL_MOVE", "NAVAL_COMBAT", "SEA_ATTACKS"):
            await self.cmd(ws, "end_phase")
        elif ph == "STRATEGIC_MOVE":
            await self.cmd(ws, "end_turn")


async def main():
    room = (sys.argv[1] if len(sys.argv) > 1 else "LUDUS").upper()
    names = sys.argv[2:] or ["Bruto", "Cassio"]
    bots = [Bot(room, n) for n in names]
    print(f"Avvio {len(bots)} bot nella stanza {room}: {', '.join(names)}", flush=True)
    await asyncio.gather(*(b.run() for b in bots))


if __name__ == "__main__":
    asyncio.run(main())

r"""Stanza ferma in SEA_ATTACKS con almeno un attacco via mare possibile,
per provare a mano l'attacco oltremare nel browser.

Uso:  .venv\Scripts\python.exe tests\seed_sea_attack_room.py [CODICE_STANZA]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from spqr.state import GAMES, TOKENS, new_game_state  # noqa: E402
from spqr.persistence import save_room  # noqa: E402

ROOM = (sys.argv[1] if len(sys.argv) > 1 else "MARE").upper()
GAMES[ROOM] = new_game_state(ROOM)
gs = GAMES[ROOM]

for i, (name, col) in enumerate([("Stefano", "RED"), ("Marco", "BLUE"), ("Giulia", "YELLOW")]):
    pid = f"sea{i}"
    gs["players"].append({
        "id": pid, "name": name, "ready": True, "color": col,
        "score": 0, "cards": [], "eliminated": False, "connected": False,
    })
    TOKENS.setdefault(ROOM, {})[pid] = f"tok{i}"

provs = gs["map"]["provinces"]
seas = gs["map"]["seas"]

# tutto a Marco, poi una costa a scacchiera: una provincia costiera su due va a
# Stefano, cosi' da ogni base c'e' quasi sempre un bersaglio nemico oltre il mare
for pr in provs.values():
    pr["owner"] = "BLUE"
    pr["legions"] = 2

costiere = sorted(pid for pid, pr in provs.items() if pr["adj_sea"])
for i, pid in enumerate(costiere):
    if i % 2 == 0:
        provs[pid]["owner"] = "RED"
        provs[pid]["legions"] = 12
    else:
        provs[pid]["legions"] = 3

# cerca coppie (mia provincia costiera, provincia nemica sullo stesso mare)
scenari = []
for pid, pr in provs.items():
    for sid in pr["adj_sea"]:
        for other in seas[sid]["adj_land"]:
            if other != pid:
                scenari.append((pid, other, sid))

# tre basi di partenza distinte, cosi' ci sono piu' attacchi da provare
basi, usati = [], set()
for frm, to, sid in scenari:
    if frm in usati or to in usati:
        continue
    basi.append((frm, to, sid))
    usati.update({frm, to})
    if len(basi) == 3:
        break

for frm, to, sid in basi:
    provs[frm]["owner"] = "RED"
    provs[frm]["legions"] = 12
    provs[to]["owner"] = "BLUE"
    provs[to]["legions"] = 3

# §12.2: serve superiorita' navale stretta nel mare di passaggio. Flotta in
# tutti i mari, non solo nei tre scenari: cosi' si puo' provare l'attacco da
# qualunque costa senza incappare nel rifiuto per parita' di triremi.
for s in seas.values():
    s["triremes"] = {"RED": 3, "BLUE": 1}

# un difensore grosso sull'ultimo scenario: perdite lunghe, sigilli nell'arena
if basi:
    provs[basi[-1][1]]["legions"] = 18
    provs[basi[-1][0]]["legions"] = 24

gs["turn"]["phase"] = "SEA_ATTACKS"
gs["turn"]["turnIndex"] = 0
gs["turn"]["round"] = 3
save_room(ROOM)

print(f"stanza {ROOM}: fase {gs['turn']['phase']}, tocca a Stefano (RED)")
for frm, to, sid in basi:
    print(f"  {frm} ({provs[frm]['legions']}) -> {to} ({provs[to]['legions']}) via {seas[sid]['name']} "
          f"[triremi RED={seas[sid]['triremes']['RED']} BLUE={seas[sid]['triremes']['BLUE']}]")
print("entra come Stefano: playerId=sea0 token=tok0")

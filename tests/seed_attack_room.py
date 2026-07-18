r"""Stanza ferma in LAND_ATTACKS con un attacco possibile, per provare a mano
la freccia d'attacco sulla mappa.

Uso:  .venv\Scripts\python.exe tests\seed_attack_room.py [CODICE_STANZA]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from spqr.state import GAMES, TOKENS, new_game_state  # noqa: E402
from spqr.persistence import save_room  # noqa: E402

ROOM = (sys.argv[1] if len(sys.argv) > 1 else "ATK").upper()
GAMES[ROOM] = new_game_state(ROOM)
gs = GAMES[ROOM]

for i, (name, col) in enumerate([("Stefano", "RED"), ("Marco", "BLUE"), ("Giulia", "YELLOW")]):
    pid = f"atk{i}"
    gs["players"].append({
        "id": pid, "name": name, "ready": True, "color": col,
        "score": 0, "cards": [], "eliminated": False, "connected": False,
    })
    TOKENS.setdefault(ROOM, {})[pid] = f"tok{i}"

# tutte le province a Marco, poi un blocco a Stefano con legioni per attaccare
for pr in gs["map"]["provinces"].values():
    pr["owner"] = "BLUE"
    pr["legions"] = 2
# solo queste a Stefano: i loro confinanti restano di Marco, cosi' ci sono
# bersagli veri da attaccare
for pid in ["ITALIA", "MACEDONIA", "AEGYPTUS"]:
    gs["map"]["provinces"][pid]["owner"] = "RED"
    gs["map"]["provinces"][pid]["legions"] = 8

gs["turn"]["phase"] = "LAND_ATTACKS"
gs["turn"]["turnIndex"] = 0
gs["turn"]["round"] = 3
save_room(ROOM)

print(f"stanza {ROOM}: fase {gs['turn']['phase']}, tocca a Stefano (RED)")
for pid in ["ITALIA", "MACEDONIA", "AEGYPTUS"]:
    pr = gs["map"]["provinces"][pid]
    nemici = [n for n in pr["adj_land"] if gs["map"]["provinces"][n]["owner"] != "RED"]
    print(f"  {pid} ({pr['legions']}) puo' attaccare: {', '.join(nemici) or 'nessuno'}")
print("entra come Stefano: playerId=atk0 token=tok0")

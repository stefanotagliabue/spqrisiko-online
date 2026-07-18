"""Prepara una stanza di prova gia' avviata, con punteggi diversi fra i
giocatori, per controllare a occhio mappa e tracciato punteggi da PC e telefono.

Uso:  .venv\\Scripts\\python.exe tests\\seed_demo_room.py [CODICE_STANZA]
Poi riavviare il server: le stanze salvate vengono ricaricate all'avvio.
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

random.seed(7)

from spqr.state import GAMES, new_game_state  # noqa: E402
from spqr.handlers import (  # noqa: E402
    handle_ready, handle_setup_claim, handle_start_game,
)
from spqr.persistence import room_file, save_room  # noqa: E402

ROOM = (sys.argv[1] if len(sys.argv) > 1 else "DEMO").upper()
NAMES = ["Stefano", "Marco", "Giulia", "Elena"]
SCORES = [12, 7, 7, 0]  # due a pari merito: si impilano sulla stessa casella

GAMES[ROOM] = new_game_state(ROOM)
gs = GAMES[ROOM]
for i, name in enumerate(NAMES):
    gs["players"].append({
        "id": f"seed{i}", "name": name, "ready": False, "color": None,
        "score": 0, "cards": [], "eliminated": False, "connected": False,
    })
    handle_ready(ROOM, f"seed{i}", {})

err = handle_start_game(ROOM, "seed0", {"targetScore": 15})
assert err is None, err

# setup: ogni giocatore pesca a caso fra le province libere finche' la fase finisce
while gs["turn"]["phase"] == "SETUP":
    pid = gs["players"][gs["turn"]["turnIndex"]]["id"]
    free = sorted(p for p, pr in gs["map"]["provinces"].items() if pr["owner"] is None)
    # finche' il pool neutrale non e' vuoto ogni claim ne piazza anche uno
    picks = random.sample(free, 2 if gs["setup"]["neutralPool"] and len(free) > 1 else 1)
    payload = {"provinceId": picks[0]}
    if len(picks) > 1:
        payload["neutralProvinceId"] = picks[1]
    err = handle_setup_claim(ROOM, pid, payload)
    assert err is None, err

for p, s in zip(gs["players"], SCORES):
    p["score"] = s

save_room(ROOM)
print(f"stanza {ROOM} pronta: fase {gs['turn']['phase']}, "
      f"obiettivo {gs['settings']['targetScore']} punti")
for p in gs["players"]:
    print(f"  {p['name']:9s} {p['color']:8s} {p['score']} punti")
print(f"salvata in {room_file(ROOM)}")

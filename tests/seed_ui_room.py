"""Come seed_demo_room, ma lascia la partita a inizio turno di un giocatore
umano, per provare il bottone FINE della console fase per fase.

Uso:  .venv\\Scripts\\python.exe tests\\seed_ui_room.py [CODICE_STANZA]
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

random.seed(11)

from spqr.state import GAMES, TOKENS, new_game_state  # noqa: E402
from spqr.handlers import (  # noqa: E402
    handle_ready, handle_setup_claim, handle_start_game,
)
from spqr.persistence import save_room  # noqa: E402

ROOM = (sys.argv[1] if len(sys.argv) > 1 else "UITEST").upper()
NAMES = ["Stefano", "Marco", "Giulia"]

GAMES[ROOM] = new_game_state(ROOM)
gs = GAMES[ROOM]
for i, name in enumerate(NAMES):
    pid = f"ui{i}"
    gs["players"].append({
        "id": pid, "name": name, "ready": False, "color": None,
        "score": 0, "cards": [], "eliminated": False, "connected": False,
    })
    # token noto: il client puo' entrare come questo giocatore
    TOKENS.setdefault(ROOM, {})[pid] = f"tok{i}"
    handle_ready(ROOM, pid, {})

assert handle_start_game(ROOM, "ui0", {"targetScore": 15}) is None

while gs["turn"]["phase"] == "SETUP":
    pid = gs["players"][gs["turn"]["turnIndex"]]["id"]
    free = sorted(p for p, pr in gs["map"]["provinces"].items() if pr["owner"] is None)
    picks = random.sample(free, 2 if gs["setup"]["neutralPool"] and len(free) > 1 else 1)
    payload = {"provinceId": picks[0]}
    if len(picks) > 1:
        payload["neutralProvinceId"] = picks[1]
    assert handle_setup_claim(ROOM, pid, payload) is None

save_room(ROOM)
p0 = gs["players"][0]
print(f"stanza {ROOM}: fase {gs['turn']['phase']}, tocca a "
      f"{gs['players'][gs['turn']['turnIndex']]['name']}")
print(f"entra come {p0['name']}: playerId={p0['id']} token=tok0")

"""Stato globale del server: costanti di gioco, store delle stanze,
creazione dello stato di partita e utilita' di base.
"""
from typing import Dict, Set, Any, Optional
import copy
import re
import time

from map_data import get_map

# il nome stanza diventa un nome di file: solo caratteri sicuri
ROOM_RE = re.compile(r"^[A-Z0-9_-]{1,16}$")

# --- Multiplayer rooms (connections) ---
ROOMS: Dict[str, Set[Any]] = {}

# --- Game state per room ---
GAMES: Dict[str, Dict[str, Any]] = {}

# --- Session tokens (MAI nel game state: verrebbero broadcastati a tutti) ---
# room -> playerId -> token segreto per riconnettersi
TOKENS: Dict[str, Dict[str, str]] = {}

# --- Socket attivo per giocatore (per rimpiazzare connessioni stantie) ---
# room -> playerId -> WebSocket
PLAYER_WS: Dict[str, Dict[str, Any]] = {}

PHASES = [
    "LOBBY",
    "SETUP",
    "SCORE",
    "REINFORCE_LAND",
    "REINFORCE_NAVAL",
    "NAVAL_MOVE",
    "NAVAL_COMBAT",
    "SEA_ATTACKS",
    "LAND_ATTACKS",
    "STRATEGIC_MOVE",
    "DRAW_CARD",
]

PLAYER_COLORS = ["RED", "BLUE", "YELLOW", "GREEN", "BLACK"]

MAX_POWER_CENTERS = 12   # §1.3
MAX_PLAYERS = 5           # §2.6: si gioca sempre con 5 eserciti
NO_ELIMINATION_ROUNDS = 4  # §18.4


def now_ms() -> int:
    return int(time.time() * 1000)


def new_game_state(room: str) -> Dict[str, Any]:
    base_map = get_map()
    game_map = copy.deepcopy(base_map)  # IMPORTANT: per-room copy

    return {
        "room": room,
        "createdAt": now_ms(),
        "players": [],
        "settings": {
            "targetScore": 15,
        },
        "turn": {
            "turnIndex": 0,
            "round": 1,
            "phase": "LOBBY",
            # flags utili già da ora
            "conqueredThisTurn": False,
            "usedStrategicMove": False,
            # tracking navale per-turno
            "navalCombats": {},          # sea_id -> {"target": color, "closed": bool} (§11.4/11.6)
            "seaAttackedProvinces": [],  # §12.6: non si può ri-attaccare via mare la stessa provincia
            "seaConqueredProvinces": [], # §12.7: da qui niente nuovo attacco via mare nel turno
            "trisPlayed": False,         # §4.4.1: una combinazione di carte per turno
        },
        "setup": {
            "claimedByPlayers": 0,
            "neutralPool": [],
            "neutralFixed": [],
        },
        # azioni multi-step / conteggi fase
        "pending": {
            "landReinforceRemaining": 0,
            # dopo una conquista: {"from": ..., "to": ...} finché il giocatore
            # può ancora spostare legioni extra nella provincia conquistata
            "occupation": None,
        },
        "deck": [],
        "discard": [],
        "map": game_map,
        "log": [],
        "winner": None,
        # ultimo combattimento risolto (per la UI: dadi e perdite)
        "lastCombat": None,
    }


def add_log(room: str, text: str) -> None:
    gs = GAMES[room]
    gs["log"].append({"t": now_ms(), "text": text})
    if len(gs["log"]) > 80:
        gs["log"] = gs["log"][-80:]


def get_player_by_id(gs: Dict[str, Any], player_id: str) -> Optional[Dict[str, Any]]:
    for p in gs["players"]:
        if p["id"] == player_id:
            return p
    return None


def assign_colors(gs: Dict[str, Any]) -> None:
    # assegna colori in ordine di join (MVP)
    for i, p in enumerate(gs["players"]):
        p["color"] = PLAYER_COLORS[i % len(PLAYER_COLORS)]


def init_neutrals(gs: Dict[str, Any]) -> None:
    """
    Neutrali come da regolamento:
    - si usano sempre 5 colori; quelli non usati diventano neutrali
    - 3 giocatori: 2 colori neutrali -> per ciascuno 9 gruppi da 3 (tot 18 gruppi)
    - 4 giocatori: 1 colore neutrale -> 3 in ITALIA + 8 gruppi da 3
    """
    used = {p["color"] for p in gs["players"]}
    unused = [c for c in PLAYER_COLORS if c not in used]

    gs["setup"]["neutralPool"] = []
    gs["setup"]["neutralFixed"] = []

    n_players = len(gs["players"])

    if n_players == 3:
        for c in unused:
            for _ in range(9):
                gs["setup"]["neutralPool"].append({"color": c, "size": 3})

    elif n_players == 4:
        if len(unused) == 1:
            c = unused[0]
            gs["setup"]["neutralFixed"].append({"provinceId": "ITALIA", "color": c, "size": 3})
            for _ in range(8):
                gs["setup"]["neutralPool"].append({"color": c, "size": 3})

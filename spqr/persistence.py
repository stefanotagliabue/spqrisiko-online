"""Persistenza su disco: un file JSON per stanza.

SPQR_DATA_DIR permette ai test di usare una directory isolata.
"""
import json
import os

from .state import GAMES, TOKENS, ROOM_RE, now_ms

DATA_DIR = os.environ.get("SPQR_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "rooms")
os.makedirs(DATA_DIR, exist_ok=True)


def room_file(room: str) -> str:
    return os.path.join(DATA_DIR, f"{room}.json")


def save_room(room: str) -> None:
    """Scrittura atomica dello stato della stanza (partite in corso).
    LOBBY e GAME_OVER non vengono conservate: dopo un riavvio non servono."""
    gs = GAMES.get(room)
    if gs is None:
        return
    if gs["turn"]["phase"] in ("LOBBY", "GAME_OVER"):
        delete_room_file(room)
        return
    tmp = room_file(room) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"game": gs, "tokens": TOKENS.get(room, {}), "savedAt": now_ms()}, f)
        os.replace(tmp, room_file(room))
    except OSError:
        pass  # il gioco continua anche se il disco fallisce


def delete_room_file(room: str) -> None:
    try:
        os.remove(room_file(room))
    except OSError:
        pass


def load_rooms() -> None:
    """Ripristina all'avvio le partite salvate. Le connessioni non
    sopravvivono al riavvio: tutti i giocatori partono disconnessi
    e rientrano con playerId+token."""
    for fn in os.listdir(DATA_DIR):
        if not fn.endswith(".json"):
            continue
        room = fn[:-5]
        if not ROOM_RE.match(room):
            continue
        try:
            with open(room_file(room), "r", encoding="utf-8") as f:
                data = json.load(f)
            gs = data["game"]
            if gs["turn"]["phase"] in ("LOBBY", "GAME_OVER"):
                delete_room_file(room)
                continue
            for p in gs["players"]:
                p["connected"] = False
            GAMES[room] = gs
            TOKENS[room] = data.get("tokens", {})
            gs["log"].append({"t": now_ms(), "text": "Server restarted: game restored from disk"})
        except (OSError, ValueError, KeyError):
            continue  # file corrotto: lo si ignora senza bloccare l'avvio

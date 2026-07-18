"""Comunicazione verso i client: broadcast e viste per-giocatore."""
from typing import Dict, Any

from .state import GAMES, ROOMS, PLAYER_WS, SPECTATOR_WS
from .persistence import save_room


async def broadcast_json_async(room: str, payload: Dict[str, Any]) -> None:
    if room not in ROOMS:
        return
    for peer in list(ROOMS[room]):
        await peer.send_json(payload)


def state_view(gs: Dict[str, Any], viewer_id: str) -> Dict[str, Any]:
    """Vista personalizzata dello stato: le carte degli altri giocatori
    non vengono inviate (solo il conteggio in cardCount).

    viewer_id None = spettatore: non vede le carte di nessuno."""
    view = dict(gs)
    view["players"] = []
    for p in gs["players"]:
        q = dict(p)
        q["cardCount"] = len(p.get("cards") or [])
        if p["id"] != viewer_id:
            q["cards"] = []
        view["players"].append(q)
    return view


async def broadcast_state_async(room: str) -> None:
    gs = GAMES.get(room)
    if gs is None:
        return
    save_room(room)  # ogni cambiamento di stato viene broadcastato: choke point ideale
    active = ROOMS.get(room, set())
    for pid, peer in list(PLAYER_WS.get(room, {}).items()):
        if peer not in active:
            continue
        try:
            await peer.send_json({"type": "state", "room": room, "state": state_view(gs, pid)})
        except Exception:
            pass
    spectator_view = None
    for peer in list(SPECTATOR_WS.get(room, set())):
        if peer not in active:
            continue
        if spectator_view is None:
            spectator_view = state_view(gs, None)  # uguale per tutti: calcolata una volta
        try:
            await peer.send_json({"type": "state", "room": room, "state": spectator_view})
        except Exception:
            pass

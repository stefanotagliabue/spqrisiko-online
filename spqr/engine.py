"""Transizioni di stato che coinvolgono più regole insieme:
eliminazione di un giocatore e chiusura del turno.
"""
from typing import Dict, Any

from .state import GAMES, add_log
from .rules import count_owned_provinces, draw_card, reset_turn_tracking


def check_elimination(room: str, color: str, conqueror: Dict[str, Any]) -> None:
    """
    §18: se un giocatore perde l'ultima provincia viene eliminato;
    chi lo elimina ne acquisisce le carte, le sue triremi escono dal gioco.
    """
    gs = GAMES[room]
    victim = next((p for p in gs["players"] if p.get("color") == color), None)
    if victim is None or victim.get("eliminated"):
        return
    if count_owned_provinces(gs, color) > 0:
        return

    victim["eliminated"] = True
    conqueror["cards"].extend(victim["cards"])
    victim["cards"] = []
    for sea in gs["map"]["seas"].values():
        sea.get("triremes", {}).pop(color, None)
    add_log(room, f"{victim['name']} ELIMINATED by {conqueror['name']}")

    alive = [p for p in gs["players"] if not p.get("eliminated")]
    if len(alive) == 1:
        gs["turn"]["phase"] = "GAME_OVER"
        gs["winner"] = alive[0]["name"]
        add_log(room, f"GAME OVER: {alive[0]['name']} wins by total conquest")


def finish_turn(room: str) -> None:
    """
    Chiude il turno del giocatore corrente: pesca carta se ha conquistato
    (§17.1-17.4), poi passa al prossimo giocatore non eliminato (fase SCORE).
    """
    gs = GAMES[room]
    turn = gs["turn"]
    p = gs["players"][turn["turnIndex"]]

    if turn["conqueredThisTurn"]:
        card = draw_card(gs)
        if card is not None:
            p["cards"].append(card)
            add_log(room, f"{p['name']} draws a card ({len(p['cards'])} in hand)")

    gs["pending"]["occupation"] = None
    gs["pending"]["landReinforceRemaining"] = 0

    n = len(gs["players"])
    for _ in range(n):
        turn["turnIndex"] = (turn["turnIndex"] + 1) % n
        if turn["turnIndex"] == 0:
            turn["round"] += 1
        if not gs["players"][turn["turnIndex"]].get("eliminated"):
            break

    turn["phase"] = "SCORE"
    reset_turn_tracking(turn)
    nxt = gs["players"][turn["turnIndex"]]
    add_log(room, f"Turn -> {nxt['name']} (round {turn['round']})")

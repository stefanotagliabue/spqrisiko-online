"""Entrypoint FastAPI di SPQRisiKo Online.

Qui vivono solo l'app, le route HTTP e il ciclo di vita delle connessioni
WebSocket (join/riconnessione, dispatch dei comandi, disconnessione).
La logica di gioco sta nel pacchetto spqr/.
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import asyncio
import json
import secrets

from map_data import get_map

# Ri-esportazioni: i test (e ogni altro consumatore storico) continuano a
# usare i nomi come attributi di questo modulo, es. server.GAMES.
from spqr.state import (  # noqa: F401
    GAMES, ROOMS, TOKENS, PLAYER_WS, SPECTATOR_WS, ROOM_RE,
    PHASES, PLAYER_COLORS, NEUTRAL_COLOR, MAX_POWER_CENTERS, MAX_PLAYERS, NO_ELIMINATION_ROUNDS,
    now_ms, new_game_state, add_log, get_player_by_id,
    assign_colors, init_neutrals,
)
from spqr.rules import (  # noqa: F401
    normalize_prov_id, roll_dice, resolve_risk_roll, resolve_naval_roll,
    tri_count, remove_triremes, has_sea_superiority, reset_turn_tracking,
    min_garrison, garrison_deficits, find_rebalance_move,
    is_last_province_protected, total_power_centers, can_place_power_center,
    count_owned_provinces, calc_land_reinforcements, build_deck, draw_card,
    largest_empire_size, count_controlled_seas, count_power_centers,
    compute_score_awards,
)
from spqr.persistence import (  # noqa: F401
    DATA_DIR, room_file, save_room, delete_room_file, load_rooms,
)
from spqr.net import broadcast_json_async, state_view, broadcast_state_async  # noqa: F401
from spqr.engine import check_elimination, finish_turn  # noqa: F401
from spqr.handlers import HANDLERS, auto_progress_turn

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True, "service": "spqrisiko-mvp"}


@app.get("/map")
def get_map_debug():
    return get_map()


@app.websocket("/ws/{room}/{player_name}")
async def ws_endpoint(ws: WebSocket, room: str, player_name: str):
    await ws.accept()
    room = room.upper()

    if not ROOM_RE.match(room):
        await ws.send_json({"type": "error", "error": "Invalid room code (use letters, digits, - or _, max 16)"})
        await ws.close(code=4003)
        return

    # --- spettatore: guarda e basta, non entra fra i giocatori ---
    if ws.query_params.get("spectate") == "1":
        if room not in GAMES:
            await ws.send_json({"type": "error", "error": "Stanza inesistente"})
            await ws.close(code=4004)
            return
        ROOMS.setdefault(room, set()).add(ws)
        SPECTATOR_WS.setdefault(room, set()).add(ws)
        await ws.send_json({"type": "welcome", "room": room, "playerId": None,
                            "token": None, "spectator": True})
        await broadcast_state_async(room)
        try:
            while True:
                await ws.receive_text()  # gli spettatori non danno comandi
                await ws.send_json({"type": "error", "error": "Sei spettatore: non puoi giocare"})
        except WebSocketDisconnect:
            ROOMS.get(room, set()).discard(ws)
            SPECTATOR_WS.get(room, set()).discard(ws)
        return

    # create game state if missing
    if room not in GAMES:
        GAMES[room] = new_game_state(room)
    gs = GAMES[room]

    # --- resume di una sessione esistente (riconnessione) ---
    req_pid = ws.query_params.get("playerId")
    req_token = ws.query_params.get("token")

    player = None
    if req_pid and req_token and TOKENS.get(room, {}).get(req_pid) == req_token:
        player = get_player_by_id(gs, req_pid)

    if player is None:
        # nuova registrazione: possibile solo in LOBBY
        if gs["turn"]["phase"] != "LOBBY":
            await ws.send_json({"type": "error", "error": "Game already started: cannot join without a valid session"})
            await ws.close(code=4001)
            return

        player_id = secrets.token_hex(4)
        token = secrets.token_hex(16)
        TOKENS.setdefault(room, {})[player_id] = token
        player = {
            "id": player_id,
            "name": player_name,
            "ready": False,
            "color": None,
            "score": 0,
            "cards": [],
            "eliminated": False,
            "connected": True,
        }
        gs["players"].append(player)
        add_log(room, f"{player_name} joined")
    else:
        # riattacco: chiudi l'eventuale socket precedente rimasto appeso
        player_id = player["id"]
        token = TOKENS[room][player_id]
        old_ws = PLAYER_WS.get(room, {}).get(player_id)
        if old_ws is not None and old_ws is not ws:
            ROOMS.get(room, set()).discard(old_ws)
            try:
                await old_ws.close(code=4002)
            except Exception:
                pass
        player["connected"] = True
        add_log(room, f"{player['name']} reconnected")

    if room not in ROOMS:
        ROOMS[room] = set()
    ROOMS[room].add(ws)
    PLAYER_WS.setdefault(room, {})[player_id] = ws

    # welcome to this client (il token serve al client per riconnettersi)
    await ws.send_json({"type": "welcome", "room": room, "playerId": player_id, "token": token})

    # notify everyone + send state snapshot
    await broadcast_json_async(room, {"type": "join", "room": room, "player": player["name"]})
    await broadcast_state_async(room)

    try:
        while True:
            raw = await ws.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await broadcast_json_async(room, {"type": "msg", "room": room, "player": player_name, "text": raw})
                continue

            if msg.get("type") != "cmd":
                await ws.send_json({"type": "error", "error": "Expected type=cmd"})
                continue

            cmd = msg.get("cmd")
            pid = msg.get("playerId")
            payload = msg.get("payload", {})

            if pid != player_id:
                await ws.send_json({"type": "error", "error": "playerId mismatch"})
                continue

            # --- protect the websocket loop from crashing ---
            try:
                handler = HANDLERS.get(cmd)
                if handler is None:
                    await ws.send_json({"type": "error", "error": f"Unknown cmd: {cmd}"})
                    continue

                error = handler(room, player_id, payload)
                if error is not None:
                    await ws.send_json({"type": "error", "error": error})
                    continue

                auto_progress_turn(room)
                await broadcast_state_async(room)

            except Exception as e:
                add_log(room, f"SERVER EXCEPTION: {type(e).__name__}: {e}")
                await ws.send_json({"type": "error", "error": f"Server exception: {type(e).__name__}: {e}"})
                await broadcast_state_async(room)
                continue

    except WebSocketDisconnect:
        ROOMS.get(room, set()).discard(ws)

        # se questo socket è stato rimpiazzato da una riconnessione più
        # recente, non toccare lo stato del giocatore
        if PLAYER_WS.get(room, {}).get(player_id) is not ws:
            return
        PLAYER_WS[room].pop(player_id, None)

        gs = GAMES.get(room)
        if gs is None:
            return

        p = get_player_by_id(gs, player_id)
        pname = p["name"] if p else player_name

        if gs["turn"]["phase"] == "LOBBY":
            # in lobby chi esce, esce davvero
            gs["players"] = [x for x in gs["players"] if x["id"] != player_id]
            TOKENS.get(room, {}).pop(player_id, None)
            add_log(room, f"{pname} left")
        else:
            # a partita iniziata resta in gioco: può riconnettersi col token
            if p:
                p["connected"] = False
            add_log(room, f"{pname} disconnected (can rejoin)")

        # La notifica agli altri NON deve girare nel task di questo socket:
        # alla chiusura il task viene cancellato e i send in corso andrebbero
        # persi. Un task indipendente sul loop sopravvive alla cancellazione.
        async def _notify_leave() -> None:
            await broadcast_json_async(room, {"type": "leave", "room": room, "player": pname})
            await broadcast_state_async(room)

        asyncio.get_running_loop().create_task(_notify_leave())

        # la room muore solo se vuota E senza una partita in corso
        if len(ROOMS.get(room, set())) == 0 and gs["turn"]["phase"] in ("LOBBY", "GAME_OVER"):
            ROOMS.pop(room, None)
            GAMES.pop(room, None)
            TOKENS.pop(room, None)
            PLAYER_WS.pop(room, None)
            delete_room_file(room)


# ripristina le partite salvate prima di accettare connessioni
load_rooms()

# web client
app.mount("/", StaticFiles(directory="static", html=True), name="static")

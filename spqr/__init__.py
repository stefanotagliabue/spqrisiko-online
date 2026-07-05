"""Pacchetto SPQRisiKo Online: motore di gioco, rete e persistenza.

Struttura a livelli (nessun import circolare):
    state -> rules -> persistence -> net
    state -> rules -> engine -> handlers
`server.py` in radice resta l'entrypoint FastAPI e ri-esporta i nomi
storici per compatibilita' con i test.
"""

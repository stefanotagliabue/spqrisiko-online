"""Ricava static/board.jpg dalla foto della plancia reale.

La foto sorgente sta in assets/ (fuori da static/: e' un file da 4 MB che non
va servito ai client). E' scattata in verticale, quindi va ruotata di 90 gradi
in senso antiorario per avere l'orientamento del gioco, col tracciato punteggi
in fondo.

I numeri di ritaglio sono tarati in modo che la porzione MAPPA finisca esatta
nel rettangolo 1240x700: e' quello il sistema di coordinate di PROV_XY/SEA_XY
in static/index.html. Cambiarli significa dover ritarare tutti i gettoni.

Uso:  .venv\\Scripts\\python.exe tools\\build_board.py
"""
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "plancia-v2.jpeg")
DST = os.path.join(ROOT, "static", "board.jpg")

# ritaglio sulla foto ruotata (3971x2901), bordo di legno escluso
CROP_L, CROP_T, CROP_R = 37, 136, 3951
MAP_BOTTOM = 2218   # dove finisce la mappa e inizia la cornice del tracciato
CROP_B = 2870       # fondo del tracciato punteggi

OUT_W, MAP_H = 1240, 700  # la mappa deve stare esatta in 1240x700


def main() -> None:
    im = Image.open(SRC).rotate(90, expand=True)
    # la scala verticale la impone la mappa: il tracciato la eredita
    scale = MAP_H / (MAP_BOTTOM - CROP_T)
    out_h = round((CROP_B - CROP_T) * scale)
    board = im.crop((CROP_L, CROP_T, CROP_R, CROP_B)).resize((OUT_W, out_h), Image.LANCZOS)
    board.save(DST, quality=88, optimize=True)
    print(f"{DST}: {board.size[0]}x{board.size[1]} "
          f"(mappa 0..{MAP_H}, tracciato {MAP_H}..{out_h})")


if __name__ == "__main__":
    main()

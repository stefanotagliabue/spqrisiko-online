"""Regole di gioco pure: dadi, combattimenti, guarnigioni, rinforzi,
mazzo di carte e punteggi. Nessuna funzione qui tocca gli store globali:
tutto lavora sullo stato di partita passato come argomento.
"""
from typing import Dict, Any, Optional
import random

from .state import NO_ELIMINATION_ROUNDS, MAX_POWER_CENTERS


def normalize_prov_id(raw) -> str:
    """
    Normalizza un provinceId dal payload:
    - None -> ""
    - strip
    - upper
    """
    if raw is None:
        return ""
    return str(raw).strip().upper()


def roll_dice(n: int) -> list[int]:
    """
    Lancia n dadi a 6 facce e ritorna la lista ordinata in modo decrescente.
    """
    return sorted([random.randint(1, 6) for _ in range(n)], reverse=True)


def resolve_risk_roll(att: list[int], deff: list[int]) -> tuple[int, int]:
    """
    Risolve un singolo lancio di dadi stile Risiko/SPQRisiKo.
    Ritorna: (perdite_attaccante, perdite_difensore)
    - i dadi sono già ordinati in modo decrescente
    - a parità vince il difensore
    """
    losses_att = 0
    losses_def = 0

    n = min(len(att), len(deff))
    for i in range(n):
        if att[i] > deff[i]:
            losses_def += 1
        else:
            losses_att += 1

    return losses_att, losses_def


def resolve_naval_roll(att: list[int], deff: list[int]) -> tuple[int, int]:
    """
    §11.5: nel combattimento navale, in caso di parità non perde nessuno.
    Ritorna: (perdite_attaccante, perdite_difensore)
    """
    losses_att = 0
    losses_def = 0
    for i in range(min(len(att), len(deff))):
        if att[i] > deff[i]:
            losses_def += 1
        elif deff[i] > att[i]:
            losses_att += 1
    return losses_att, losses_def


def tri_count(sea: Dict[str, Any], color: str) -> int:
    return int(sea.get("triremes", {}).get(color, 0))


def remove_triremes(sea: Dict[str, Any], color: str, n: int) -> None:
    tri = sea.setdefault("triremes", {})
    tri[color] = tri.get(color, 0) - n
    if tri[color] <= 0:
        tri.pop(color, None)


def has_sea_superiority(sea: Dict[str, Any], color: str) -> bool:
    """§15.2: strettamente più triremi di ogni altro giocatore presente nel mare."""
    tri = sea.get("triremes", {})
    mine = tri.get(color, 0)
    return mine > 0 and all(v < mine for c, v in tri.items() if c != color)


def reset_turn_tracking(turn: Dict[str, Any]) -> None:
    turn["conqueredThisTurn"] = False
    turn["usedStrategicMove"] = False
    turn["navalCombats"] = {}
    turn["seaAttackedProvinces"] = []
    turn["seaConqueredProvinces"] = []
    turn["trisPlayed"] = False


def min_garrison(gs: Dict[str, Any], prov_id: str, color: str) -> int:
    """
    §14.2: dopo uno spostamento volontario devono restare almeno 2 legioni
    in una provincia confinante via terra con una provincia nemica
    (contiamo anche i neutrali: non attaccano, ma il testo non li esclude).
    Altrimenti basta la guarnigione minima di 1 (§14.1).
    """
    provs = gs["map"]["provinces"]
    for nb in provs[prov_id].get("adj_land", []):
        owner = provs[nb].get("owner")
        if owner is not None and owner != color:
            return 2
    return 1


def garrison_deficits(gs: Dict[str, Any], color: str) -> list[str]:
    """
    Regola casa "garrison-first": province di color a 1 legione con almeno
    un vicino terrestre nemico (neutrali inclusi).
    """
    return sorted(
        pid for pid, pr in gs["map"]["provinces"].items()
        if pr.get("owner") == color and int(pr.get("legions", 0)) == 1
        and min_garrison(gs, pid, color) == 2
    )


def find_rebalance_move(gs: Dict[str, Any], color: str, deficits: list[str]) -> Optional[Dict[str, str]]:
    """
    Cerca uno spostamento strategico (from, to) che porti a 2 una provincia in
    deficit. Usa esattamente i criteri di validazione di strategic_move (rotta
    via terra o via mare con superiorità navale §15.2, guarnigione minima §14.2
    sulla donatrice), così la mossa restituita è sempre eseguibile.
    """
    provs = gs["map"]["provinces"]
    for d in deficits:
        d_seas = set(provs[d].get("adj_sea", []))
        for q, pq in provs.items():
            if q == d or pq.get("owner") != color:
                continue
            if int(pq.get("legions", 0)) - min_garrison(gs, q, color) < 1:
                continue
            route = d in pq.get("adj_land", []) or any(
                sid in d_seas and has_sea_superiority(gs["map"]["seas"][sid], color)
                for sid in pq.get("adj_sea", [])
            )
            if route:
                return {"from": q, "to": d}
    return None


def is_last_province_protected(gs: Dict[str, Any], defender_color: Optional[str]) -> bool:
    """§18.4: nessuno può essere eliminato prima della fine del 4° round."""
    if not defender_color or str(defender_color).startswith("NEUTRAL_"):
        return False
    if gs["turn"]["round"] > NO_ELIMINATION_ROUNDS:
        return False
    return count_owned_provinces(gs, defender_color) == 1


def total_power_centers(gs: Dict[str, Any]) -> int:
    return sum(1 for p in gs["map"]["provinces"].values() if p.get("power_center"))


def can_place_power_center(gs: Dict[str, Any], color: str, prov_id: str) -> tuple[bool, str]:
    """§16.4: provincia propria, senza centro, senza centri nelle confinanti via terra."""
    provs = gs["map"]["provinces"]
    if prov_id not in provs:
        return False, f"Invalid provinceId: {prov_id}"
    prov = provs[prov_id]
    if prov.get("owner") != color:
        return False, f"You do not own {prov_id}"
    if prov.get("power_center"):
        return False, f"{prov_id} already has a power center"
    for nb in prov.get("adj_land", []):
        if provs[nb].get("power_center"):
            return False, f"Adjacent province {nb} has a power center (§16.4)"
    if total_power_centers(gs) >= MAX_POWER_CENTERS:
        return False, "All 12 power centers are already in play"
    return True, ""


def count_owned_provinces(gs: Dict[str, Any], owner_color: str) -> int:
    n = 0
    for prov in gs["map"]["provinces"].values():
        if prov.get("owner") == owner_color:
            n += 1
    return n


def calc_land_reinforcements(gs: Dict[str, Any], owner_color: str) -> int:
    """
    Regole base regolamento §6:
    - < 3 province -> 1
    - 3..11 -> 3
    - > 11 -> floor(province/3)
    (Centri di Potere: verranno aggiunti più avanti)
    """
    nprov = count_owned_provinces(gs, owner_color)
    if nprov < 3:
        return 1
    if 3 <= nprov <= 11:
        return 3
    return nprov // 3


def build_deck() -> list[dict]:
    """
    Mazzo di 55 carte (§1.6). La distribuzione esatta dei simboli non è
    indicata nel regolamento: usiamo 14/14/14/13 come approssimazione.
    """
    symbols = (
        ["LEGIONARIO"] * 14
        + ["TRIREME"] * 14
        + ["VESSILLO"] * 14
        + ["ARENA"] * 13
    )
    random.shuffle(symbols)
    return [{"symbol": s} for s in symbols]


def draw_card(gs: Dict[str, Any]) -> Optional[dict]:
    if not gs["deck"] and gs["discard"]:
        random.shuffle(gs["discard"])
        gs["deck"] = gs["discard"]
        gs["discard"] = []
    if not gs["deck"]:
        return None
    return gs["deck"].pop()


def largest_empire_size(gs: Dict[str, Any], color: str) -> int:
    """
    §5.6.1: dimensione del più grande insieme di province del giocatore
    collegate fra loro via terra (le isole non contano come collegate).
    """
    provs = gs["map"]["provinces"]
    owned = {pid for pid, p in provs.items() if p.get("owner") == color}
    best = 0
    seen: set = set()
    for start in owned:
        if start in seen:
            continue
        size = 0
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            size += 1
            for nb in provs[cur]["adj_land"]:
                if nb in owned and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        best = max(best, size)
    return best


def count_controlled_seas(gs: Dict[str, Any], color: str) -> int:
    """§5.6.3: un mare è controllato da chi vi ha strettamente più triremi."""
    n = 0
    for sea in gs["map"]["seas"].values():
        tri = sea.get("triremes", {})
        mine = tri.get(color, 0)
        if mine > 0 and all(v < mine for c, v in tri.items() if c != color):
            n += 1
    return n


def count_power_centers(gs: Dict[str, Any], color: str) -> int:
    return sum(
        1
        for p in gs["map"]["provinces"].values()
        if p.get("owner") == color and p.get("power_center")
    )


def compute_score_awards(gs: Dict[str, Any], color: str) -> tuple[int, list[str]]:
    """
    §5.6: calcola i Punti Vittoria spettanti al giocatore di turno.
    Ritorna (punti, dettagli) — i pareggi non assegnano punti.
    """
    points = 0
    details: list[str] = []
    other_colors = [p["color"] for p in gs["players"] if p["color"] != color]

    # 5.6.1 impero maggiore (>= 4 province, strettamente il più grande)
    my_empire = largest_empire_size(gs, color)
    best_other_empire = max(
        (largest_empire_size(gs, c) for c in other_colors), default=0
    )
    if my_empire >= 4 and my_empire > best_other_empire:
        points += 1
        details.append(f"largest empire ({my_empire})")

    # 5.6.2 maggior numero di province (strettamente)
    my_provs = count_owned_provinces(gs, color)
    best_other_provs = max(
        (count_owned_provinces(gs, c) for c in other_colors), default=0
    )
    if my_provs > best_other_provs:
        points += 1
        details.append(f"most provinces ({my_provs})")

    # 5.6.3 controllo dei mari (strettamente)
    my_seas = count_controlled_seas(gs, color)
    best_other_seas = max(
        (count_controlled_seas(gs, c) for c in other_colors), default=0
    )
    if my_seas > 0 and my_seas > best_other_seas:
        points += 1
        details.append(f"sea control ({my_seas})")

    # 5.6.4 centri di potere (1 VP ciascuno)
    pc = count_power_centers(gs, color)
    if pc > 0:
        points += pc
        details.append(f"power centers ({pc})")

    return points, details

import sys
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server

gs = server.new_game_state("T")
gs["players"] = [
    {"id": "a", "name": "A", "color": "RED", "score": 0, "cards": [], "eliminated": False},
    {"id": "b", "name": "B", "color": "BLUE", "score": 0, "cards": [], "eliminated": False},
]
provs = gs["map"]["provinces"]

# RED: impero connesso di 5 (Hispania+Aquitania) + piu' province totali (6)
for pid in ["LUSITANIA", "GALICIA", "BAETICA", "TERRACONENSIS", "AQUITANIA", "CRETA"]:
    provs[pid]["owner"] = "RED"; provs[pid]["legions"] = 2
# BLUE: 3 province, impero max 2 (ITALIA-CISALPINA adiacenti)
for pid in ["ITALIA", "CISALPINA", "AEGYPTUS"]:
    provs[pid]["owner"] = "BLUE"; provs[pid]["legions"] = 2

# mari: RED controlla TYRRENUM (2 vs 1), pareggio su AEGEUM (non conta)
gs["map"]["seas"]["TYRRENUM"]["triremes"] = {"RED": 2, "BLUE": 1}
gs["map"]["seas"]["AEGEUM"]["triremes"] = {"RED": 1, "BLUE": 1}

# centro di potere per RED
provs["CRETA"]["power_center"] = True

pts, det = server.compute_score_awards(gs, "RED")
print("RED:", pts, det)
assert pts == 4, det  # impero(5) + province(6) + mari(1) + centro(1)

pts, det = server.compute_score_awards(gs, "BLUE")
print("BLUE:", pts, det)
assert pts == 0, det  # nessun primato; pareggio sui mari non conta

assert server.largest_empire_size(gs, "RED") == 5
assert server.largest_empire_size(gs, "BLUE") == 2
assert server.count_controlled_seas(gs, "RED") == 1
assert server.count_controlled_seas(gs, "BLUE") == 0

# caso: impero maggiore ma sotto soglia 4 -> niente VP impero
gs2 = server.new_game_state("T2")
gs2["players"] = gs["players"]
p2 = gs2["map"]["provinces"]
for pid in ["ITALIA", "CISALPINA", "RAETIA"]:
    p2[pid]["owner"] = "RED"; p2[pid]["legions"] = 2
p2["AEGYPTUS"]["owner"] = "BLUE"; p2["AEGYPTUS"]["legions"] = 2
pts, det = server.compute_score_awards(gs2, "RED")
print("RED small empire:", pts, det)
assert "empire" not in " ".join(det), det  # impero di 3 < soglia 4
assert pts == 1, det  # solo most provinces

print("SCORING OK")



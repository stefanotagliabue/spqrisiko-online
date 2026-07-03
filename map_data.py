# map_data.py
from typing import Dict, Any

MAP_ID = "spqrisiko_base_v1"

def P(name: str) -> Dict[str, Any]:
    return {
        "type": "land",
        "name": name,
        "owner": None,
        "legions": 0,
        "power_center": False,
        "adj_land": [],
        "adj_sea": [],
    }

def S(name: str) -> Dict[str, Any]:
    return {
        "type": "sea",
        "name": name,
        "owner": None,
        "triremes": {},
        "adj_sea": [],
        "adj_land": [],
    }

def add_land_edge(provinces: Dict[str, Any], a: str, b: str) -> None:
    if a not in provinces:
        raise ValueError(f"Unknown province in land edge: {a}")
    if b not in provinces:
        raise ValueError(f"Unknown province in land edge: {b}")
    if b not in provinces[a]["adj_land"]:
        provinces[a]["adj_land"].append(b)
    if a not in provinces[b]["adj_land"]:
        provinces[b]["adj_land"].append(a)

def add_coast_edge(provinces: Dict[str, Any], seas: Dict[str, Any], prov_id: str, sea_id: str) -> None:
    if prov_id not in provinces:
        raise ValueError(f"Unknown province in coast edge: {prov_id}")
    if sea_id not in seas:
        raise ValueError(f"Unknown sea in coast edge: {sea_id}")

    if sea_id not in provinces[prov_id]["adj_sea"]:
        provinces[prov_id]["adj_sea"].append(sea_id)
    if prov_id not in seas[sea_id]["adj_land"]:
        seas[sea_id]["adj_land"].append(prov_id)

def add_sea_edge(seas: Dict[str, Any], a: str, b: str) -> None:
    if a not in seas:
        raise ValueError(f"Unknown sea in sea edge: {a}")
    if b not in seas:
        raise ValueError(f"Unknown sea in sea edge: {b}")

    if b not in seas[a]["adj_sea"]:
        seas[a]["adj_sea"].append(b)
    if a not in seas[b]["adj_sea"]:
        seas[b]["adj_sea"].append(a)

def validate_sea_symmetry(seas: Dict[str, Any]) -> None:
    for a, sa in seas.items():
        for b in sa["adj_sea"]:
            if a not in seas[b]["adj_sea"]:
                raise ValueError(f"Sea adjacency not symmetric: {a} -> {b} but not {b} -> {a}")


def validate_symmetry(provinces: Dict[str, Any]) -> None:
    for a, pa in provinces.items():
        for b in pa["adj_land"]:
            if a not in provinces[b]["adj_land"]:
                raise ValueError(f"Adjacency not symmetric: {a} -> {b} but not {b} -> {a}")


def validate_map(m: Dict[str, Any]) -> None:
    for k in list(m["provinces"].keys()) + list(m["seas"].keys()):
        if k != k.upper() or " " in k:
            raise ValueError(f"Bad id key: {k}")

def get_map() -> Dict[str, Any]:
    provinces = {
        # Hispania
        "LUSITANIA": P("Lusitania"),
        "GALICIA": P("Galicia"),
        "BAETICA": P("Baetica"),
        "TERRACONENSIS": P("Terraconensis"),

        # Gallia / Nord
        "AQUITANIA": P("Aquitania"),
        "NARBONENSIS": P("Narbonensis"),
        "LUGDUNENSE": P("Lugdunense"),
        "BELGICA": P("Belgica"),
        "GERMANIA_INFERIOR": P("Germania Inferior"),
        "BRITANNIA": P("Britannia"),

        # Danubio / Balcani
        "RAETIA": P("Raetia"),
        "NORICUM": P("Noricum"),
        "PANNONIA": P("Pannonia"),
        "DALMAZIA": P("Dalmazia"),
        "ILLIRIA": P("Illiria"),
        "MOESIA": P("Moesia"),
        "DACIA": P("Dacia"),

        # Italia e isole occ.
        "CISALPINA": P("Cisalpina"),
        "ITALIA": P("Italia"),
        "CORSICA": P("Corsica"),
        "SARDINIA": P("Sardinia"),
        "SICILIA": P("Sicilia"),
        "BALEARES": P("Baleares"),

        # Grecia / Egeo
        "EPIRUS": P("Epirus"),
        "MACEDONIA": P("Macedonia"),
        "THRACIA": P("Thracia"),
        "ACHAIA": P("Achaia"),
        "CRETA": P("Creta"),

        # Asia Minore
        "ASIA": P("Asia"),
        "BITHYNIA": P("Bithynia"),
        "PONTO": P("Ponto"),
        "GALATIA": P("Galatia"),
        "CAPPADOCIA": P("Cappadocia"),
        "CILICIA": P("Cilicia"),
        "CIPRO": P("Cipro"),

        # Oriente
        "SYRIA": P("Syria"),
        "JUDEA": P("Judea"),
        "ARABIA": P("Arabia"),
        "ARMENIA": P("Armenia"),
        "MESOPOTAMIA": P("Mesopotamia"),

        # Africa
        "MAURETANIA": P("Mauretania"),
        "NUMIDIA": P("Numidia"),
        "AFRICA": P("Africa"),
        "CYRENAICA": P("Cyrenaica"),
        "AEGYPTUS": P("Aegyptus"),
    }

        # --- ADIACENZE TERRESTRI (tranche 1: Italia + Alpino/Danubio + Balcani/Grecia) ---

    LAND_EDGES = [

    # =========================
    # ITALIA / ARCO ALPINO
    # =========================
    ("ITALIA", "CISALPINA"),

    ("CISALPINA", "NARBONENSIS"),
    ("CISALPINA", "LUGDUNENSE"),
    ("CISALPINA", "RAETIA"),
    ("CISALPINA", "NORICUM"),
    ("CISALPINA", "ILLIRIA"),

    ("RAETIA", "LUGDUNENSE"),
    ("RAETIA", "BELGICA"),
    ("RAETIA", "GERMANIA_INFERIOR"),
    ("RAETIA", "NORICUM"),

    ("NORICUM", "PANNONIA"),
    ("NORICUM", "DALMAZIA"),
    ("NORICUM", "ILLIRIA"),

    # =========================
    # BALCANI / GRECIA
    # =========================
    ("ILLIRIA", "DALMAZIA"),
    ("ILLIRIA", "EPIRUS"),
    ("ILLIRIA", "MACEDONIA"),

    ("DALMAZIA", "PANNONIA"),
    ("DALMAZIA", "MACEDONIA"),
    ("DALMAZIA", "THRACIA"),

    ("PANNONIA", "MOESIA"),
    ("PANNONIA", "DACIA"),
    ("PANNONIA", "THRACIA"),

    ("MOESIA", "DACIA"),
    ("MOESIA", "THRACIA"),

    ("EPIRUS", "MACEDONIA"),
    ("EPIRUS", "ACHAIA"),

    ("MACEDONIA", "THRACIA"),
    ("MACEDONIA", "ACHAIA"),

    # =========================
    # HISPANIA
    # =========================
    ("LUSITANIA", "GALICIA"),
    ("LUSITANIA", "BAETICA"),
    ("LUSITANIA", "TERRACONENSIS"),

    ("GALICIA", "TERRACONENSIS"),

    ("BAETICA", "TERRACONENSIS"),

    ("TERRACONENSIS", "NARBONENSIS"),
    ("TERRACONENSIS", "AQUITANIA"),

    # =========================
    # GALLIA / BRITANNIA / GERMANIA
    # =========================
    ("AQUITANIA", "NARBONENSIS"),
    ("AQUITANIA", "LUGDUNENSE"),

    ("NARBONENSIS", "LUGDUNENSE"),

    ("LUGDUNENSE", "BELGICA"),

    ("BELGICA", "GERMANIA_INFERIOR"),

    # =========================
    # AFRICA
    # =========================
    ("MAURETANIA", "NUMIDIA"),
    ("NUMIDIA", "AFRICA"),
    ("NUMIDIA", "CYRENAICA"),
    ("CYRENAICA", "AEGYPTUS"),

    # =========================
    # ASIA MINORE / LEVANTE
    # =========================
    ("ASIA", "BITHYNIA"),
    ("ASIA", "GALATIA"),
    ("ASIA", "CILICIA"),

    ("BITHYNIA", "PONTO"),
    ("BITHYNIA", "GALATIA"),
    ("BITHYNIA", "CAPPADOCIA"),

    ("PONTO", "CAPPADOCIA"),
    ("PONTO", "ARMENIA"),

    ("GALATIA", "CAPPADOCIA"),
    ("GALATIA", "CILICIA"),

    ("CAPPADOCIA", "CILICIA"),
    ("CAPPADOCIA", "ARMENIA"),
    ("CAPPADOCIA", "SYRIA"),
    ("CAPPADOCIA", "MESOPOTAMIA"),

    ("SYRIA", "JUDEA"),
    ("SYRIA", "MESOPOTAMIA"),

    ("JUDEA", "ARABIA"),
    ("JUDEA", "AEGYPTUS"),

    ("ARABIA", "MESOPOTAMIA"),

    ("ARMENIA", "MESOPOTAMIA"),
    ]


    for a, b in LAND_EDGES:
        add_land_edge(provinces, a, b)

    validate_symmetry(provinces)

    seas = {
    "IBERICUM": S("Ibericum"),
    "CANTABRICUM": S("Cantabricum"),
    "BALEARICUM": S("Balearicum"),
    "BRITANNICUS": S("Britannicus"),
    "SINUS_GALLICUS": S("Sinus Gallicus"),
    "TYRRENUM": S("Tyrrenum"),
    "HADRIATICUM": S("Hadriaticum"),
    "IONIUM": S("Ionium"),
    "AEGEUM": S("Aegeum"),
    "CRETICUM": S("Creticum"),
    "PONTUS_EUXINUS": S("Pontus Euxinus"),
    "SYRTIS": S("Syrtis"),
    }

    # =========================
    # ADIACENZE COSTIERE (ordinate per MARE)
    # =========================

    COAST_EDGES = [

        # =========================
        # TYRRENUM
        # =========================
        ("ITALIA", "TYRRENUM"),
        ("SICILIA", "TYRRENUM"),
        ("CORSICA", "TYRRENUM"),
        ("SARDINIA", "TYRRENUM"),
        ("CISALPINA", "TYRRENUM"),
        ("AFRICA", "TYRRENUM"),

        # =========================
        # SINUS_GALLICUS
        # =========================
        ("CORSICA", "SINUS_GALLICUS"),
        ("SARDINIA", "SINUS_GALLICUS"),
        ("CISALPINA", "SINUS_GALLICUS"),
        ("NARBONENSIS", "SINUS_GALLICUS"),
        ("TERRACONENSIS", "SINUS_GALLICUS"),
        ("BALEARES", "SINUS_GALLICUS"),

        # =========================
        # BALEARICUM
        # =========================
        ("SARDINIA", "BALEARICUM"),
        ("BALEARES", "BALEARICUM"),
        ("AFRICA", "BALEARICUM"),
        ("NUMIDIA", "BALEARICUM"),
        ("MAURETANIA", "BALEARICUM"),
        ("BAETICA", "BALEARICUM"),
        ("TERRACONENSIS", "BALEARICUM"),

        # =========================
        # HADRIATICUM
        # =========================
        ("ITALIA", "HADRIATICUM"),
        ("EPIRUS", "HADRIATICUM"),
        ("CISALPINA", "HADRIATICUM"),
        ("ILLIRIA", "HADRIATICUM"),

        # =========================
        # IONIUM
        # =========================
        ("ITALIA", "IONIUM"),
        ("SICILIA", "IONIUM"),
        ("EPIRUS", "IONIUM"),
        ("ACHAIA", "IONIUM"),
        ("CRETA", "IONIUM"),
        ("CYRENAICA", "IONIUM"),

        # =========================
        # AEGEUM
        # =========================
        ("ACHAIA", "AEGEUM"),
        ("MACEDONIA", "AEGEUM"),
        ("THRACIA", "AEGEUM"),
        ("CRETA", "AEGEUM"),
        ("CIPRO", "AEGEUM"),
        ("ASIA", "AEGEUM"),
        ("CILICIA", "AEGEUM"),

        # =========================
        # CRETICUM
        # =========================
        ("CRETA", "CRETICUM"),
        ("CIPRO", "CRETICUM"),
        ("CILICIA", "CRETICUM"),
        ("CAPPADOCIA", "CRETICUM"),
        ("SYRIA", "CRETICUM"),
        ("JUDEA", "CRETICUM"),
        ("AEGYPTUS", "CRETICUM"),
        ("CYRENAICA", "CRETICUM"),

        # =========================
        # PONTUS_EUXINUS
        # =========================
        ("THRACIA", "PONTUS_EUXINUS"),
        ("MOESIA", "PONTUS_EUXINUS"),
        ("DACIA", "PONTUS_EUXINUS"),
        ("BITHYNIA", "PONTUS_EUXINUS"),
        ("PONTO", "PONTUS_EUXINUS"),
        ("ASIA", "PONTUS_EUXINUS"),

        # =========================
        # SYRTIS
        # =========================
        ("SICILIA", "SYRTIS"),
        ("AFRICA", "SYRTIS"),
        ("NUMIDIA", "SYRTIS"),
        ("CYRENAICA", "SYRTIS"),
        
        
        # =========================
        # IBERICUM
        # =========================
        ("MAURETANIA", "IBERICUM"),
        ("BAETICA", "IBERICUM"),
        ("LUSITANIA", "IBERICUM"),


        # =========================
        # CANTABRICUM
        # =========================
        ("GALICIA", "CANTABRICUM"),
        ("AQUITANIA", "CANTABRICUM"),
        ("LUSITANIA", "CANTABRICUM"),
        ("LUGDUNENSE", "CANTABRICUM"),


        # =========================
        # BRITANNICUS
        # =========================
        ("BRITANNIA", "BRITANNICUS"),
        ("GERMANIA_INFERIOR", "BRITANNICUS"),
        ("BELGICA", "BRITANNICUS"),
        ("LUGDUNENSE", "BRITANNICUS")
        
    ]


    SEA_EDGES = [
        # collegamenti tra mari centrali/orientali (coerenti con la plancia in quella zona)
        ("HADRIATICUM", "IONIUM"),
        ("IONIUM", "AEGEUM"),
        ("IONIUM", "CRETICUM"),
        ("IONIUM", "SYRTIS"),
        ("IONIUM", "TYRRENUM"),
        ("TYRRENUM", "SYRTIS"),
        ("TYRRENUM", "SINUS_GALLICUS"),
        ("TYRRENUM", "BALEARICUM"),
        ("BALEARICUM", "SINUS_GALLICUS"),
        ("BALEARICUM", "IBERICUM"),
        ("CANTABRICUM", "IBERICUM"),
        ("CANTABRICUM", "BRITANNICUS"),
        ("AEGEUM", "CRETICUM"),
        ("AEGEUM", "PONTUS_EUXINUS"),
    ]

    for prov_id, sea_id in COAST_EDGES:
        add_coast_edge(provinces, seas, prov_id, sea_id)

    for a, b in SEA_EDGES:
        add_sea_edge(seas, a, b)

    validate_sea_symmetry(seas)




    m = {"mapId": MAP_ID, "provinces": provinces, "seas": seas}
    validate_map(m)
    return m

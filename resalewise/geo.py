"""Geospatial helpers shared by the pipeline scripts and their tests.

Pure functions only (no database access), so the matching rules can be unit
tested without OneMap or a database.
"""
import difflib
import math
import re

# Singapore's bounding box (validation check V2).
SG_BBOX = {"lat": (1.15, 1.48), "lon": (103.6, 104.1)}

# Radii used by each engine. MongoDB's spherical geometry uses 6378.1 km;
# the haversine reference uses the IUGG mean radius; PostGIS geography uses
# the WGS84 spheroid by default (or a 6371008.8 m sphere with use_spheroid=false).
R_MONGO = 6378100.0
R_MEAN = 6371008.8

ABBREVIATIONS = {
    "AVE": "AVENUE", "AV": "AVENUE", "ST": "STREET", "RD": "ROAD", "DR": "DRIVE",
    "CRES": "CRESCENT", "CTRL": "CENTRAL", "NTH": "NORTH", "STH": "SOUTH",
    "UPP": "UPPER", "LOR": "LORONG", "BT": "BUKIT", "JLN": "JALAN", "PK": "PARK",
    "PL": "PLACE", "CL": "CLOSE", "TER": "TERRACE", "GDNS": "GARDENS", "HTS": "HEIGHTS",
    "CTR": "CENTRE", "KG": "KAMPONG", "TG": "TANJONG", "MKT": "MARKET", "STN": "STATION",
    "C'WEALTH": "COMMONWEALTH", "CWEALTH": "COMMONWEALTH", "SQ": "SQUARE", "VW": "VIEW",
    "WAY": "WAY", "LK": "LINK", "HWAY": "HIGHWAY",
}


def norm_street(s):
    """Upper-case, expand abbreviations, drop punctuation: makes HDB street
    names and OneMap road names comparable ("BT BATOK WEST AVE 6" ->
    "BUKIT BATOK WEST AVENUE 6")."""
    s = (s or "").upper().replace("'", "").replace(".", " ").replace(",", " ")
    words = [ABBREVIATIONS.get(w, w) for w in s.split()]
    # "ST. GEORGE'S" stays "ST" only when it starts the name (Saint).
    if words and words[0] == "STREET" and len(words) > 1 and not words[1].isdigit():
        words[0] = "ST"
    return " ".join(words)


def norm_block(s):
    return re.sub(r"\s+", "", (s or "").upper())


def in_sg(lat, lon):
    return (lat is not None and lon is not None
            and SG_BBOX["lat"][0] <= lat <= SG_BBOX["lat"][1]
            and SG_BBOX["lon"][0] <= lon <= SG_BBOX["lon"][1])


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def match_onemap(block_no, street, results):
    """Pick the OneMap search result that is really this HDB block.

    exact:     same block number and the same road (after normalisation)
    fuzzy:     same block number and a very similar road name (ratio >= 0.85) that
               has the SAME numbers in it, e.g. a differently abbreviated street.
               "AVENUE 1" vs "AVENUE 2" is 95 % similar as text but is a different
               road, so differing road numbers are never fuzzy-matched.
    unmatched: anything else. We never accept a different block number:
               a wrong point is worse than a missing one.
    Returns (quality, result_dict_or_None, road_similarity).
    """
    want_blk, want_road = norm_block(block_no), norm_street(street)
    best = (None, None, 0.0)
    for r in results or []:
        lat = _num(r.get("LATITUDE"))
        lon = _num(r.get("LONGITUDE", r.get("LONGTITUDE")))   # very old responses misspelt it
        if norm_block(r.get("BLK_NO")) != want_blk or not in_sg(lat, lon):
            continue
        road = norm_street(r.get("ROAD_NAME"))
        if road == want_road:
            return "exact", r, 1.0
        if re.findall(r"\d+", road) != re.findall(r"\d+", want_road):
            continue
        ratio = difflib.SequenceMatcher(None, road, want_road).ratio()
        if ratio > best[2]:
            best = ("fuzzy", r, ratio)
    if best[1] is not None and best[2] >= 0.85:
        return best
    return "unmatched", None, best[2]


STATION_RE = re.compile(r"^(?P<name>.+?)\s+(?P<kind>MRT|LRT)\s+STATION\b", re.I)
CODES_RE = re.compile(r"\(([A-Z]{1,3}\s?\d{1,2}(?:\s*/\s*[A-Z]{1,3}\s?\d{1,2})*)\)")


def parse_station(text):
    """'TAMPINES MRT STATION (EW2 / DT32)' -> ('TAMPINES', 'mrt', ['EW2', 'DT32'])."""
    m = STATION_RE.match((text or "").strip())
    if not m:
        return None
    codes = []
    c = CODES_RE.search(text)
    if c:
        codes = [x.replace(" ", "") for x in re.split(r"\s*/\s*", c.group(1))]
    return m.group("name").strip().upper(), m.group("kind").lower(), codes


def drop_excluded(points_by_station, excluded):
    """Remove stations that are not open MRT/LRT stations. `points_by_station` is keyed
    (NAME, kind); `excluded` is a set of (NAME, kind) pairs. Returns (kept, dropped_keys)."""
    kept, dropped = {}, []
    for key, pts in points_by_station.items():
        if (key[0].upper(), key[1].lower()) in excluded:
            dropped.append(key)
        else:
            kept[key] = pts
    return kept, sorted(dropped)


def slug(*parts):
    return re.sub(r"[^a-z0-9]+", "-", " ".join(parts).lower()).strip("-")


def haversine_m(lon1, lat1, lon2, lat2, r=R_MEAN):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def destination(lon, lat, bearing_deg, dist_m, r=R_MONGO):
    """Point reached travelling dist_m from (lon, lat) on a sphere of radius r.
    Used to place edge-case probes exactly 999 m / 1001 m away (check V5)."""
    d = dist_m / r
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(l2), math.degrees(p2)


def mrt_band(distance_m):
    if distance_m is None:
        return None
    if distance_m <= 500:
        return "<=500m"
    return "500m-1km" if distance_m <= 1000 else ">1km"


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def mad(xs):
    m = median(xs)
    return median([abs(x - m) for x in xs]) if xs else None

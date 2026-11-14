"""Nearby amenities (parks, hawker centres, gyms, childcare, schools): pure helpers.

No database access here, so everything can be unit tested without OneMap or a
database (tests/test_amenities.py). The scripts and routes that use these are:

    scripts/load_amenities.py          files -> MongoDB `amenities` + PostgreSQL `amenities`
    scripts/build_amenity_features.py  per-block distances/counts (MongoDB $geoNear or PostGIS)
    analysis/amenity_model.py          does knowing the surroundings help predict price?
    resalewise/routes/amenities.py     API: what is near a block, rank blocks by your priorities

Distance model
--------------
A point amenity (a hawker centre, a school) is one coordinate. A park is a
polygon, so it is stored as a MultiPoint of points along its boundary (about
every 100 m): "distance to the park" then means "distance to its nearest edge
point", accurate to about 50 m, the same idea as a station's exits.
Distances are straight-line (as the crow flies), not walking routes.
"""
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

from .geo import in_sg, haversine_m, slug

# ---------------------------------------------------------------------------
# What we measure, and how a distance becomes a 0-100 score
# ---------------------------------------------------------------------------
# full_m: at or below this distance the score is 100; zero_m: at or beyond it the
# score is 0; in between it falls in a straight line. The school distances follow
# MOE's Primary 1 registration phases (priority for homes within 1 km, then 2 km).
Kind = namedtuple("Kind", "kind label plural full_m zero_m merge_by_name sort_order")

KINDS = [
    Kind("park",             "Park",             "Parks",             200, 1200, True,  1),
    Kind("hawker",           "Hawker centre",    "Hawker centres",    200, 1200, False, 2),
    Kind("gym",              "Gym",              "Gyms",              500, 2500, False, 3),
    Kind("childcare",        "Childcare centre", "Childcare centres", 100,  400, False, 4),
    Kind("primary_school",   "Primary school",   "Primary schools",  1000, 2000, False, 5),
    Kind("secondary_school", "Secondary school", "Secondary schools", 1000, 2500, False, 6),
]
# MRT/LRT is not stored in `amenities` (stations have their own tables) but it is
# scored the same way so a buyer can weigh it against the rest.
MRT = Kind("mrt", "MRT/LRT station", "MRT/LRT stations", 300, 1500, False, 0)

KIND_BY_NAME = {k.kind: k for k in KINDS}
SCORE_KINDS = [MRT] + KINDS          # the order shown in the interface
KIND_NAMES = [k.kind for k in KINDS]

def retired_kinds(existing):
    """Kinds that are stored but no longer part of KINDS (for example the supermarkets that
    were replaced by gyms and childcare). The loader removes them so every database ends up
    the same, whichever version of the project loaded it first."""
    return sorted(set(existing) - set(KIND_NAMES))


WALK_M_PER_MIN = 80.0                # ordinary walking pace
DETOUR = 1.3                         # real routes are longer than the straight line


def proximity_score(distance_m, full_m, zero_m):
    """100 at or inside full_m, 0 at or beyond zero_m, linear in between.
    No such amenity at all (None) scores 0."""
    if distance_m is None:
        return 0.0
    if distance_m <= full_m:
        return 100.0
    if distance_m >= zero_m:
        return 0.0
    return 100.0 * (zero_m - distance_m) / (zero_m - full_m)


def kind_score(kind, distance_m):
    k = MRT if kind == "mrt" else KIND_BY_NAME[kind]
    return proximity_score(distance_m, k.full_m, k.zero_m)


def composite_score(distances, weights):
    """Weighted mean of the per-kind scores, using only kinds with weight > 0.
    `distances` and `weights` are dicts keyed by kind name. Returns
    (score 0-100 or None if no weight was given, {kind: score})."""
    per = {k.kind: kind_score(k.kind, distances.get(k.kind)) for k in SCORE_KINDS}
    total_w = sum(w for w in weights.values() if w and w > 0)
    if total_w <= 0:
        return None, per
    s = sum(per[k] * w for k, w in weights.items() if w and w > 0 and k in per)
    return round(s / total_w, 1), per


def walk_minutes(distance_m):
    """Rough walking time for a straight-line distance (minimum 1 minute)."""
    if distance_m is None:
        return None
    return max(1, int(round(distance_m * DETOUR / WALK_M_PER_MIN)))


# ---------------------------------------------------------------------------
# Reading GeoJSON
# ---------------------------------------------------------------------------
NAME_FIELDS = ("NAME", "PARK_NAME", "PARKS_NAME", "PARKNAME", "SITE_NAME", "HAWKER_CENTRE_NAME",
               "BUSINESS_NAME", "LIC_NAME", "LICENSEE_NAME", "BUILDING_NAME", "SCHOOL_NAME",
               "STN_NAME", "TITLE")
_DESC_CELL = re.compile(r"<th>\s*(.*?)\s*</th>\s*<td>\s*(.*?)\s*</td>", re.S | re.I)


def description_cells(props):
    """data.gov.sg GeoJSON often hides the real fields in an HTML table inside
    'Description'. Returns {FIELD: value} from it (empty if there is none)."""
    desc = props.get("Description") or props.get("description") or ""
    return {k.strip().upper(): v.strip() for k, v in _DESC_CELL.findall(desc) if v and v.strip()}


_PLACEHOLDER_NAME = re.compile(r"^(kml_\d+|nil|na|n/a|-)$", re.I)     # KML exports label features kml_1, kml_2 ...


def feature_name(props, name_field=None):
    """The best name for a GeoJSON feature, or None. `name_field` forces a field."""
    props = props or {}
    cells = description_cells(props)
    upper = {str(k).upper(): v for k, v in props.items()}
    if name_field:
        v = upper.get(name_field.upper()) or cells.get(name_field.upper())
        return str(v).strip() if v else None
    for f in NAME_FIELDS:
        for v in (upper.get(f), cells.get(f)):       # the plain property, then the table inside Description
            if v and str(v).strip() and not _PLACEHOLDER_NAME.match(str(v).strip()):
                return str(v).strip()
    return None


def _ring_length_m(ring):
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(ring, ring[1:]))


def densify(ring, step_m):
    """Points along a line/ring at most `step_m` apart (the original vertices are kept)."""
    out = []
    for a, b in zip(ring, ring[1:]):
        out.append((a[0], a[1]))
        d = haversine_m(a[0], a[1], b[0], b[1])
        n = int(math.ceil(d / step_m)) if step_m > 0 else 1
        for i in range(1, n):
            f = i / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    if ring:
        out.append((ring[-1][0], ring[-1][1]))
    return out


def geometry_points(geom, step_m=100.0, max_points=300):
    """All points that stand for a GeoJSON geometry: a Point is itself; lines and
    polygon rings (outer ring only) become points about `step_m` apart. If that
    would give more than `max_points`, the spacing is widened so it fits."""
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Point":
        return [(c[0], c[1])]
    if t == "MultiPoint":
        return [(p[0], p[1]) for p in c]
    if t == "LineString":
        lines = [c]
    elif t == "MultiLineString":
        lines = list(c)
    elif t == "Polygon":
        lines = [c[0]] if c else []
    elif t == "MultiPolygon":
        lines = [poly[0] for poly in c if poly]
    elif t == "GeometryCollection":
        pts = []
        for g in geom.get("geometries") or []:
            pts.extend(geometry_points(g, step_m, max_points))
        return pts
    else:
        return []
    total = sum(_ring_length_m(r) for r in lines)
    step = max(step_m, total / max_points) if max_points else step_m
    pts = []
    for r in lines:
        pts.extend(densify([(p[0], p[1]) for p in r], step))
    return pts


def parse_geojson(gj, kind, name_field=None, step_m=100.0, max_points=300):
    """GeoJSON FeatureCollection -> ({(name, key): [points]}, info).

    Kinds with merge_by_name (parks) join features that share a name (one park
    drawn as several polygons); the others keep every feature separate, because
    two gyms of the same chain are different places."""
    k = KIND_BY_NAME[kind]
    out, info = {}, {"features": 0, "no_geometry": 0, "outside_sg": 0, "unnamed": 0, "fields_seen": []}
    for i, f in enumerate((gj or {}).get("features", [])):
        info["features"] += 1
        if not info["fields_seen"]:
            props = f.get("properties") or {}
            info["fields_seen"] = sorted(set(map(str, props)) | set(description_cells(props)))
        pts = [p for p in geometry_points(f.get("geometry"), step_m, max_points)]
        if not pts:
            info["no_geometry"] += 1
            continue
        pts = [p for p in pts if in_sg(p[1], p[0])]
        if not pts:
            info["outside_sg"] += 1
            continue
        name = feature_name(f.get("properties"), name_field)
        if not name:
            info["unnamed"] += 1
            name = f"{k.label} {i + 1}"
        if k.merge_by_name:
            key = name.upper()
        else:
            key = "%s|%.5f,%.5f" % (name.upper(), pts[0][0], pts[0][1])
        out.setdefault((name, key), []).extend(pts)
    return out, info


def build_amenities(kind, groups, source):
    """{(name, key): points} -> documents for MongoDB (and rows for PostgreSQL)."""
    docs = []
    for (name, key), pts in sorted(groups.items(), key=lambda kv: kv[0][1]):
        uniq = sorted({(round(lon, 6), round(lat, 6)) for lon, lat in pts})
        clon = sum(p[0] for p in uniq) / len(uniq)
        clat = sum(p[1] for p in uniq) / len(uniq)
        loc = ({"type": "Point", "coordinates": [float(uniq[0][0]), float(uniq[0][1])]} if len(uniq) == 1
               else {"type": "MultiPoint", "coordinates": [[float(a), float(b)] for a, b in uniq]})
        suffix = "" if KIND_BY_NAME[kind].merge_by_name else "-" + hashlib.md5(key.encode()).hexdigest()[:6]
        docs.append({"_id": slug(kind, name) + suffix, "name": name.title() if name.isupper() else name,
                     "kind": kind, "loc": loc,
                     "centroid": {"type": "Point", "coordinates": [float(clon), float(clat)]},
                     "n_points": len(uniq), "source": source})
    return docs


# ---------------------------------------------------------------------------
# Schools (MOE's list has addresses and postal codes, not coordinates)
# ---------------------------------------------------------------------------
def find_column(columns, aliases):
    """The first column whose normalised name matches an alias (case, spaces and
    underscores ignored), or None."""
    norm = {re.sub(r"[^a-z0-9]", "", str(c).lower()): c for c in columns}
    for a in aliases:
        hit = norm.get(re.sub(r"[^a-z0-9]", "", a.lower()))
        if hit is not None:
            return hit
    return None


SCHOOL_COLUMNS = {
    "name": ("school_name", "name", "school"),
    "postal": ("postal_code", "postalcode", "postal", "zipcode"),
    "level": ("mainlevel_code", "main_level_code", "mainlevel", "school_level", "level"),
}


def classify_school(level):
    """MOE 'mainlevel_code' -> our kind. Mixed-level schools (integrated
    programmes) are counted as secondary; junior colleges and centralised
    institutes are not part of the primary/secondary picture."""
    s = re.sub(r"\s+", " ", str(level or "").strip().upper())
    if s.startswith("PRIMARY"):
        return "primary_school"
    if s.startswith("SECONDARY") or s.startswith("MIXED"):
        return "secondary_school"
    return None


def school_kinds(level):
    """Every kind a school counts as. Integrated schools that start at Primary 1
    (MOE 'MIXED LEVEL (P1-S4)': Catholic High, CHIJ St Nicholas, Maris Stella)
    are both a primary and a secondary school."""
    kind = classify_school(level)
    if kind is None:
        return []
    s = re.sub(r"\s+", " ", str(level or "").strip().upper())
    if s.startswith("MIXED") and "P1" in s:
        return ["primary_school", "secondary_school"]
    return [kind]


def clean_postal(value):
    """Postal codes lose their leading zero in spreadsheets: '78884' -> '078884'."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(6) if 1 <= len(digits) <= 6 else None


def pick_postal_result(results, postal):
    """The OneMap search hit for exactly this postal code (first one in Singapore)."""
    for r in results or []:
        try:
            lat, lon = float(r.get("LATITUDE")), float(r.get("LONGITUDE"))
        except (TypeError, ValueError):
            continue
        if str(r.get("POSTAL", "")).strip().zfill(6) == postal and in_sg(lat, lon):
            return lat, lon
    return None


ONEMAP_BASE = "https://www.onemap.gov.sg"


def onemap_postal(postal, base=ONEMAP_BASE, retries=6):
    """Look one postal code up on OneMap. Returns (lat, lon) or None. Waits and
    retries politely when OneMap says it is busy (HTTP 429)."""
    q = urllib.parse.urlencode({"searchVal": postal, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1})
    req = urllib.request.Request(f"{base}/api/common/elastic/search?{q}",
                                 headers={"User-Agent": "ResaleWiseSG/1.0 (student project)"})
    delay = 5.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return pick_postal_result(json.loads(resp.read().decode("utf-8")).get("results"), postal)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise SystemExit(f"OneMap keeps answering HTTP {e.code}. Wait a few minutes and run the same "
                             "command again: postal codes already looked up are saved and are skipped.")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise SystemExit(f"Cannot reach OneMap ({e}). Check your internet connection and run the "
                             "command again: postal codes already looked up are saved.")
    return None


# ---------------------------------------------------------------------------
# Comparing the two engines
# ---------------------------------------------------------------------------
def compare_rows(a, b, rel_tol=0.01, abs_tol_m=3.0):
    """Compare two lists of (block_id, kind, nearest_m, nearest_name, n500, n1k, n2k).

    MongoDB measures on a sphere and PostGIS on the WGS84 spheroid, so distances
    can differ by up to about 0.3 %: nearest distances count as agreeing when
    within `rel_tol` or `abs_tol_m` metres. Counts can legitimately differ by one
    for an amenity almost exactly on a radius, so they are reported, not asserted."""
    ia = {(r[0], r[1]): r for r in a}
    ib = {(r[0], r[1]): r for r in b}
    both = ia.keys() & ib.keys()
    dist_ok, diffs, count_same = 0, [], 0
    for key in both:
        ra, rb = ia[key], ib[key]
        da, db_ = ra[2], rb[2]
        if da is None or db_ is None:
            dist_ok += int(da is None and db_ is None)
        else:
            diff = abs(float(da) - float(db_))
            diffs.append(diff)
            if diff <= max(abs_tol_m, rel_tol * max(float(da), float(db_))):
                dist_ok += 1
        count_same += int(tuple(ra[4:7]) == tuple(rb[4:7]))
    n = len(both)
    return {"pairs_compared": n, "only_in_first": len(ia.keys() - ib.keys()),
            "only_in_second": len(ib.keys() - ia.keys()),
            "nearest_distance_agree": dist_ok,
            "nearest_distance_agree_pct": round(100.0 * dist_ok / n, 2) if n else None,
            "max_abs_diff_m": round(max(diffs), 1) if diffs else None,
            "mean_abs_diff_m": round(sum(diffs) / len(diffs), 2) if diffs else None,
            "counts_identical": count_same,
            "counts_identical_pct": round(100.0 * count_same / n, 2) if n else None}

"""Nearby amenities: what is around a block, and which blocks fit your priorities.

GET /api/amenities/status          what has been loaded
GET /api/amenities/block/<id>      distances, counts and scores for one block
GET /api/amenities/rank            blocks ranked by how well they match the weights you give

The score is deliberately simple and explained on the page: each kind of place
scores 0-100 by how close the nearest one is (100 inside a short distance, falling in
a straight line to 0 at a longer one: see resalewise/amenities.py), and the overall
score is the weighted average using the weights the user picks. It is a way to sort
by YOUR priorities, not a verdict on which block is "best". The model-based location
premium (analysis/amenity_model.py) is shown next to it when it has been computed;
it is an association in past sales, not a cause.

Every endpoint answers {"available": false} until the pipeline has been run.
"""
import functools

from flask import Blueprint, jsonify, request

from .. import amenities as am
from .. import db
from ..benchmarks import norm_flat_type
from ..web import ApiError, arg_float, arg_int, arg_str, cache_json

bp = Blueprint("amenities", __name__)

DEFAULT_WEIGHTS = {"mrt": 3, "park": 2, "hawker": 2, "gym": 1, "childcare": 1, "primary_school": 2, "secondary_school": 1}


def _ready():
    return db.has_relation("amenities") and db.has_relation("block_amenity_features")


def amenities_required(fn):
    """Placed ABOVE @cache_json so a 'not ready' reply can never be cached."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _ready():
            resp = jsonify({"available": False})
            resp.headers["Cache-Control"] = "no-store"
            return resp
        return fn(*args, **kwargs)
    return wrapper


def _item(kind, label, nearest_m, name=None, n500=None, n1k=None, n2k=None):
    d = float(nearest_m) if nearest_m is not None else None
    return {"kind": kind, "label": label, "nearest_m": d, "nearest_name": name,
            "walk_min": am.walk_minutes(d), "within_500m": n500, "within_1km": n1k, "within_2km": n2k,
            "score": round(am.kind_score(kind, d), 1)}


@bp.route("/api/amenities/status")
def status():
    if not _ready():
        return jsonify({"available": False})
    kinds = db.q("SELECT kind, COUNT(*)::int AS n FROM amenities GROUP BY kind ORDER BY kind")
    feats = db.q("SELECT COUNT(DISTINCT block_id)::int AS blocks FROM block_amenity_features")[0]["blocks"]
    prem = db.q("SELECT COUNT(*)::int AS n FROM block_location_premium")[0]["n"] \
        if db.has_relation("block_location_premium") else 0
    return jsonify({"available": True, "amenities": {r["kind"]: r["n"] for r in kinds},
                    "blocks_with_features": feats, "blocks_with_premium": prem})


PLACES_PER_KIND = 5          # the nearest few of each kind, so a block next to ten childcare centres stays readable
PLACES_RADIUS_M = 2000


def _places(block_id):
    """The nearest places of every kind within 2 km, each with a point to draw on a map.
    A park's point is the nearest bit of its boundary, which is what its distance means."""
    rows = db.q("""
      SELECT kind, name, d, lat, lon FROM (
        SELECT a.kind, a.name, ST_Distance(a.geog, bl.geog) AS d,
               ST_Y(cp.g) AS lat, ST_X(cp.g) AS lon,
               ROW_NUMBER() OVER (PARTITION BY a.kind ORDER BY ST_Distance(a.geog, bl.geog), a.name) AS rn
        FROM block_locations bl
        JOIN amenities a ON ST_DWithin(a.geog, bl.geog, %s)
        CROSS JOIN LATERAL (SELECT ST_ClosestPoint(a.geog::geometry, bl.geog::geometry) AS g) cp
        WHERE bl.block_id = %s) t
      WHERE rn <= %s ORDER BY kind, d""", [PLACES_RADIUS_M, block_id, PLACES_PER_KIND])
    return [{"kind": r["kind"], "name": r["name"], "distance_m": round(float(r["d"]), 1),
             "walk_min": am.walk_minutes(float(r["d"])), "lat": r["lat"], "lon": r["lon"]} for r in rows]


@bp.route("/api/amenities/block/<int:block_id>")
@amenities_required
@cache_json(3600)
def block(block_id):
    rows = db.q("""
      SELECT f.kind, k.label, f.nearest_m::float AS nearest_m, f.nearest_name,
             f.within_500m, f.within_1km, f.within_2km
      FROM block_amenity_features f JOIN amenity_kinds k ON k.kind = f.kind
      WHERE f.block_id = %s ORDER BY k.sort_order""", [block_id])
    items = []
    if db.has_relation("block_features"):
        m = db.q("SELECT nearest_station_name, nearest_mrt_m::float AS nearest_mrt_m, stations_within_1km "
                 "FROM block_features WHERE block_id = %s", [block_id])
        if m and m[0]["nearest_mrt_m"] is not None:
            items.append(_item("mrt", am.MRT.label, m[0]["nearest_mrt_m"], m[0]["nearest_station_name"],
                               None, m[0]["stations_within_1km"], None))
    items += [_item(r["kind"], r["label"], r["nearest_m"], r["nearest_name"],
                    r["within_500m"], r["within_1km"], r["within_2km"]) for r in rows]
    if not items:
        raise ApiError("No amenity data for that block (it may not have been located).", 404, "not_found")
    premium = None
    if db.has_relation("block_location_premium"):
        p = db.q("SELECT premium_pct::float AS premium_pct, n_sales, model_run FROM block_location_premium "
                 "WHERE block_id = %s", [block_id])
        premium = p[0] if p else None
    return jsonify({"available": True, "block_id": block_id, "items": items, "premium": premium,
                    "places": _places(block_id),
                    "note": "Straight-line distances; walking time assumes about 80 m a minute with a 30 % detour."})


def _weights():
    w = {k.kind: arg_float(request.args, "w_" + k.kind, DEFAULT_WEIGHTS.get(k.kind, 0), lo=0, hi=5)
         for k in am.SCORE_KINDS}
    if not any(v > 0 for v in w.values()):
        raise ApiError("Give at least one priority a weight above 0.", 400, "invalid_parameter")
    return w


@bp.route("/api/amenities/rank")
@amenities_required
@cache_json(300)
def rank():
    weights = _weights()
    loaded = {r["kind"] for r in db.q("SELECT DISTINCT kind FROM block_amenity_features")}
    if db.has_relation("block_features"):
        loaded.add("mrt")
    unloaded = sorted(k for k in weights if k not in loaded)
    weights = {k: w for k, w in weights.items() if k in loaded}   # a kind with no data cannot be scored
    if not any(w > 0 for w in weights.values()):
        raise ApiError("None of the places you weighted have been loaded yet.", 400, "invalid_parameter")
    flat_type = norm_flat_type(arg_str(request.args, "flat_type", "4 ROOM"))
    town = arg_str(request.args, "town")
    max_price = arg_int(request.args, "max_price", None, lo=0, hi=5_000_000)
    months = arg_int(request.args, "months", 24, lo=1, hi=120)
    min_sales = arg_int(request.args, "min_sales", 3, lo=1, hi=50)
    limit = arg_int(request.args, "limit", 20, lo=1, hi=50)

    sql = """
      SELECT b.block_id, b.block_no, b.street_name, tw.town_name,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price / NULLIF(t.floor_area_sqm, 0)))::int AS median_psm,
             COUNT(*)::int AS n_sales,
             (ARRAY_AGG(t.txn_id ORDER BY t.month DESC, t.txn_id DESC))[1] AS latest_txn_id
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE UPPER(REPLACE(ft.flat_type_name, '-', ' ')) = %s
        AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - make_interval(months => %s))"""
    params = [flat_type, months]
    if town:
        sql += " AND tw.town_name = %s"
        params.append(town.upper())
    sql += " GROUP BY b.block_id, b.block_no, b.street_name, tw.town_name HAVING COUNT(*) >= %s"
    params.append(min_sales)
    blocks = db.q(sql, params)
    if max_price is not None:
        blocks = [b for b in blocks if b["median_price"] is not None and b["median_price"] <= max_price]

    ids = [b["block_id"] for b in blocks]
    dist, counts = {}, {}
    if ids:
        for r in db.q("SELECT block_id, kind, nearest_m::float AS nearest_m, within_1km "
                      "FROM block_amenity_features WHERE block_id = ANY(%s)", [ids]):
            dist.setdefault(r["block_id"], {})[r["kind"]] = r["nearest_m"]
            counts.setdefault(r["block_id"], {})[r["kind"]] = r["within_1km"]
        if db.has_relation("block_features"):
            for r in db.q("SELECT block_id, nearest_mrt_m::float AS m, stations_within_1km "
                          "FROM block_features WHERE block_id = ANY(%s)", [ids]):
                if r["m"] is not None:
                    dist.setdefault(r["block_id"], {})["mrt"] = r["m"]
                    counts.setdefault(r["block_id"], {})["mrt"] = r["stations_within_1km"]
    premium = {}
    if ids and db.has_relation("block_location_premium"):
        premium = {r["block_id"]: r["premium_pct"] for r in db.q(
            "SELECT block_id, premium_pct::float AS premium_pct FROM block_location_premium "
            "WHERE block_id = ANY(%s)", [ids])}

    ranked, without = [], 0
    for b in blocks:
        d = dist.get(b["block_id"])
        if not d:
            without += 1                       # not located: cannot be scored
            continue
        score, per = am.composite_score(d, weights)
        ranked.append({**b, "score": score, "scores": {k: round(v, 1) for k, v in per.items() if k in loaded},
                       "nearest_m": {k: round(v, 1) for k, v in d.items() if v is not None},
                       "walk_min": {k: am.walk_minutes(v) for k, v in d.items() if v is not None},
                       "within_1km": counts.get(b["block_id"], {}),
                       "premium_pct": premium.get(b["block_id"])})
    ranked.sort(key=lambda r: (-r["score"], r["median_price"] or 0, r["block_id"]))
    return jsonify({"available": True, "flat_type": flat_type, "town": town.upper() if town else None,
                    "months": months, "max_price": max_price, "weights": weights, "not_loaded": unloaded,
                    "candidates": len(blocks), "scored": len(ranked), "not_located": without,
                    "results": ranked[:limit],
                    "scale": {k.kind: {"label": k.label, "full_score_within_m": k.full_m, "zero_score_at_m": k.zero_m}
                              for k in am.SCORE_KINDS},
                    "note": "Sorted by how close each block is to what you weighted; a higher score is not "
                            "a verdict that a block is better value. Straight-line distances."})

"""Nearby amenities: what is around a block.

GET /api/amenities/status          what has been loaded
GET /api/amenities/block/<id>      distances, counts and scores for one block

Each kind of place scores 0-100 by how close the nearest one is (100 inside a short
distance, falling in a straight line to 0 at a longer one: see resalewise/amenities.py).
The model-based location premium (analysis/amenity_model.py) is shown next to it when it
has been computed; it is an association in past sales, not a cause.

Every endpoint answers {"available": false} until the pipeline has been run.
"""
import functools

from flask import Blueprint, jsonify, request

from .. import amenities as am
from .. import db
from ..web import ApiError, cache_json

bp = Blueprint("amenities", __name__)


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

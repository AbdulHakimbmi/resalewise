"""Geospatial endpoints. Block coordinates come from PostgreSQL
(block_locations); nearest-station queries run in MongoDB with $geoNear on
the `places` collection (2dsphere index). Every endpoint degrades to
{"available": false} until the geospatial pipeline has been run."""
import functools

from flask import Blueprint, jsonify, request

from .. import db
from ..web import ApiError, arg_int, cache_json

bp = Blueprint("geo", __name__)


def _geo_ready():
    return db.has_relation("block_locations") and db.has_relation("mrt_stations")


def geo_required(fn):
    """Answer {"available": false} (never cached) until the pipeline has run;
    placed ABOVE @cache_json so the 'not ready yet' reply can't stick in the
    cache after the data arrives."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _geo_ready():
            resp = jsonify({"available": False})
            resp.headers["Cache-Control"] = "no-store"
            return resp
        return fn(*args, **kwargs)
    return wrapper


@bp.route("/api/geo/status")
def geo_status():
    if not _geo_ready():
        return jsonify({"available": False})
    loc = db.q("SELECT match_quality, COUNT(*)::int AS n FROM block_locations GROUP BY 1")
    counts = {r["match_quality"]: r["n"] for r in loc}
    stations = db.q("SELECT COUNT(*)::int AS n FROM mrt_stations")[0]["n"]
    feats = db.q("SELECT COUNT(*)::int AS n FROM block_features")[0]["n"] \
        if db.has_relation("block_features") else 0
    return jsonify({"available": True, "blocks": counts, "stations": stations, "block_features": feats})


def nearest_stations(lon, lat, limit=3, max_m=None):
    """$geoNear must be the FIRST stage of the pipeline. GeoJSON coordinates
    are [longitude, latitude]; with a 2dsphere index distances are metres."""
    stage = {"near": {"type": "Point", "coordinates": [lon, lat]},
             "distanceField": "distance_m", "spherical": True, "key": "loc"}
    if max_m:
        stage["maxDistance"] = max_m
    docs = db.mongo().places.aggregate([
        {"$geoNear": stage},
        {"$limit": limit},
        {"$project": {"name": 1, "kind": 1, "codes": 1, "distance_m": 1,
                      "centroid": "$centroid.coordinates"}},
    ])
    return [{"station_id": d["_id"], "name": d["name"], "kind": d.get("kind"),
             "codes": d.get("codes", []), "distance_m": round(d["distance_m"], 1),
             "lon": d["centroid"][0], "lat": d["centroid"][1]} for d in docs]


@bp.route("/api/geo/block/<int:block_id>")
@geo_required
@cache_json(3600)
def geo_block(block_id):
    rows = db.q("""
      SELECT l.block_id, l.lat, l.lon, l.postal, l.matched_address, l.match_quality,
             f.nearest_station_name, f.nearest_mrt_m::float AS nearest_mrt_m,
             f.stations_within_1km, f.mrt_band
      FROM block_locations l
      LEFT JOIN block_features f ON f.block_id = l.block_id
      WHERE l.block_id = %s
    """, [block_id])
    if not rows:
        raise ApiError("That block has not been geocoded.", 404, "not_found")
    r = rows[0]
    if r["lat"] is None:
        return jsonify({"available": True, "located": False, "match_quality": r["match_quality"]})
    r.update({"available": True, "located": True,
              "stations": nearest_stations(r["lon"], r["lat"], limit=3)})
    return jsonify(r)


@bp.route("/api/geo/town/<path:town>")
@geo_required
@cache_json(3600)
def geo_town(town):
    """Every geocoded block in a town with its recent median price per sqm,
    plus the stations around it: the data for the town-page map."""
    months = arg_int(request.args, "months", 24, lo=1, hi=240)
    blocks = db.q("""
      SELECT b.block_id, b.block_no, b.street_name, l.lat, l.lon,
             f.nearest_station_name, f.nearest_mrt_m::float AS nearest_mrt_m, f.mrt_band,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY t.resale_price / NULLIF(t.floor_area_sqm, 0)))::int AS median_psm,
             COUNT(t.txn_id)::int AS n
      FROM blocks b
      JOIN towns tw           ON tw.town_id = b.town_id
      JOIN block_locations l  ON l.block_id = b.block_id AND l.lat IS NOT NULL
      LEFT JOIN block_features f ON f.block_id = b.block_id
      LEFT JOIN resale_transactions t
             ON t.block_id = b.block_id
            AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - (%s || ' months')::interval)
      WHERE tw.town_name = %s
      GROUP BY b.block_id, b.block_no, b.street_name, l.lat, l.lon,
               f.nearest_station_name, f.nearest_mrt_m, f.mrt_band
    """, [months, town.upper()])
    if not blocks:
        return jsonify({"available": True, "town": town.upper(), "blocks": [], "stations": []})
    lats = [b["lat"] for b in blocks]
    lons = [b["lon"] for b in blocks]
    pad = 0.01                                              # ~1.1 km around the town
    w, s, e, n = min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad
    ring = [[w, s], [e, s], [e, n], [w, n], [w, s]]             # closed GeoJSON ring, [lon, lat]
    stations = [{"station_id": d["_id"], "name": d["name"], "kind": d.get("kind"),
                 "codes": d.get("codes", []), "lon": d["centroid"]["coordinates"][0],
                 "lat": d["centroid"]["coordinates"][1]}
                for d in db.mongo().places.find(
                    {"centroid": {"$geoWithin": {"$geometry": {"type": "Polygon", "coordinates": [ring]}}}},
                    {"name": 1, "kind": 1, "codes": 1, "centroid": 1})]
    return jsonify({"available": True, "town": town.upper(), "months": months,
                    "blocks": blocks, "stations": stations})

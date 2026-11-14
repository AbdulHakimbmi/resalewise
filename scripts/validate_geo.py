"""Step 4 of the geospatial pipeline: validate it (checks V1-V6).

    python scripts/validate_geo.py

Writes data/results/geo_validation.json (shown on the About page and used in
the report) and data/results/geo_validation.md. Check V3 needs a person:
the script writes data/results/v3_ground_truth.csv with 30 sampled blocks;
open each link, write Y or N in the `correct` column, save, and run the
script again to score it.

  V1 Coverage        share of blocks / sales that got a location
  V2 Plausibility    inside Singapore; not far from the rest of its town
  V3 Ground truth    30 random blocks checked by hand against the map
  V4 Cross-engine    MongoDB $geoNear vs PostGIS KNN vs haversine agree
  V5 Edge cases      1 km boundary, MultiPoint stations, stage order, [lon,lat]
  V6 Rebuild         re-running the pipeline gives identical output

Tolerances in V4 were set from a measured pilot, not guessed: MongoDB uses a
sphere of radius 6,378.1 km, PostGIS geography uses the WGS84 spheroid by
default. Near the equator the two differ by up to ~0.7 % depending on
direction (~7 m per km), and a sphere-to-sphere comparison differs only by
the radius ratio (~0.11 %).
"""
import csv
import importlib.util
import json
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from pymongo.errors import OperationFailure, WriteError  # noqa: E402

from resalewise import config, db  # noqa: E402
from resalewise.geo import R_MEAN, R_MONGO, destination, haversine_m, mad, median  # noqa: E402

OUT_DIR = os.path.join(config.ROOT, "data", "results")
V3_CSV = os.path.join(OUT_DIR, "v3_ground_truth.csv")
V3_ROUND1 = os.path.join(OUT_DIR, "v3_round1.csv")   # the first hand check, kept as evidence
SEED = 2003                                   # INF2003: fixed so the sample is reproducible

TOL_SPHERE = 0.002        # 0.2 %: same model, radius ratio 6378.1/6371.0 explains ~0.11 %
TOL_SPHEROID = 0.010      # 1.0 %: sphere vs WGS84 spheroid near the equator (~0.7 % measured)
TIE_REL = 0.010           # two stations closer than 1 % apart count as a tie
ROUNDING_M = 0.05         # block_features stores metres to 0.1 m: allow half a step


def _rel(stored, reference):
    """Relative difference beyond what the 0.1 m rounding of the stored value
    explains (at 22 m, rounding alone is ~0.2 %)."""
    return max(0.0, abs(stored - reference) - ROUNDING_M) / reference


def _script(name):
    path = os.path.join(config.ROOT, "scripts", name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pctile(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def check(cid, name, status, criterion, metrics, detail=""):
    return {"id": cid, "name": name, "status": status, "criterion": criterion,
            "metrics": metrics, "detail": detail}


# --------------------------------------------------------------------------- V1
def v1(cur):
    cur.execute("""
      SELECT COUNT(*) AS blocks,
             COUNT(*) FILTER (WHERE l.match_quality = 'exact') AS exact,
             COUNT(*) FILTER (WHERE l.match_quality = 'fuzzy') AS fuzzy,
             COUNT(*) FILTER (WHERE l.match_quality = 'unmatched') AS unmatched,
             COUNT(*) FILTER (WHERE l.block_id IS NULL) AS not_attempted,
             COUNT(*) FILTER (WHERE l.match_quality = 'unmatched' AND NOT b.in_property_info) AS unmatched_demolished
      FROM blocks b LEFT JOIN block_locations l USING (block_id)""")
    m = dict(cur.fetchone())
    cur.execute("""
      SELECT COUNT(*) AS sales,
             COUNT(*) FILTER (WHERE l.lat IS NOT NULL) AS sales_located,
             COUNT(*) FILTER (WHERE t.month >= DATE '2017-01-01') AS sales_2017,
             COUNT(*) FILTER (WHERE t.month >= DATE '2017-01-01' AND l.lat IS NOT NULL) AS located_2017
      FROM resale_transactions t LEFT JOIN block_locations l USING (block_id)""")
    s = dict(cur.fetchone())
    cur.execute("""
      SELECT b.block_no, b.street_name, tw.town_name, b.in_property_info, COUNT(t.txn_id) AS sales
      FROM block_locations l JOIN blocks b USING (block_id) JOIN towns tw USING (town_id)
      LEFT JOIN resale_transactions t USING (block_id)
      WHERE l.match_quality = 'unmatched'
      GROUP BY 1, 2, 3, 4 ORDER BY sales DESC LIMIT 15""")
    worst = [dict(r) for r in cur.fetchall()]
    located = m["exact"] + m["fuzzy"]
    pct_blocks = 100 * located / max(m["blocks"], 1)
    pct_sales = 100 * s["sales_located"] / max(s["sales"], 1)
    pct_recent = 100 * s["located_2017"] / max(s["sales_2017"], 1)
    m.update(located=located, pct_blocks=round(pct_blocks, 2), pct_sales=round(pct_sales, 2),
             pct_sales_2017=round(pct_recent, 2), **s)
    status = "pass" if pct_blocks >= 95 and pct_recent >= 99 else ("warn" if pct_blocks >= 90 else "fail")
    return check("V1", "Coverage", status,
                 ">= 95 % of blocks and >= 99 % of 2017+ sales located",
                 m, {"largest_unmatched": worst})


# --------------------------------------------------------------------------- V2
def v2(cur):
    cur.execute("""
      SELECT l.block_id, l.lat, l.lon, l.in_sg_bbox, tw.town_name, b.block_no, b.street_name, l.match_quality
      FROM block_locations l JOIN blocks b USING (block_id) JOIN towns tw USING (town_id)
      WHERE l.lat IS NOT NULL""")
    rows = cur.fetchall()
    outside = [dict(r) for r in rows if not r["in_sg_bbox"]]
    by_town = {}
    for r in rows:
        by_town.setdefault(r["town_name"], []).append(r)
    outliers = []
    for town, rs in by_town.items():
        if len(rs) < 10:
            continue
        clat, clon = median([r["lat"] for r in rs]), median([r["lon"] for r in rs])
        dists = [haversine_m(r["lon"], r["lat"], clon, clat) for r in rs]
        cut = median(dists) + 3 * mad(dists)
        for r, d in zip(rs, dists):
            if d > cut and d > 2000:
                outliers.append({"block_id": r["block_id"], "town": town, "block_no": r["block_no"],
                                 "street": r["street_name"], "quality": r["match_quality"],
                                 "km_from_town_centre": round(d / 1000, 2), "cutoff_km": round(cut / 1000, 2)})
    outliers.sort(key=lambda o: -o["km_from_town_centre"])
    dist_share = 100 * len(outliers) / max(len(rows), 1)
    per_town = sorted(({"town": t, "flagged": sum(1 for o in outliers if o["town"] == t), "blocks": len(rs)}
                       for t, rs in by_town.items() if any(o["town"] == t for o in outliers)),
                      key=lambda x: -x["flagged"])

    # Neighbour test. A block placed in the wrong spot lands among blocks of ANOTHER
    # town; a block merely far from its town's middle (Sengkang, Hougang and Pasir Ris
    # are long and thin) still has neighbours from its own town. So: of a block's K
    # nearest located neighbours, are nearly all from a different town?
    import numpy as np
    K, NEED = 7, 6
    lat = np.array([float(r["lat"]) for r in rows])
    lon = np.array([float(r["lon"]) for r in rows])
    xy = np.column_stack([lat * 111_320.0, lon * 111_320.0 * np.cos(np.radians(lat.mean()))])
    towns = np.array([r["town_name"] for r in rows])
    suspicious = []
    for i in range(0, len(rows), 1000):
        d2 = ((xy[i:i + 1000, None, :] - xy[None, :, :]) ** 2).sum(axis=2)
        for j in range(d2.shape[0]):
            d2[j, i + j] = np.inf
        nn = np.argpartition(d2, K, axis=1)[:, :K]
        for j in range(nn.shape[0]):
            idx = nn[j]
            own = str(towns[i + j])
            same = int((towns[idx] == own).sum())
            if K - same >= NEED:                      # >= 6 of 7 neighbours are from other towns
                neighbours = sorted(str(t) for t in set(towns[idx]))
                r = rows[i + j]
                suspicious.append({"block_id": r["block_id"], "town": own, "block_no": r["block_no"],
                                   "street": r["street_name"], "quality": r["match_quality"],
                                   "neighbour_towns": neighbours,
                                   "nearest_neighbour_m": round(float(np.sqrt(d2[j, idx].min()))) })
    share = 100 * len(suspicious) / max(len(rows), 1)
    status = "fail" if outside else ("pass" if share <= 0.5 else "warn")
    return check("V2", "Plausibility", status,
                 "100 % inside Singapore's bounding box; <= 0.5 % of blocks sit among "
                 f"neighbours of another town ({NEED} of the {K} nearest from a different town)",
                 {"located": len(rows), "outside_bbox": len(outside),
                  "neighbour_inconsistent": len(suspicious), "pct_neighbour_inconsistent": round(share, 2),
                  "distance_test_flagged": len(outliers), "pct_distance_test_flagged": round(dist_share, 2)},
                 {"note": "The originally planned distance test (> median + 3 MAD and > 2 km from the town's "
                          "median point) flags long, thin towns where far-apart blocks are normal, so it is "
                          "reported for transparency but is not the pass criterion; the neighbour test is. "
                          "This change was made after seeing the distance-test result.",
                  "neighbour_inconsistent_blocks": suspicious[:25],
                  "distance_test_by_town": per_town[:12],
                  "distance_test_top": outliers[:15], "outside": outside[:10]})


# --------------------------------------------------------------------------- V3
def _v3_round1():
    """Result of an earlier hand check, if one was kept (see the report: it found
    that fuzzy matching accepted roads with a different number)."""
    if not os.path.isfile(V3_ROUND1):
        return None
    with open(V3_ROUND1, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("correct") or "").strip().upper() in ("Y", "N")]
    wrong = [{"block": r["block"], "street": r["street"], "match_quality": r["match_quality"],
              "onemap_address": r["onemap_address"], "notes": r.get("notes")}
             for r in rows if r["correct"].strip().upper() == "N"]
    return {"sampled": len(rows), "correct": len(rows) - len(wrong), "wrong": wrong,
            "outcome": "defect found and fixed: a fuzzy match accepted a road with a different number; "
                       "fuzzy matching now requires identical road numbers, and a fresh sample was drawn"}


def v3(cur):
    existing = {}
    if os.path.isfile(V3_CSV):
        with open(V3_CSV, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                existing[int(r["block_id"])] = r
    if not existing:
        cur.execute("""
          SELECT l.block_id, b.block_no, b.street_name, tw.town_name, l.lat, l.lon, l.match_quality,
                 l.matched_address
          FROM block_locations l JOIN blocks b USING (block_id) JOIN towns tw USING (town_id)
          WHERE l.lat IS NOT NULL ORDER BY l.block_id""")
        rows = cur.fetchall()
        rng = random.Random(SEED)
        fuzzy = [r for r in rows if r["match_quality"] == "fuzzy"]
        exact = [r for r in rows if r["match_quality"] == "exact"]
        sample = rng.sample(fuzzy, min(10, len(fuzzy)))           # over-sample the riskier matches
        sample += rng.sample(exact, min(30 - len(sample), len(exact)))
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(V3_CSV, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["block_id", "block", "street", "town", "lat", "lon", "match_quality",
                        "onemap_address", "map_link", "correct", "notes"])
            for r in sorted(sample, key=lambda r: r["block_id"]):
                w.writerow([r["block_id"], r["block_no"], r["street_name"], r["town_name"],
                            f"{r['lat']:.6f}", f"{r['lon']:.6f}", r["match_quality"], r["matched_address"],
                            f"https://www.openstreetmap.org/?mlat={r['lat']:.6f}&mlon={r['lon']:.6f}#map=19/{r['lat']:.6f}/{r['lon']:.6f}",
                            "", ""])
        return check("V3", "Manual ground truth", "pending",
                     ">= 29 of 30 sampled blocks confirmed on the map",
                     {"sampled": len(sample), "checked": 0},
                     {"instructions": "Open data/results/v3_ground_truth.csv, check each map link, write Y/N in "
                                      "'correct', then re-run this script.", "round1": _v3_round1()})
    done = [r for r in existing.values() if (r.get("correct") or "").strip().upper() in ("Y", "N")]
    ok = sum(1 for r in done if r["correct"].strip().upper() == "Y")
    wrong = [{"block_id": int(r["block_id"]), "block": r["block"], "street": r["street"], "notes": r.get("notes")}
             for r in done if r["correct"].strip().upper() == "N"]
    if len(done) < len(existing):
        status = "pending"
    else:
        status = "pass" if ok >= len(existing) - 1 else "fail"
    return check("V3", "Manual ground truth", status,
                 ">= 29 of 30 sampled blocks confirmed on the map",
                 {"sampled": len(existing), "checked": len(done), "correct": ok,
                  "accuracy_pct": round(100 * ok / len(done), 1) if done else None},
                 {"wrong": wrong, "round1": _v3_round1()})


# --------------------------------------------------------------------------- V4
V4_SQL = """
SELECT l.block_id, l.lat, l.lon, f.nearest_station_id AS mongo_id, f.nearest_mrt_m::float AS mongo_m,
       k.station_id AS pg_id, k.d_sphere, k.d_spheroid, k.second_spheroid
FROM block_locations l
JOIN block_features f USING (block_id)
CROSS JOIN LATERAL (
  SELECT x.station_id,
         ST_Distance(x.geog, l.geog, false) AS d_sphere,
         ST_Distance(x.geog, l.geog, true)  AS d_spheroid,
         LEAD(ST_Distance(x.geog, l.geog, true)) OVER (ORDER BY x.geog <-> l.geog) AS second_spheroid
  FROM (SELECT station_id, geog FROM mrt_stations ORDER BY geog <-> l.geog LIMIT 2) x
  ORDER BY x.geog <-> l.geog
  LIMIT 1
) k
WHERE l.lat IS NOT NULL
"""


def v4(cur):
    cur.execute(V4_SQL)
    rows = cur.fetchall()
    if not rows:
        return check("V4", "Cross-engine distances", "fail", "-", {}, "No block_features rows: run build_block_features.py")
    agree = ties = disagree = 0
    rel_sphere, rel_spheroid, rel_hav, flips = [], [], [], 0
    examples = []
    for r in rows:
        if r["mongo_id"] == r["pg_id"]:
            agree += 1
        elif r["second_spheroid"] and abs(r["second_spheroid"] - r["d_spheroid"]) / max(r["d_spheroid"], 1) < TIE_REL:
            ties += 1
        else:
            disagree += 1
            if len(examples) < 10:
                examples.append({k: r[k] for k in ("block_id", "mongo_id", "pg_id", "mongo_m", "d_spheroid")})
        if r["mongo_id"] == r["pg_id"] and r["d_sphere"] > 1:
            rel_sphere.append(_rel(r["mongo_m"], r["d_sphere"]))
            rel_spheroid.append(_rel(r["mongo_m"], r["d_spheroid"]))
        if (r["mongo_m"] <= 1000) != (r["d_spheroid"] <= 1000):
            flips += 1
    # Haversine on MongoDB's own sphere, to the nearest member (exit) of the
    # nearest station: isolates the formula from the earth model.
    members = {}
    for d in db.mongo().places.find({}, {"loc": 1}):
        pts = d["loc"]["coordinates"]
        members[d["_id"]] = pts if d["loc"]["type"] == "MultiPoint" else [pts]
    for r in rows:
        pts = members.get(r["mongo_id"])
        if pts:
            hv = min(haversine_m(r["lon"], r["lat"], p[0], p[1], R_MONGO) for p in pts)
            if hv > 1:
                rel_hav.append(_rel(r["mongo_m"], hv))
    n = len(rows)
    agree_pct = 100 * agree / max(n - ties, 1)
    m = {
        "blocks_compared": n, "same_nearest_station": agree, "ties_within_1pct": ties, "disagreements": disagree,
        "agreement_pct_excl_ties": round(agree_pct, 3),
        "rel_diff_vs_postgis_sphere": {"max_pct": round(100 * max(rel_sphere, default=0), 4),
                                       "p99_pct": round(100 * (pctile(rel_sphere, .99) or 0), 4)},
        "rel_diff_vs_postgis_spheroid": {"max_pct": round(100 * max(rel_spheroid, default=0), 4),
                                         "p99_pct": round(100 * (pctile(rel_spheroid, .99) or 0), 4)},
        "rel_diff_vs_haversine_same_radius": {"max_pct": round(100 * max(rel_hav, default=0), 5)},
        "one_km_band_flips": flips,
        "radius_ratio_pct": round(100 * (R_MONGO / R_MEAN - 1), 4),
    }
    ok = (agree_pct >= 99.5 and max(rel_sphere, default=0) <= TOL_SPHERE
          and max(rel_spheroid, default=0) <= TOL_SPHEROID)
    return check("V4", "Cross-engine distances", "pass" if ok else "fail",
                 "same nearest station >= 99.5 % (ties excluded); |Mongo - PostGIS sphere| <= 0.2 %; "
                 "|Mongo - PostGIS spheroid| <= 1.0 % (beyond the 0.05 m storage rounding)", m,
                 {"disagreement_examples": examples,
                  "explanation": "MongoDB measures on a 6,378.1 km sphere; PostGIS geography on the WGS84 "
                                 "spheroid. 'one_km_band_flips' counts blocks whose 'within 1 km' answer "
                                 "differs between the two models; they are reported as a known limitation."})


# --------------------------------------------------------------------------- V5
def v5(cur):
    mdb = db.mongo()
    tmp = mdb["_v5_probe"]
    tmp.drop()
    tmp.create_index([("loc", "2dsphere")])
    results = []
    base_lon, base_lat = 103.8500, 1.3000
    tmp.insert_one({"_id": "stn", "loc": {"type": "MultiPoint", "coordinates": [[base_lon, base_lat], [base_lon + 0.01, base_lat]]}})

    # (a) 1 km boundary on MongoDB's own sphere: 999 m inside, 1001 m outside.
    # (Not due east: that direction leads towards the station's second exit.)
    for bearing in (0, 180, 270):
        for d, expect in ((999, True), (1001, False)):
            lon, lat = destination(base_lon, base_lat, bearing, d, R_MONGO)
            got = list(tmp.aggregate([{"$geoNear": {"near": {"type": "Point", "coordinates": [lon, lat]},
                                                    "distanceField": "d", "spherical": True,
                                                    "maxDistance": 1000}}]))
            results.append({"case": f"{d} m at bearing {bearing}", "expected_inside": expect,
                            "mongo_inside": bool(got), "ok": bool(got) == expect})
    # The same probes against PostGIS's spheroid: reported, not asserted.
    pg_flips = 0
    for bearing in (0, 180, 270):
        for d in (999, 1001):
            lon, lat = destination(base_lon, base_lat, bearing, d, R_MONGO)
            cur.execute("SELECT ST_DWithin(ST_SetSRID(ST_Collect(ST_MakePoint(%s, %s), ST_MakePoint(%s, %s)), 4326)::geography, "
                        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 1000) AS inside",
                        [base_lon, base_lat, base_lon + 0.01, base_lat, lon, lat])
            pg_flips += int(cur.fetchone()["inside"] != (d == 999))

    # (b) MultiPoint: distance is to the NEAREST member.
    lon, lat = base_lon + 0.0101, base_lat
    got = list(tmp.aggregate([{"$geoNear": {"near": {"type": "Point", "coordinates": [lon, lat]},
                                            "distanceField": "d", "spherical": True}}]))[0]["d"]
    want = haversine_m(lon, lat, base_lon + 0.01, base_lat, R_MONGO)
    results.append({"case": "MultiPoint uses nearest exit", "expected_m": round(want, 1), "mongo_m": round(got, 1),
                    "ok": abs(got - want) < 0.5})

    # (c) $geoNear must be the first pipeline stage.
    try:
        list(tmp.aggregate([{"$match": {"_id": "stn"}}, {"$geoNear": {"near": {"type": "Point", "coordinates": [lon, lat]},
                                                           "distanceField": "d"}}]))
        results.append({"case": "$geoNear after $match is rejected", "ok": False})
    except OperationFailure:
        results.append({"case": "$geoNear after $match is rejected", "ok": True})

    # (d) Swapped [lat, lon] cannot be stored (latitude 103.85 is impossible).
    try:
        tmp.insert_one({"_id": "swapped", "loc": {"type": "Point", "coordinates": [base_lat, base_lon]}})
        results.append({"case": "swapped [lat, lon] rejected by the 2dsphere index", "ok": False})
    except WriteError:
        results.append({"case": "swapped [lat, lon] rejected by the 2dsphere index", "ok": True})
    tmp.drop()

    # (e) Real data: every stored block point really is [lon, lat] (lon ~104, lat ~1).
    bad_order = db.mongo().blocks_geo.count_documents({"loc.coordinates.0": {"$lt": 90}})
    results.append({"case": "all blocks_geo points stored as [lon, lat]", "count_wrong": bad_order, "ok": bad_order == 0})

    # (f) Opening years, when provided, are plausible.
    cur.execute("SELECT COUNT(*) FILTER (WHERE opened_year IS NOT NULL) AS with_year, COUNT(*) AS n FROM mrt_stations")
    oy = cur.fetchone()
    results.append({"case": "station opening years attached", "with_year": oy["with_year"], "stations": oy["n"],
                    "ok": True, "note": "optional (data/reference/station_opening_years.csv)"})

    ok = all(r["ok"] for r in results)
    return check("V5", "Edge cases", "pass" if ok else "fail",
                 "every probe behaves as specified", {"probes": len(results), "failed": sum(not r["ok"] for r in results),
                                                      "postgis_spheroid_boundary_flips": pg_flips},
                 {"probes": results})


# --------------------------------------------------------------------------- V6
def v6(cur):
    feats = _script("build_block_features")
    geocode = _script("geocode_blocks")
    cur.execute("SELECT block_id, nearest_station_id, nearest_station_name, nearest_mrt_m, stations_within_1km, "
                "mrt_band FROM block_features ORDER BY block_id")
    stored = [(r["block_id"], r["nearest_station_id"], r["nearest_station_name"],
               float(r["nearest_mrt_m"]) if r["nearest_mrt_m"] is not None else None,
               r["stations_within_1km"], r["mrt_band"]) for r in cur.fetchall()]
    fresh = feats.compute()
    a, b = feats.checksum(stored), feats.checksum(fresh)

    cur.execute("SELECT b.block_id, b.block_no, b.street_name, tw.town_name FROM blocks b "
                "JOIN towns tw USING (town_id) ORDER BY b.block_id")
    rows, _, _ = geocode.apply(cur.fetchall(), geocode.load_cache())
    cur.execute("SELECT block_id, lat, lon, postal, matched_address, match_quality FROM block_locations ORDER BY block_id")
    stored_loc = [tuple(r.values()) for r in cur.fetchall()]
    c, d = feats.checksum(sorted(rows)), feats.checksum(stored_loc)
    ok = a == b and c == d
    return check("V6", "Idempotent rebuild", "pass" if ok else "fail",
                 "re-running geocode (from cache) and feature build reproduces the stored tables exactly",
                 {"block_features_stored": a, "block_features_rebuilt": b,
                  "block_locations_stored": d, "block_locations_rebuilt": c})


def main():
    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    conn.autocommit = True
    checks = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for fn in (v1, v2, v3, v4, v5, v6):
            print(f"  running {fn.__name__.upper()} ...", flush=True)
            checks.append(fn(cur))
    conn.close()
    summary = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail", "pending")}
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "summary": summary, "checks": checks,
           "tolerances": {"sphere_pct": 100 * TOL_SPHERE, "spheroid_pct": 100 * TOL_SPHEROID, "tie_pct": 100 * TIE_REL},
           "radii_m": {"mongodb": R_MONGO, "postgis_sphere_and_haversine_reference": R_MEAN}}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "geo_validation.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    with open(os.path.join(OUT_DIR, "geo_validation.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Geospatial validation ({out['generated_at']})\n\n| Check | Status | Criterion | Key numbers |\n|---|---|---|---|\n")
        for c in checks:
            key = ", ".join(f"{k}={v}" for k, v in list(c["metrics"].items())[:5] if not isinstance(v, dict))
            fh.write(f"| {c['id']} {c['name']} | {c['status'].upper()} | {c['criterion']} | {key} |\n")
    for c in checks:
        print(f"{c['id']} {c['name']:24s} {c['status'].upper()}")
    print("Wrote data/results/geo_validation.json and .md")


if __name__ == "__main__":
    main()

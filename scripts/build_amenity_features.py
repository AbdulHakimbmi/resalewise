"""Per-block amenity features: how far is the nearest park / hawker centre /
gym / childcare / school, and how many are within 500 m, 1 km and 2 km?

    python scripts/build_amenity_features.py                    # MongoDB $geoNear (default)
    python scripts/build_amenity_features.py --engine postgis   # the same answer, set-based SQL
    python scripts/build_amenity_features.py --compare          # run both, write one, report the agreement

Both engines answer the same question from the same points (MongoDB `amenities`,
PostGIS `amenities`), so --compare is a check on the data AND a speed comparison
(per-block aggregation round trips vs one set-based query). The result is written
to PostgreSQL `block_amenity_features`, rebuilt from scratch on every run, and the
report goes to data/results/amenity_features_report.json.

Distances are straight-line. MongoDB uses a sphere and PostGIS the WGS84 spheroid,
so they can differ by up to about 0.3 %.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resalewise import amenities as am  # noqa: E402
from resalewise import config  # noqa: E402

RADII = (500, 1000, 2000)

POSTGIS_SQL = """
SELECT bl.block_id, k.kind,
       ROUND(nn.d::numeric, 1) AS nearest_m, nn.name AS nearest_name,
       c.n500, c.n1k, c.n2k
FROM block_locations bl
CROSS JOIN amenity_kinds k
CROSS JOIN LATERAL (
  SELECT a.name, ST_Distance(a.geog, bl.geog) AS d
  FROM amenities a WHERE a.kind = k.kind
  ORDER BY a.geog <-> bl.geog LIMIT 1
) nn
CROSS JOIN LATERAL (
  SELECT COUNT(*) FILTER (WHERE ST_Distance(a.geog, bl.geog) <= 500)::int  AS n500,
         COUNT(*) FILTER (WHERE ST_Distance(a.geog, bl.geog) <= 1000)::int AS n1k,
         COUNT(*)::int                                                    AS n2k
  FROM amenities a WHERE a.kind = k.kind AND ST_DWithin(a.geog, bl.geog, 2000)
) c
WHERE bl.lat IS NOT NULL
ORDER BY bl.block_id, k.kind
"""


def rows_postgis(conn):
    with conn.cursor() as cur:
        cur.execute(POSTGIS_SQL)
        return [(r[0], r[1], float(r[2]), r[3], r[4], r[5], r[6]) for r in cur.fetchall()]


def _near(doc):
    lon, lat = doc["loc"]["coordinates"]
    return {"type": "Point", "coordinates": [lon, lat]}


def block_rows_mongo(coll, doc, kinds):
    """One block: a $geoNear over every kind within 2 km (grouped by kind), then a
    nearest-only $geoNear for the kinds with nothing that close."""
    near = _near(doc)
    stage = {"near": near, "distanceField": "d", "spherical": True, "key": "loc"}
    got = {g["_id"]: g for g in coll.aggregate([
        {"$geoNear": dict(stage, maxDistance=RADII[-1])},
        {"$sort": {"d": 1}},
        {"$group": {"_id": "$kind", "nearest_m": {"$first": "$d"}, "nearest_name": {"$first": "$name"},
                    "n500": {"$sum": {"$cond": [{"$lte": ["$d", RADII[0]]}, 1, 0]}},
                    "n1k": {"$sum": {"$cond": [{"$lte": ["$d", RADII[1]]}, 1, 0]}},
                    "n2k": {"$sum": 1}}}])}
    rows = []
    for kind in kinds:
        g = got.get(kind)
        if g is None:                                     # nothing within 2 km: find the nearest anywhere
            far = list(coll.aggregate([{"$geoNear": dict(stage, query={"kind": kind})}, {"$limit": 1},
                                       {"$project": {"name": 1, "d": 1}}]))
            if not far:
                continue
            rows.append((doc["_id"], kind, round(far[0]["d"], 1), far[0]["name"], 0, 0, 0))
        else:
            rows.append((doc["_id"], kind, round(g["nearest_m"], 1), g["nearest_name"],
                         g["n500"], g["n1k"], g["n2k"]))
    return rows


def rows_mongo(mdb, workers=8):
    kinds = sorted(mdb.amenities.distinct("kind"))
    if not kinds:
        sys.exit("No amenities in MongoDB: run scripts/load_amenities.py first.")
    blocks = list(mdb.blocks_geo.find({}, {"loc": 1}).sort("_id", 1))
    if not blocks:
        sys.exit("No geocoded blocks in MongoDB `blocks_geo`: run scripts/geocode_blocks.py first.")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(lambda d: block_rows_mongo(mdb.amenities, d, kinds), blocks))
    return sorted((r for p in parts for r in p), key=lambda r: (r[0], r[1]))


def write_rows(conn, rows, engine):
    import psycopg2.extras
    with conn, conn.cursor() as cur:
        cur.execute("TRUNCATE block_amenity_features")
        psycopg2.extras.execute_values(cur, """
          INSERT INTO block_amenity_features (block_id, kind, nearest_m, nearest_name,
                                              within_500m, within_1km, within_2km, computed_by)
          VALUES %s""", [(*r, engine) for r in rows], page_size=3000)
        cur.execute("ANALYZE block_amenity_features")


def summary(rows):
    out = {}
    for kind in sorted({r[1] for r in rows}):
        d = sorted(r[2] for r in rows if r[1] == kind and r[2] is not None)
        if d:
            out[kind] = {"blocks": len(d), "median_nearest_m": round(am_median(d), 1),
                         "share_within_500m": round(100.0 * sum(1 for x in d if x <= 500) / len(d), 1),
                         "share_within_1km": round(100.0 * sum(1 for x in d if x <= 1000) / len(d), 1)}
    return out


def am_median(xs):
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=["mongo", "postgis"], default="mongo")
    ap.add_argument("--compare", action="store_true", help="run both engines, write the chosen one's rows")
    args = ap.parse_args()

    import psycopg2
    from resalewise import db
    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('amenities') IS NOT NULL")
        if not cur.fetchone()[0]:
            sys.exit("No `amenities` table yet: run scripts/load_amenities.py first.")
        cur.execute("SELECT COUNT(*) FROM amenities")
        if cur.fetchone()[0] == 0:
            sys.exit("`amenities` is empty: run scripts/load_amenities.py first.")
    conn.rollback()

    timings, results = {}, {}
    engines = ["mongo", "postgis"] if args.compare else [args.engine]
    for e in engines:
        t0 = time.time()
        results[e] = rows_mongo(db.mongo()) if e == "mongo" else rows_postgis(conn)
        timings[e] = round(time.time() - t0, 2)
        print(f"{e:8s} {len(results[e]):,} block x kind rows in {timings[e]:.1f} s")
    chosen = args.engine
    write_rows(conn, results[chosen], "mongodb $geoNear" if chosen == "mongo" else "postgis")
    conn.close()

    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "written_by": chosen, "seconds": timings, "rows": len(results[chosen]),
              "per_kind": summary(results[chosen])}
    if args.compare:
        report["agreement"] = am.compare_rows(results["mongo"], results["postgis"])
        a = report["agreement"]
        print(f"Agreement: nearest distance within tolerance for {a['nearest_distance_agree_pct']} % of "
              f"{a['pairs_compared']:,} pairs (max difference {a['max_abs_diff_m']} m); "
              f"identical counts for {a['counts_identical_pct']} %.")
    for kind, s in report["per_kind"].items():
        print(f"  {kind:17s} median nearest {s['median_nearest_m']:7.1f} m   "
              f"{s['share_within_500m']:5.1f} % of blocks within 500 m, {s['share_within_1km']:5.1f} % within 1 km")
    os.makedirs(os.path.join(config.ROOT, "data", "results"), exist_ok=True)
    with open(os.path.join(config.ROOT, "data", "results", "amenity_features_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print("Wrote block_amenity_features and data/results/amenity_features_report.json. "
          "Next: python analysis/amenity_model.py")


if __name__ == "__main__":
    main()

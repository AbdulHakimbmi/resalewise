"""Step 3 of the geospatial pipeline: nearest station for every block.

    python scripts/build_block_features.py

For each geocoded block (MongoDB `blocks_geo`) it runs two $geoNear
aggregations against `places`:
    nearest station + distance      [$geoNear, $limit 1]
    stations within 1 km            [$geoNear maxDistance 1000, $count]
and writes the result to PostgreSQL `block_features`, where it can be joined
to transactions (price-driver analysis) and cross-checked against PostGIS
(scripts/validate_geo.py). The table is rebuilt from scratch every run and
the rows are written in block order, so two runs give identical output; the
checksum printed at the end is what validation check V6 compares.
"""
import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from resalewise import config, db  # noqa: E402
from resalewise.geo import mrt_band  # noqa: E402

RADIUS_M = 1000


def features_for(places, doc):
    lon, lat = doc["loc"]["coordinates"]
    near = {"type": "Point", "coordinates": [lon, lat]}
    nearest = list(places.aggregate([
        {"$geoNear": {"near": near, "distanceField": "d", "spherical": True, "key": "loc"}},
        {"$limit": 1}, {"$project": {"name": 1, "d": 1}}]))
    within = list(places.aggregate([
        {"$geoNear": {"near": near, "distanceField": "d", "spherical": True, "key": "loc",
                      "maxDistance": RADIUS_M}},
        {"$count": "n"}]))
    if not nearest:
        return (doc["_id"], None, None, None, 0, None)
    d = round(nearest[0]["d"], 1)
    return (doc["_id"], nearest[0]["_id"], nearest[0]["name"], d,
            within[0]["n"] if within else 0, mrt_band(d))


def checksum(rows):
    h = hashlib.sha256()
    for r in rows:
        h.update(("|".join("" if v is None else str(v) for v in r) + "\n").encode())
    return h.hexdigest()[:16]


def compute(workers=8):
    mdb = db.mongo()
    places = mdb.places
    if places.estimated_document_count() == 0:
        sys.exit("No stations in MongoDB `places`: run scripts/load_stations.py first.")
    blocks = list(mdb.blocks_geo.find({}, {"loc": 1}).sort("_id", 1))
    if not blocks:
        sys.exit("No geocoded blocks in MongoDB `blocks_geo`: run scripts/geocode_blocks.py first.")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda d: features_for(places, d), blocks))
    rows.sort(key=lambda r: r[0])
    return rows


def main():
    t0 = time.time()
    rows = compute()
    took = time.time() - t0
    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    with conn, conn.cursor() as cur:
        cur.execute("TRUNCATE block_features")
        psycopg2.extras.execute_values(cur, """
          INSERT INTO block_features (block_id, nearest_station_id, nearest_station_name,
                                      nearest_mrt_m, stations_within_1km, mrt_band)
          VALUES %s""", rows, page_size=2000)
        cur.execute("ANALYZE block_features")
    conn.close()
    bands = {}
    for r in rows:
        bands[r[5]] = bands.get(r[5], 0) + 1
    print(f"block_features: {len(rows):,} blocks in {took:.1f} s "
          f"({2 * len(rows) / max(took, 1e-9):,.0f} $geoNear queries/s). Bands: {bands}")
    print(f"checksum {checksum(rows)}   Next: python scripts/validate_geo.py")


if __name__ == "__main__":
    main()

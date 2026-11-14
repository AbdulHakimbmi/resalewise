"""Step 1 of the geospatial pipeline: find every HDB block on the map.

    python scripts/geocode_blocks.py              # fetch what's missing, then apply
    python scripts/geocode_blocks.py --apply-only # rebuild tables from the cache (no network)
    python scripts/geocode_blocks.py --retry-unmatched

For each block in PostgreSQL it asks OneMap's public search API for
"<block> <street>", keeps the result only if the block number AND road match
(see resalewise/geo.py: match_onemap), and stores the point in:

  PostgreSQL  block_locations   (lat/lon + generated PostGIS geography column)
  MongoDB     blocks_geo        (GeoJSON Point, 2dsphere index)

Every API response is appended to data/geo/geocode_cache.jsonl as it
arrives, so an interrupted run resumes where it stopped and re-applying the
cache is deterministic (validation check V6). OneMap allows ~250 requests per
minute; the default pace (3.5/s) stays under that. ~10,000 blocks take about
50 minutes the first time.
"""
import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from pymongo import ReplaceOne  # noqa: E402

from resalewise import config, db  # noqa: E402
from resalewise.geo import match_onemap  # noqa: E402
from resalewise.mongo_setup import ensure_collection  # noqa: E402

ONEMAP_BASE = os.environ.get("ONEMAP_BASE", "https://www.onemap.gov.sg")
SEARCH_PATH = "/api/common/elastic/search"
CACHE = os.path.join(config.ROOT, "data", "geo", "geocode_cache.jsonl")


def key_of(block_no, street):
    return f"{block_no}|{street}"


def load_cache():
    cache = {}
    if os.path.isfile(CACHE):
        with open(CACHE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    cache[rec["key"]] = rec          # the latest line for a key wins
    return cache


def onemap_search(query, retries=5):
    params = urllib.parse.urlencode({"searchVal": query, "returnGeom": "Y",
                                     "getAddrDetails": "Y", "pageNum": 1})
    req = urllib.request.Request(f"{ONEMAP_BASE}{SEARCH_PATH}?{params}",
                                 headers={"User-Agent": "ResaleWiseSG/1.0 (student project)"})
    token = os.environ.get("ONEMAP_TOKEN")
    if token:
        req.add_header("Authorization", token)
    delay = 2.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay); delay *= 2
                continue
            return e.code, None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(delay); delay *= 2
                continue
            raise SystemExit(f"\nCannot reach OneMap ({e}). Check your internet connection; "
                             "progress so far is saved, just run the script again.")
    return None, None


def fetch(blocks, cache, pace, retry_unmatched, workers=1):
    todo = []
    for b in blocks:
        rec = cache.get(key_of(b["block_no"], b["street_name"]))
        if rec is None:
            todo.append(b)
        elif retry_unmatched and match_onemap(b["block_no"], b["street_name"], rec.get("results"))[0] == "unmatched":
            todo.append(b)
    if not todo:
        print("Cache already has every block.")
        return
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    print(f"Geocoding {len(todo):,} blocks via OneMap (~{len(todo) / pace / 60:.0f} min). Ctrl+C is safe.")
    t0 = time.time()
    tty = sys.stdout.isatty()
    keep = ("SEARCHVAL", "BLK_NO", "ROAD_NAME", "BUILDING", "ADDRESS", "POSTAL", "LATITUDE", "LONGITUDE")
    lock = threading.Lock()
    gate = {"next": time.time()}

    def work(b):
        # Start requests at most `pace` per second in total, however many workers run.
        with lock:
            now = time.time()
            wait = max(0.0, gate["next"] - now)
            gate["next"] = max(now, gate["next"]) + 1.0 / pace
        if wait:
            time.sleep(wait)
        status, body = onemap_search(f"{b['block_no']} {b['street_name']}")
        results = (body or {}).get("results", [])[:10]
        return {"key": key_of(b["block_no"], b["street_name"]), "status": status,
                "found": (body or {}).get("found"),
                "results": [{k: r.get(k) for k in keep} for r in results],
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    with open(CACHE, "a", encoding="utf-8") as out, \
            concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        # Only the main thread writes the cache; a bounded window keeps Ctrl+C responsive.
        it = iter(todo)
        pending = set()
        done_n = 0
        try:
            while True:
                while len(pending) < workers * 2:
                    b = next(it, None)
                    if b is None:
                        break
                    pending.add(pool.submit(work, b))
                if not pending:
                    break
                finished, pending = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for f in finished:
                    rec = f.result()
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    cache[rec["key"]] = rec
                    done_n += 1
                    if done_n % (50 if tty else 500) == 0 or done_n == len(todo):
                        rate = done_n / (time.time() - t0)
                        msg = f"  {done_n:,}/{len(todo):,}  ({rate:.1f}/s, ~{(len(todo) - done_n) / max(rate, 0.1) / 60:.0f} min left)"
                        print(("\r" + msg) if tty else msg, end="" if tty else "\n", flush=True)
        except KeyboardInterrupt:
            for f in pending:
                f.cancel()
            print("\nStopped. Progress is saved; run the same command to continue.")
            raise SystemExit(130)
    print()


def apply(blocks, cache):
    """Turn cached responses into rows. Pure function of (blocks, cache), so
    running it twice gives identical tables."""
    rows, docs, counts = [], [], {"exact": 0, "fuzzy": 0, "unmatched": 0, "not_fetched": 0}
    for b in blocks:
        rec = cache.get(key_of(b["block_no"], b["street_name"]))
        if rec is None:
            counts["not_fetched"] += 1
            continue
        quality, r, _ = match_onemap(b["block_no"], b["street_name"], rec.get("results"))
        counts[quality] += 1
        if r is None:
            rows.append((b["block_id"], None, None, None, None, "unmatched"))
            continue
        lat, lon = float(r["LATITUDE"]), float(r["LONGITUDE"])
        rows.append((b["block_id"], lat, lon, (r.get("POSTAL") or "")[:6] or None, r.get("ADDRESS"), quality))
        docs.append(ReplaceOne({"_id": b["block_id"]}, {
            "_id": b["block_id"], "town": b["town_name"], "block_no": b["block_no"], "street": b["street_name"],
            "match_quality": quality, "loc": {"type": "Point", "coordinates": [lon, lat]}}, upsert=True))
    return rows, docs, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply-only", action="store_true", help="no network: rebuild tables from the cache")
    ap.add_argument("--retry-unmatched", action="store_true", help="ask OneMap again for unmatched blocks")
    ap.add_argument("--pace", type=float, default=3.5, help="requests per second (OneMap allows ~4)")
    ap.add_argument("--workers", type=int, default=4, help="parallel requests (total still capped by --pace)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N blocks (testing)")
    args = ap.parse_args()

    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    with conn, conn.cursor() as cur:
        with open(os.path.join(config.ROOT, "sql", "geo.sql"), encoding="utf-8") as fh:
            cur.execute(fh.read())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT b.block_id, b.block_no, b.street_name, tw.town_name FROM blocks b "
                    "JOIN towns tw USING (town_id) ORDER BY b.block_id")
        blocks = cur.fetchall()
    if args.limit:
        blocks = blocks[:args.limit]

    cache = load_cache()
    if not args.apply_only:
        fetch(blocks, cache, args.pace, args.retry_unmatched, args.workers)

    rows, docs, counts = apply(blocks, cache)
    with conn, conn.cursor() as cur:
        cur.execute("TRUNCATE block_locations CASCADE")
        psycopg2.extras.execute_values(cur, """
            INSERT INTO block_locations (block_id, lat, lon, postal, matched_address, match_quality)
            VALUES %s""", rows, page_size=2000)
    conn.close()

    coll = ensure_collection(db.mongo(), "blocks_geo")
    coll.delete_many({})
    for i in range(0, len(docs), 2000):
        coll.bulk_write(docs[i:i + 2000], ordered=False)

    total = len(blocks)
    located = counts["exact"] + counts["fuzzy"]
    print(f"Blocks: {total:,}  exact {counts['exact']:,}  fuzzy {counts['fuzzy']:,}  "
          f"unmatched {counts['unmatched']:,}  not fetched {counts['not_fetched']:,}")
    print(f"Located {located:,} of {total:,} ({100 * located / max(total, 1):.1f}%). "
          "Next: python scripts/load_stations.py")


if __name__ == "__main__":
    main()

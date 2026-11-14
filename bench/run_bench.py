"""SQL vs NoSQL benchmark: the same questions asked of PostgreSQL and MongoDB.

    python bench/run_bench.py                     # full protocol (~10-15 min)
    python bench/run_bench.py --quick             # smoke test (~1 min)
    python bench/run_bench.py --skip-geo          # before the geospatial pipeline has run
    python bench/run_bench.py --no-index          # W0-W3 again with NO secondary indexes (~5 min)

Configurations
  postgres       the application's real schema: 5 normalised tables + joins
  postgres_flat  the same rows denormalised into one table (materialized view
                 bench_txn_flat) with the SAME indexes as MongoDB. Comparing
                 the three separates the data-model effect (joins vs one
                 document) from the engine effect (PostgreSQL vs MongoDB).
  mongodb        denormalised documents (collection `transactions`)

Workloads
  W0  primary-key lookup            one sale by id (baseline)
  W1  filtered search               town + flat type + budget, newest 50
  W2  median by town                exact PERCENTILE_CONT vs $median (approximate)
  W3  quarterly trend               medians per quarter for one town + type, 1990-2026
  W4  nearest station               PostGIS KNN (GiST) vs $geoNear (2dsphere)
  W5  blocks within 1 km            ST_DWithin vs $geoWithin/$centerSphere
  W6  bulk load                     COPY vs insert_many, rows per second

Protocol
  1. Equivalence first: every workload is checked on 20 parameter sets; a
     configuration that returns a different answer is reported, not timed.
  2. Fixed-seed parameters, replayed in the same order for every
     configuration; the order of configurations rotates each run so drift
     (cache warming, background work) is spread evenly.
  3. Warm-up runs are discarded; then N measured runs per configuration.
  4. Client-observed latency (includes driver decoding), p50/p95/p99 and a
     bootstrap 95 % confidence interval for the median.
  5. Throughput under concurrency (1, 4, 8 threads) for W1 and W4.
  6. Query plans: EXPLAIN (ANALYZE, BUFFERS) and explain("executionStats").
  7. Environment (versions, settings, CPU) and the index parity table are
     saved with the results.
Writes data/results/benchmark.json (+ raw latencies in bench/results/raw/).

--no-index is the control experiment for step 3 of the protocol. It copies the
flat table and the `transactions` collection WITHOUT any secondary index (only
the primary key / _id that both engines always keep), repeats W0-W3 on the
copies and writes data/results/benchmark_noindex.json, including how many
times slower each query is than in the indexed run. It answers "how much of
the speed comes from the engine and how much from the indexes we designed?".
Run the full benchmark first; the copies are dropped when the run ends.
"""
import argparse
import csv
import io
import json
import os
import platform
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from resalewise import config, db as rdb  # noqa: E402
from resalewise.geo import R_MONGO  # noqa: E402

RAW_DIR = os.path.join(ROOT, "bench", "results", "raw")
OUT = os.path.join(ROOT, "data", "results", "benchmark.json")

FLAT_SQL = """
CREATE MATERIALIZED VIEW IF NOT EXISTS bench_txn_flat AS
SELECT txn_id, month, town_name, flat_type_name, flat_model_name, block_id, block_no, street_name,
       storey_low, storey_high, floor_area_sqm, lease_commence_date, remaining_lease_months, resale_price
FROM v_transactions;
CREATE UNIQUE INDEX IF NOT EXISTS bench_flat_pk         ON bench_txn_flat (txn_id);
CREATE INDEX IF NOT EXISTS bench_flat_month_desc_id     ON bench_txn_flat (month DESC, txn_id DESC);
-- Equality (town, type) -> Sort (month, id) -> Range (price): MongoDB's ESR rule, applied to both engines.
CREATE INDEX IF NOT EXISTS bench_flat_town_type_month_id ON bench_txn_flat (town_name, flat_type_name, month DESC, txn_id DESC);
CREATE INDEX IF NOT EXISTS bench_flat_type_month        ON bench_txn_flat (flat_type_name, month);
CREATE INDEX IF NOT EXISTS bench_flat_price             ON bench_txn_flat (resale_price);
CREATE INDEX IF NOT EXISTS bench_flat_block             ON bench_txn_flat (block_id);
ANALYZE bench_txn_flat;
"""

COLS_NORM = """t.txn_id, t.month, tw.town_name, ft.flat_type_name, fm.flat_model_name, b.block_no, b.street_name,
               t.storey_low, t.storey_high, t.floor_area_sqm, t.remaining_lease_months, t.resale_price"""
JOINS = """FROM resale_transactions t
           JOIN blocks b       ON b.block_id = t.block_id
           JOIN towns tw       ON tw.town_id = b.town_id
           JOIN flat_types ft  ON ft.flat_type_id = t.flat_type_id
           JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id"""
COLS_FLAT = """txn_id, month, town_name, flat_type_name, flat_model_name, block_no, street_name,
               storey_low, storey_high, floor_area_sqm, remaining_lease_months, resale_price"""
# The table and collection that the postgres_flat / mongodb configurations query. `--no-index` points
# them at copies that carry only a primary key / _id index (see prepare_unindexed).
FLAT = "bench_txn_flat"
TXN = "transactions"
PROJ = {"month": 1, "town": 1, "flat_type": 1, "flat_model": 1, "block_no": 1, "street": 1, "storey_low": 1,
        "storey_high": 1, "floor_area_sqm": 1, "remaining_lease_months": 1, "price": 1}


# ----------------------------------------------------------------------------
# Connections: one psycopg2 connection per thread (they are not thread-safe);
# the MongoDB client is thread-safe and shared.
# ----------------------------------------------------------------------------
_local = threading.local()


def pg():
    c = getattr(_local, "conn", None)
    if c is None or c.closed:
        c = psycopg2.connect(config.database_url(), connect_timeout=15)
        c.autocommit = True
        _local.conn = c
    return c.cursor()


def mongo():
    return rdb.mongo()


# ----------------------------------------------------------------------------
# Workloads. Each run_* returns a comparable answer for the equivalence check.
# ----------------------------------------------------------------------------
class Workload:
    id = title = what = ""
    needs_geo = False
    configs = ("postgres", "postgres_flat", "mongodb")

    def params(self, rng, n):
        raise NotImplementedError

    def same(self, a, b):
        return a == b, None


class W0(Workload):
    id, title, what = "W0", "Primary-key lookup", "one sale by its id (baseline)"

    def __init__(self, ctx):
        self.max_id = ctx["max_txn_id"]

    def params(self, rng, n):
        return [{"id": rng.randint(1, self.max_id)} for _ in range(n)]

    def postgres(self, p):
        cur = pg()
        cur.execute(f"SELECT {COLS_NORM} {JOINS} WHERE t.txn_id = %s", [p["id"]])
        r = cur.fetchone()
        return r[0] if r else None

    def postgres_flat(self, p):
        cur = pg()
        cur.execute(f"SELECT {COLS_FLAT} FROM {FLAT} WHERE txn_id = %s", [p["id"]])
        r = cur.fetchone()
        return r[0] if r else None

    def mongodb(self, p):
        d = mongo()[TXN].find_one({"_id": p["id"]}, PROJ)
        return d["_id"] if d else None


class W1(Workload):
    id, title, what = "W1", "Filtered search", "town + flat type + budget, newest 50 (the search page)"

    def __init__(self, ctx):
        self.combos = ctx["combos"]

    def params(self, rng, n):
        out = []
        for _ in range(n):
            c = rng.choice(self.combos)
            out.append({"town": c["town_name"], "flat_type": c["flat_type_name"], "budget": int(c["p75"])})
        return out

    def postgres(self, p):
        cur = pg()
        cur.execute(f"""SELECT {COLS_NORM} {JOINS}
                        WHERE tw.town_name = %s AND ft.flat_type_name = %s AND t.resale_price <= %s
                        ORDER BY t.month DESC, t.txn_id DESC LIMIT 50""", [p["town"], p["flat_type"], p["budget"]])
        return [r[0] for r in cur.fetchall()]

    def postgres_flat(self, p):
        cur = pg()
        cur.execute(f"""SELECT {COLS_FLAT} FROM {FLAT}
                        WHERE town_name = %s AND flat_type_name = %s AND resale_price <= %s
                        ORDER BY month DESC, txn_id DESC LIMIT 50""", [p["town"], p["flat_type"], p["budget"]])
        return [r[0] for r in cur.fetchall()]

    def mongodb(self, p):
        docs = mongo()[TXN].find({"town": p["town"], "flat_type": p["flat_type"],
                                          "price": {"$lte": p["budget"]}}, PROJ) \
            .sort([("month", -1), ("_id", -1)]).limit(50)
        return [d["_id"] for d in docs]


def _medians_close(a, b, tol=0.001):
    """Same groups and the same median in each (within `tol` relative).
    PostgreSQL computes PERCENTILE_DISC(0.5) here: like MongoDB's $median it
    returns an observed value (the lower middle one), whereas the app's
    PERCENTILE_CONT interpolates between the two middle values. Comparing
    CONT with $median mixes a definition difference into the engine
    comparison; `semantics` below measures that difference separately."""
    if set(a) != set(b):
        return False, {"missing": sorted(set(a) ^ set(b))[:5]}
    worst = max((abs(a[k] - b[k]) / max(abs(a[k]), 1) for k in a), default=0)
    return worst <= tol, {"max_rel_diff_pct": round(100 * worst, 3)}


class W2(Workload):
    id, title, what = "W2", "Median by town", "median price per town for one flat type, last 24 months"

    def __init__(self, ctx):
        self.cutoff = ctx["cutoff_24m"]
        self.types = ["3 ROOM", "4 ROOM", "5 ROOM", "EXECUTIVE"]

    def params(self, rng, n):
        return [{"flat_type": rng.choice(self.types)} for _ in range(n)]

    def postgres(self, p):
        cur = pg()
        cur.execute(f"""SELECT tw.town_name, PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY t.resale_price)
                        {JOINS} WHERE ft.flat_type_name = %s AND t.month >= %s GROUP BY tw.town_name""",
                    [p["flat_type"], self.cutoff])
        return {k: float(v) for k, v in cur.fetchall()}

    def postgres_flat(self, p):
        cur = pg()
        cur.execute(f"""SELECT town_name, PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY resale_price)
                       FROM {FLAT} WHERE flat_type_name = %s AND month >= %s GROUP BY town_name""",
                    [p["flat_type"], self.cutoff])
        return {k: float(v) for k, v in cur.fetchall()}

    def mongodb(self, p):
        rows = mongo()[TXN].aggregate([
            {"$match": {"flat_type": p["flat_type"], "month": {"$gte": self.cutoff}}},
            {"$group": {"_id": "$town", "m": {"$median": {"input": "$price", "method": "approximate"}}}}])
        return {d["_id"]: float(d["m"]) for d in rows}

    def same(self, a, b):
        return _medians_close(a, b)


class W3(Workload):
    id, title, what = "W3", "Quarterly trend", "median price per quarter for one town + flat type, all years"

    def __init__(self, ctx):
        self.combos = ctx["combos"]

    def params(self, rng, n):
        return [{"town": c["town_name"], "flat_type": c["flat_type_name"]}
                for c in (rng.choice(self.combos) for _ in range(n))]

    def postgres(self, p):
        cur = pg()
        cur.execute(f"""SELECT DATE_TRUNC('quarter', t.month)::date,
                               PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY t.resale_price)
                        {JOINS} WHERE tw.town_name = %s AND ft.flat_type_name = %s GROUP BY 1""",
                    [p["town"], p["flat_type"]])
        return {k.isoformat(): float(v) for k, v in cur.fetchall()}

    def postgres_flat(self, p):
        cur = pg()
        cur.execute(f"""SELECT DATE_TRUNC('quarter', month)::date,
                              PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY resale_price)
                       FROM {FLAT} WHERE town_name = %s AND flat_type_name = %s GROUP BY 1""",
                    [p["town"], p["flat_type"]])
        return {k.isoformat(): float(v) for k, v in cur.fetchall()}

    def mongodb(self, p):
        rows = mongo()[TXN].aggregate([
            {"$match": {"town": p["town"], "flat_type": p["flat_type"]}},
            {"$group": {"_id": {"$dateTrunc": {"date": "$month", "unit": "quarter"}},
                        "m": {"$median": {"input": "$price", "method": "approximate"}}}}])
        return {d["_id"].date().isoformat(): float(d["m"]) for d in rows}

    def same(self, a, b):
        return _medians_close(a, b)


class W4(Workload):
    id, title, what = "W4", "Nearest station", "nearest MRT/LRT station to a block"
    needs_geo = True
    configs = ("postgres", "mongodb")

    def __init__(self, ctx):
        self.points = ctx["block_points"]

    def params(self, rng, n):
        return [dict(zip(("lon", "lat"), rng.choice(self.points))) for _ in range(n)]

    def postgres(self, p):
        cur = pg()
        cur.execute("""SELECT station_id, ST_Distance(geog, pt, false)
                       FROM mrt_stations, (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography AS pt) q
                       ORDER BY geog <-> pt LIMIT 1""", [p["lon"], p["lat"]])
        r = cur.fetchone()
        return (r[0], r[1])

    def mongodb(self, p):
        d = next(mongo().places.aggregate([
            {"$geoNear": {"near": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                          "distanceField": "d", "spherical": True, "key": "loc"}}, {"$limit": 1}]))
        return (d["_id"], d["d"])

    def same(self, a, b):
        ok = a[0] == b[0] and abs(a[1] - b[1]) <= 0.002 * max(a[1], 1) + 0.05
        return ok, {"pg": a, "mongo": b} if not ok else None


class W5(Workload):
    id, title, what = "W5", "Blocks within 1 km", "every block within 1 km of a station"
    needs_geo = True
    configs = ("postgres", "mongodb")

    def __init__(self, ctx):
        self.stations = ctx["station_points"]

    def params(self, rng, n):
        return [dict(zip(("lon", "lat"), rng.choice(self.stations))) for _ in range(n)]

    def postgres(self, p):
        cur = pg()
        # use_spheroid = false: a sphere, like MongoDB (the radius still differs by 0.11 %)
        cur.execute("""SELECT block_id FROM block_locations
                       WHERE ST_DWithin(geog, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 1000, false)""",
                    [p["lon"], p["lat"]])
        return {r[0] for r in cur.fetchall()}

    def mongodb(self, p):
        return {d["_id"] for d in mongo().blocks_geo.find(
            {"loc": {"$geoWithin": {"$centerSphere": [[p["lon"], p["lat"]], 1000 / R_MONGO]}}}, {"_id": 1})}

    def same(self, a, b):
        union = a | b
        j = len(a & b) / len(union) if union else 1.0
        return j >= 0.98, {"jaccard": round(j, 4), "only_pg": len(a - b), "only_mongo": len(b - a)}


# ----------------------------------------------------------------------------
def context(skip_geo):
    cur = pg()
    cur.execute(FLAT_SQL)
    cur.execute("SELECT MAX(txn_id), MAX(month) FROM resale_transactions")
    max_id, max_month = cur.fetchone()
    m = max_month.month - 24
    cutoff = max_month.replace(year=max_month.year + (m - 1) // 12, month=(m - 1) % 12 + 1)
    cur.execute("""SELECT town_name, flat_type_name, COUNT(*) AS n,
                          PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY resale_price) AS p75
                   FROM bench_txn_flat WHERE month >= %s GROUP BY 1, 2 HAVING COUNT(*) >= 300
                   ORDER BY 1, 2""", [cutoff])
    combos = [dict(zip(("town_name", "flat_type_name", "n", "p75"), r)) for r in cur.fetchall()]
    for c in combos:
        c["p75"] = round(float(c["p75"]), -3)
    ctx = {"max_txn_id": max_id, "cutoff_24m": datetime(cutoff.year, cutoff.month, 1), "combos": combos,
           "geo": False}
    if not skip_geo:
        cur.execute("SELECT to_regclass('block_locations') IS NOT NULL AND to_regclass('mrt_stations') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT lon, lat FROM block_locations WHERE lat IS NOT NULL ORDER BY block_id")
            ctx["block_points"] = [tuple(r) for r in cur.fetchall()]
            cur.execute("SELECT ST_X(centroid::geometry), ST_Y(centroid::geometry) FROM mrt_stations ORDER BY station_id")
            ctx["station_points"] = [tuple(r) for r in cur.fetchall()]
            ctx["geo"] = bool(ctx["block_points"] and ctx["station_points"]
                              and mongo().places.estimated_document_count()
                              and mongo().blocks_geo.estimated_document_count())
    return ctx


def median_semantics(ctx):
    """How often does the interpolated median (PERCENTILE_CONT, used by the
    app) differ from the observed-value median (PERCENTILE_DISC = MongoDB's
    $median here)? Over every town x flat type x quarter group."""
    cur = pg()
    cur.execute("""SELECT COUNT(*),
                          COUNT(*) FILTER (WHERE c <> d),
                          MAX(ABS(c - d) / d)
                   FROM (SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY resale_price) AS c,
                                PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY resale_price) AS d
                         FROM bench_txn_flat GROUP BY town_name, flat_type_name, DATE_TRUNC('quarter', month)) g""")
    n, differ, worst = cur.fetchone()
    return {"groups": n, "cont_differs_from_disc": differ, "pct_groups": round(100 * differ / max(n, 1), 1),
            "max_rel_diff_pct": round(100 * float(worst or 0), 2),
            "note": "MongoDB's $median returned exactly PERCENTILE_DISC(0.5) in the equivalence checks; "
                    "the app's PERCENTILE_CONT averages the two middle values when the count is even."}


def timed(fn, p):
    t0 = time.perf_counter()
    out = fn(p)
    return (time.perf_counter() - t0) * 1000.0, out


def summarise(ms, rng):
    a = np.asarray(ms)
    boots = np.median(rng.choice(a, size=(2000, len(a)), replace=True), axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    r = lambda x: round(float(x), 3)   # noqa: E731
    return {"n": len(a), "mean_ms": r(a.mean()), "p50_ms": r(np.median(a)), "p50_ci95": [r(lo), r(hi)],
            "p95_ms": r(np.percentile(a, 95)), "p99_ms": r(np.percentile(a, 99)),
            "min_ms": r(a.min()), "max_ms": r(a.max())}


def equivalence(w, params):
    """Run every configuration on the same parameters and compare each answer
    with the first configuration's. Failures are recorded with the inputs."""
    checked, passed, failures, notes = 0, 0, [], []
    reference = w.configs[0]
    for p in params:
        answers = {c: getattr(w, c)(p) for c in w.configs}
        ok_all = True
        for c in w.configs[1:]:
            ok, d = w.same(answers[reference], answers[c])
            if not ok:
                ok_all = False
                if len(failures) < 5:
                    failures.append({"params": p, "config": c, "detail": d})
            elif d and len(notes) < 3:
                notes.append({"config": c, **d})
        checked += 1
        passed += ok_all
    return {"checked": checked, "passed": passed, "failures": failures, "notes": notes}


def measure(w, params, warmup, stat_rng):
    lat = {c: [] for c in w.configs}
    for i, p in enumerate(params):
        order = w.configs[i % len(w.configs):] + w.configs[:i % len(w.configs)]   # rotate
        for c in order:
            ms, _ = timed(getattr(w, c), p)
            if i >= warmup:
                lat[c].append(ms)
    os.makedirs(RAW_DIR, exist_ok=True)
    with open(os.path.join(RAW_DIR, f"{w.id}.csv"), "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["run"] + list(w.configs))
        for i in range(len(lat[w.configs[0]])):
            wr.writerow([i] + [round(lat[c][i], 4) for c in w.configs])
    return {c: summarise(v, stat_rng) for c, v in lat.items()}


def concurrency(w, params, threads, seconds):
    out = []
    for c in w.configs:
        fn = getattr(w, c)
        for n in threads:
            stop = time.perf_counter() + seconds
            lat, lock = [], threading.Lock()

            def worker(k):
                i = k
                while time.perf_counter() < stop:
                    ms, _ = timed(fn, params[i % len(params)])
                    with lock:
                        lat.append(ms)
                    i += n
            with ThreadPoolExecutor(max_workers=n) as ex:
                list(ex.map(worker, range(n)))
            a = np.asarray(lat)
            out.append({"workload": w.id, "config": c, "threads": n, "ops_per_s": round(len(a) / seconds, 1),
                        "p50_ms": round(float(np.median(a)), 3), "p95_ms": round(float(np.percentile(a, 95)), 3)})
            print(f"    {w.id} {c:14s} {n} threads: {out[-1]['ops_per_s']:8.1f} ops/s  p95 {out[-1]['p95_ms']:.2f} ms")
    return out


def bulk_load(rows_n, repeats):
    cur = pg()
    cur.execute(f"SELECT {COLS_FLAT} FROM bench_txn_flat ORDER BY txn_id LIMIT %s", [rows_n])
    rows = cur.fetchall()
    buf_text = io.StringIO()
    csv.writer(buf_text).writerows(rows)
    text = buf_text.getvalue()
    keys = ["_id", "month", "town", "flat_type", "flat_model", "block_no", "street", "storey_low",
            "storey_high", "floor_area_sqm", "remaining_lease_months", "price"]
    docs = [dict(zip(keys, (r[0], datetime(r[1].year, r[1].month, 1), *r[2:9], float(r[9]), r[10], float(r[11]))))
            for r in rows]
    res = {"rows": len(rows), "postgres_copy": [], "mongodb_insert_many": []}
    mdb = mongo()
    for _ in range(repeats):
        cur.execute("DROP TABLE IF EXISTS bench_load; CREATE TABLE bench_load (LIKE bench_txn_flat); "
                    "ALTER TABLE bench_load ADD PRIMARY KEY (txn_id)")
        t0 = time.perf_counter()
        cur.copy_expert(f"COPY bench_load ({COLS_FLAT.replace(chr(10), ' ')}) FROM STDIN WITH (FORMAT csv)",
                        io.StringIO(text))
        res["postgres_copy"].append(round(len(rows) / (time.perf_counter() - t0)))
        mdb.bench_load.drop()
        t0 = time.perf_counter()
        for i in range(0, len(docs), 10_000):
            mdb.bench_load.insert_many([dict(d) for d in docs[i:i + 10_000]], ordered=False)
        res["mongodb_insert_many"].append(round(len(rows) / (time.perf_counter() - t0)))
    cur.execute("DROP TABLE IF EXISTS bench_load")
    mdb.bench_load.drop()
    for k in ("postgres_copy", "mongodb_insert_many"):
        v = res[k]
        res[k] = {"rows_per_s_median": int(np.median(v)), "runs": v}
    return res


def _walk(node, key):
    """Yield every value stored under `key` anywhere in a nested plan."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def explain_pg(sql, params):
    cur = pg()
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
    plan = cur.fetchone()[0][0]
    return {"top_node": plan["Plan"]["Node Type"], "nodes": sorted(set(_walk(plan, "Node Type"))),
            "indexes": sorted(set(_walk(plan, "Index Name"))), "execution_ms": round(plan["Execution Time"], 3),
            "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks"),
            "shared_read_blocks": plan["Plan"].get("Shared Read Blocks")}


def explain_mongo(command):
    out = mongo().command({"explain": command, "verbosity": "executionStats"})
    winning = next(_walk(out, "winningPlan"), out)          # ignore the rejected candidate plans
    return {"stages": sorted(set(_walk(winning, "stage"))), "indexes": sorted(set(_walk(winning, "indexName"))),
            "docs_examined": next(_walk(out, "totalDocsExamined"), None),
            "keys_examined": next(_walk(out, "totalKeysExamined"), None),
            "execution_ms": next(_walk(out, "executionTimeMillis"), None)}


def explains(ctx, w1p, w2p, flat_only=False):
    e = {}
    p = w1p
    e["W1"] = {
        "postgres": explain_pg(f"SELECT {COLS_NORM} {JOINS} WHERE tw.town_name = %s AND ft.flat_type_name = %s "
                               "AND t.resale_price <= %s ORDER BY t.month DESC, t.txn_id DESC LIMIT 50",
                               [p["town"], p["flat_type"], p["budget"]]),
        "postgres_flat": explain_pg(f"SELECT {COLS_FLAT} FROM {FLAT} WHERE town_name = %s AND flat_type_name = %s "
                                    "AND resale_price <= %s ORDER BY month DESC, txn_id DESC LIMIT 50",
                                    [p["town"], p["flat_type"], p["budget"]]),
        "mongodb": explain_mongo({"find": TXN, "filter": {"town": p["town"], "flat_type": p["flat_type"],
                                                                     "price": {"$lte": p["budget"]}},
                                  "sort": {"month": -1, "_id": -1}, "limit": 50, "projection": PROJ}),
    }
    if flat_only:                  # the --no-index copies have no normalised twin
        e["W1"].pop("postgres")
        return e
    p = w2p
    e["W2"] = {
        "postgres": explain_pg(f"SELECT tw.town_name, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price) "
                               f"{JOINS} WHERE ft.flat_type_name = %s AND t.month >= %s GROUP BY tw.town_name",
                               [p["flat_type"], ctx["cutoff_24m"]]),
        "mongodb": explain_mongo({"aggregate": "transactions", "cursor": {}, "pipeline": [
            {"$match": {"flat_type": p["flat_type"], "month": {"$gte": ctx["cutoff_24m"]}}},
            {"$group": {"_id": "$town", "m": {"$median": {"input": "$price", "method": "approximate"}}}}]}),
    }
    if ctx["geo"]:
        lon, lat = ctx["block_points"][0]
        e["W4"] = {
            "postgres": explain_pg("SELECT station_id FROM mrt_stations ORDER BY geog <-> "
                                   "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography LIMIT 1", [lon, lat]),
            "mongodb": explain_mongo({"aggregate": "places", "cursor": {}, "pipeline": [
                {"$geoNear": {"near": {"type": "Point", "coordinates": [lon, lat]}, "distanceField": "d",
                              "spherical": True, "key": "loc"}}, {"$limit": 1}]}),
        }
    return e


UNINDEXED_TABLE = "bench_txn_noidx"
UNINDEXED_COLLECTION = "transactions_noidx"
RAW_DIR_NOINDEX = os.path.join(ROOT, "bench", "results", "raw_noindex")
OUT_NOINDEX = os.path.join(ROOT, "data", "results", "benchmark_noindex.json")


def prepare_unindexed():
    """Copies of the flat table and of the sales collection with no secondary
    index: only the primary key / _id that PostgreSQL and MongoDB always keep."""
    cur = pg()
    cur.execute(f"DROP TABLE IF EXISTS {UNINDEXED_TABLE}")
    cur.execute(f"CREATE TABLE {UNINDEXED_TABLE} AS SELECT * FROM bench_txn_flat")
    cur.execute(f"ALTER TABLE {UNINDEXED_TABLE} ADD PRIMARY KEY (txn_id)")
    cur.execute(f"ANALYZE {UNINDEXED_TABLE}")
    mdb = mongo()
    mdb[UNINDEXED_COLLECTION].drop()
    list(mdb.transactions.aggregate([{"$match": {}}, {"$out": UNINDEXED_COLLECTION}], allowDiskUse=True))
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s ORDER BY 1", [UNINDEXED_TABLE])
    return {"postgres_flat": [r[0] for r in cur.fetchall()],
            "mongodb": sorted(mdb[UNINDEXED_COLLECTION].index_information())}


def drop_unindexed():
    pg().execute(f"DROP TABLE IF EXISTS {UNINDEXED_TABLE}")
    mongo()[UNINDEXED_COLLECTION].drop()


def index_effect(timings_by_workload):
    """How many times slower each query is without indexes: median latency in
    this run divided by the median in the indexed run (benchmark.json)."""
    if not os.path.isfile(OUT):
        return None
    with open(OUT, encoding="utf-8") as fh:
        base = json.load(fh)
    indexed = {w["id"]: w["latency"] for w in base["workloads"]}
    out = {"baseline_file": os.path.relpath(OUT, ROOT), "baseline_generated_at": base.get("generated_at"),
           "slowdown_x": {}}
    for wid, lat in timings_by_workload.items():
        for c, s in lat.items():
            ref = indexed.get(wid, {}).get(c)
            if ref and ref["p50_ms"] > 0:
                out["slowdown_x"].setdefault(wid, {})[c] = round(s["p50_ms"] / ref["p50_ms"], 1)
    return out


def run_unindexed(args):
    """W0-W3 on the postgres_flat and mongodb configurations without secondary indexes."""
    global FLAT, TXN, RAW_DIR
    print("Preparing the unindexed copies (bench_txn_noidx, transactions_noidx) ...")
    ctx = context(skip_geo=True)               # parameters come from the indexed data; cheap
    copies = prepare_unindexed()
    FLAT, TXN, RAW_DIR = UNINDEXED_TABLE, UNINDEXED_COLLECTION, RAW_DIR_NOINDEX
    rng = random.Random(args.seed)
    stat_rng = np.random.default_rng(args.seed)
    results = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "label": args.label, "environment": environment(), "mode": "no secondary indexes",
               "indexes_left": copies,
               "protocol": {"runs": args.runs, "warmup": args.warmup, "seed": args.seed, "equivalence_sets": 20,
                            "latency": "client-observed, milliseconds", "order": "configurations rotate every run"},
               "workloads": []}
    timings, w1_first = {}, None
    try:
        for w in [W0(ctx), W1(ctx), W2(ctx), W3(ctx)]:
            w.configs = ("postgres_flat", "mongodb")      # the normalised schema has no unindexed twin
            params = w.params(rng, args.runs + args.warmup)
            print(f"{w.id} {w.title}: equivalence ...", end=" ", flush=True)
            eq = equivalence(w, params[:20])
            print(f"{eq['passed']}/{eq['checked']}; timing ...", flush=True)
            lat = measure(w, params, args.warmup, stat_rng)
            for c, s in lat.items():
                print(f"    {c:14s} p50 {s['p50_ms']:9.3f} ms  p95 {s['p95_ms']:9.3f}  p99 {s['p99_ms']:9.3f}")
            timings[w.id] = lat
            if w.id == "W1":
                w1_first = params[0]
            results["workloads"].append({"id": w.id, "title": w.title, "what": w.what, "configs": list(w.configs),
                                         "equivalence": eq, "latency": lat})
        results["explain"] = explains(ctx, w1_first, None, flat_only=True)
    finally:
        FLAT, TXN = "bench_txn_flat", "transactions"
        drop_unindexed()
    results["index_effect"] = index_effect(timings)
    if results["index_effect"]:
        print("Slow-down without indexes (x, median latency):")
        for wid, row in results["index_effect"]["slowdown_x"].items():
            print(f"    {wid}  " + "   ".join(f"{c} x{v}" for c, v in row.items()))
    else:
        print("(run the full benchmark first to get the slow-down factors)")
    os.makedirs(os.path.dirname(OUT_NOINDEX), exist_ok=True)
    with open(OUT_NOINDEX, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"Wrote {os.path.relpath(OUT_NOINDEX, ROOT)} (raw latencies in bench/results/raw_noindex/)")


def environment():
    cur = pg()
    cur.execute("SELECT version()")
    pg_version = cur.fetchone()[0]
    settings = {}
    for s in ("shared_buffers", "work_mem", "max_parallel_workers_per_gather", "jit", "effective_cache_size"):
        cur.execute("SHOW " + s)
        settings[s] = cur.fetchone()[0]
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
    r = cur.fetchone()
    info = mongo().client.server_info()
    cur.execute("SELECT COUNT(*) FROM resale_transactions")
    n_pg = cur.fetchone()[0]
    return {"postgres": pg_version.split(",")[0], "postgis": r[0] if r else None, "postgres_settings": settings,
            "mongodb": info.get("version"), "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}", "cpu_count": os.cpu_count(),
            "machine": platform.machine(), "rows_postgres": n_pg,
            "docs_mongodb": mongo().transactions.estimated_document_count()}


def index_parity():
    cur = pg()
    cur.execute("""SELECT tablename, indexname, indexdef FROM pg_indexes
                   WHERE tablename IN ('resale_transactions', 'bench_txn_flat', 'blocks', 'block_locations', 'mrt_stations')
                   ORDER BY tablename, indexname""")
    pgi = [{"table": t, "name": n, "definition": d} for t, n, d in cur.fetchall()]
    mi = []
    for coll in ("transactions", "places", "blocks_geo"):
        for name, spec in mongo()[coll].index_information().items():
            mi.append({"collection": coll, "name": name, "key": [[k, v] for k, v in spec["key"]]})
    return {"postgres": pgi, "mongodb": mi}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=None,
                    help="measured runs per workload and configuration (default 300; 100 with --no-index)")
    ap.add_argument("--warmup", type=int, default=None, help="discarded runs (default 30; 10 with --no-index)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--concurrency-seconds", type=float, default=10)
    ap.add_argument("--bulk-rows", type=int, default=200_000)
    ap.add_argument("--bulk-repeats", type=int, default=3)
    ap.add_argument("--skip-geo", action="store_true")
    ap.add_argument("--no-index", action="store_true",
                    help="control experiment: W0-W3 on copies with no secondary indexes (run the full benchmark first)")
    ap.add_argument("--quick", action="store_true", help="small numbers, for a smoke test")
    ap.add_argument("--label", default="", help="free text saved with the results (e.g. 'laptop, Docker')")
    args = ap.parse_args()
    if args.runs is None:
        args.runs = 100 if args.no_index else 300
    if args.warmup is None:
        args.warmup = 10 if args.no_index else 30
    if args.quick:
        args.runs, args.warmup, args.concurrency_seconds, args.bulk_rows, args.bulk_repeats = 40, 5, 2, 20_000, 1

    if mongo().transactions.estimated_document_count() == 0:
        sys.exit("MongoDB `transactions` is empty: run scripts/load_mongo_transactions.py first.")
    if args.no_index:
        return run_unindexed(args)
    print("Preparing (bench_txn_flat, parameters) ...")
    ctx = context(args.skip_geo)
    rng = random.Random(args.seed)
    stat_rng = np.random.default_rng(args.seed)
    workloads = [W0(ctx), W1(ctx), W2(ctx), W3(ctx)]
    if ctx["geo"]:
        workloads += [W4(ctx), W5(ctx)]
    else:
        print("  geospatial workloads W4/W5 skipped (no geo data yet)")

    results = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "label": args.label, "environment": environment(),
               "protocol": {"runs": args.runs, "warmup": args.warmup, "seed": args.seed,
                            "equivalence_sets": 20, "latency": "client-observed, milliseconds",
                            "ci": "bootstrap 95 % CI of the median (2000 resamples)",
                            "order": "configurations rotate every run"},
               "workloads": [], "concurrency": [], "geo": ctx["geo"]}
    w_params = {}
    for w in workloads:
        params = w.params(rng, args.runs + args.warmup)
        w_params[w.id] = params
        print(f"{w.id} {w.title}: equivalence ...", end=" ", flush=True)
        eq = equivalence(w, params[:20])
        print(f"{eq['passed']}/{eq['checked']}; timing ...", flush=True)
        timings = measure(w, params, args.warmup, stat_rng)
        for c, s in timings.items():
            print(f"    {c:14s} p50 {s['p50_ms']:8.3f} ms  p95 {s['p95_ms']:8.3f}  p99 {s['p99_ms']:8.3f}")
        results["workloads"].append({"id": w.id, "title": w.title, "what": w.what, "configs": list(w.configs),
                                     "equivalence": eq, "latency": timings})

    results["median_semantics"] = median_semantics(ctx)
    print(f"Median definitions: CONT != DISC in {results['median_semantics']['pct_groups']} % of groups")

    print("Concurrency ...")
    conc = [w for w in workloads if w.id in ("W1", "W4")]
    for w in conc:
        results["concurrency"] += concurrency(w, w_params[w.id], (1, 4, 8), args.concurrency_seconds)

    print("W6 bulk load ...")
    results["bulk_load"] = bulk_load(args.bulk_rows, args.bulk_repeats)
    print(f"    COPY {results['bulk_load']['postgres_copy']['rows_per_s_median']:,} rows/s   "
          f"insert_many {results['bulk_load']['mongodb_insert_many']['rows_per_s_median']:,} docs/s")

    print("Query plans and index parity ...")
    results["explain"] = explains(ctx, w_params["W1"][0], w_params["W2"][0])
    results["index_parity"] = index_parity()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"Wrote {os.path.relpath(OUT, ROOT)} (raw latencies in bench/results/raw/)")


if __name__ == "__main__":
    main()

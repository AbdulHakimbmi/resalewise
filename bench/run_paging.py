"""How much does page depth cost? OFFSET paging versus keyset paging.

    python bench/run_paging.py                 # ~1 minute, PostgreSQL (+ MongoDB if it is reachable)
    python bench/run_paging.py --repeats 30

The website pages through results with LIMIT/OFFSET (numbered pages 1..500 of
20 rows, so any page can be opened directly). OFFSET has a known cost: the
database must produce and throw away every skipped row, so page N costs about N
times page 1. Keyset ("seek") paging avoids that by asking for the rows *after*
the last one seen -- WHERE (month, txn_id) < (last_month, last_txn_id) -- which
the index answers directly. This script measures both on the application's real
search query so the report can state the trade-off with numbers instead of
folklore: it is why the API caps the window at 10,000 results.

For every depth it first checks that both methods return exactly the same 20
rows, then times each one (median and p95 of N runs, client-observed).

Writes data/results/paging.json.
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from werkzeug.datastructures import MultiDict  # noqa: E402

from resalewise import db  # noqa: E402
from resalewise.routes.search import SEARCH_COLUMNS, build_search_filters  # noqa: E402

OUT = os.path.join(ROOT, "data", "results", "paging.json")
PAGE = 20
DEPTHS = (0, 1_000, 10_000, 100_000, 500_000, 900_000)
FILTERS = {"all sales": {}, "TAMPINES, 4 ROOM": {"town": "TAMPINES", "flat_type": "4 ROOM"}}
ORDER = " ORDER BY t.month DESC, t.txn_id DESC "


def timed(fn, repeats):
    fn()                                                       # warm-up, discarded
    ms = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        ms.append((time.perf_counter() - t0) * 1000.0)
    ms.sort()
    return {"p50_ms": round(statistics.median(ms), 3), "p95_ms": round(ms[int(0.95 * (len(ms) - 1))], 3)}


# --- PostgreSQL -------------------------------------------------------------
def pg_cases(args_dict, depth, repeats):
    from_where, params, _ = build_search_filters(MultiDict(args_dict))
    total = db.q("SELECT COUNT(*) AS n " + from_where, params)[0]["n"]
    if depth >= total:
        return None
    offset_sql = SEARCH_COLUMNS + from_where + ORDER + "LIMIT %s OFFSET %s"
    keyset_sql = SEARCH_COLUMNS + from_where + " AND (t.month, t.txn_id) < (%s, %s) " + ORDER + "LIMIT %s"
    if depth:                                                   # the last row of the previous page = the cursor
        last = db.q(SEARCH_COLUMNS + from_where + ORDER + "LIMIT 1 OFFSET %s", params + [depth - 1])[0]
        cursor = (last["month"], last["txn_id"])
    by_offset = lambda: db.q(offset_sql, params + [PAGE, depth])                      # noqa: E731
    by_keyset = (lambda: db.q(keyset_sql, params + [*cursor, PAGE])) if depth else by_offset   # noqa: E731
    same = [r["txn_id"] for r in by_offset()] == [r["txn_id"] for r in by_keyset()]
    return {"total": total, "same_rows": same, "offset": timed(by_offset, repeats), "keyset": timed(by_keyset, repeats)}


# --- MongoDB ----------------------------------------------------------------
def mongo_cases(args_dict, depth, repeats):
    coll = db.mongo().transactions
    base = {}
    if args_dict.get("town"):
        base["town"] = args_dict["town"]
    if args_dict.get("flat_type"):
        base["flat_type"] = args_dict["flat_type"]
    total = coll.count_documents(base)
    if depth >= total:
        return None
    sort = [("month", -1), ("_id", -1)]
    by_skip = lambda: list(coll.find(base, {"_id": 1}).sort(sort).skip(depth).limit(PAGE))   # noqa: E731
    if depth:
        last = next(coll.find(base, {"month": 1}).sort(sort).skip(depth - 1).limit(1))
        seek = {"$and": [base, {"$or": [{"month": {"$lt": last["month"]}},
                                        {"month": last["month"], "_id": {"$lt": last["_id"]}}]}]} if base else \
               {"$or": [{"month": {"$lt": last["month"]}}, {"month": last["month"], "_id": {"$lt": last["_id"]}}]}
        by_keyset = lambda: list(coll.find(seek, {"_id": 1}).sort(sort).limit(PAGE))         # noqa: E731
    else:
        by_keyset = by_skip
    same = [d["_id"] for d in by_skip()] == [d["_id"] for d in by_keyset()]
    return {"total": total, "same_rows": same, "skip": timed(by_skip, repeats), "keyset": timed(by_keyset, repeats)}


def mongo_available():
    try:
        return db.mongo().transactions.estimated_document_count() > 0
    except Exception:                                           # noqa: BLE001 - any connection problem means "skip"
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repeats", type=int, default=15)
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    use_mongo = mongo_available()
    if not use_mongo:
        print("(MongoDB `transactions` is empty or unreachable: PostgreSQL only)")
    cases = []
    print(f"{'filter':18s} {'depth':>8s}  {'PG offset':>10s} {'PG keyset':>10s}   {'Mongo skip':>10s} {'Mongo keyset':>12s}   same")
    for name, flt in FILTERS.items():
        for depth in DEPTHS:
            pg = pg_cases(flt, depth, args.repeats)
            if pg is None:
                continue
            mg = mongo_cases(flt, depth, args.repeats) if use_mongo else None
            cases.append({"filter": name, "depth": depth, "page_size": PAGE, "postgres": pg, "mongodb": mg})
            print(f"{name:18s} {depth:8,d}  {pg['offset']['p50_ms']:10.2f} {pg['keyset']['p50_ms']:10.2f}   "
                  + (f"{mg['skip']['p50_ms']:10.2f} {mg['keyset']['p50_ms']:12.2f}" if mg else f"{'-':>10s} {'-':>12s}")
                  + f"   {pg['same_rows'] and (mg is None or mg['same_rows'])}")
    bad = [c for c in cases if not c["postgres"]["same_rows"] or (c["mongodb"] and not c["mongodb"]["same_rows"])]
    if bad:
        print("WARNING: offset and keyset returned different rows for", [(c["filter"], c["depth"]) for c in bad])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "label": args.label,
                   "repeats": args.repeats, "page_size": PAGE, "cases": cases,
                   "note": "median/p95 of client-observed milliseconds; the API exposes OFFSET paging capped at 10,000 results"},
                  fh, indent=2, default=str)
    print(f"Wrote {os.path.relpath(OUT, ROOT)}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

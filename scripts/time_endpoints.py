"""How much do the precomputed views save? Live query vs materialized view.

    python scripts/time_endpoints.py               # about a minute
    python scripts/time_endpoints.py --repeats 10

For each question the home and insights pages ask, this times the original
live query (an exact median over every matching sale) against the read from
the small precomputed views (sql/schema_extras.sql, 3b to 3d), checks that both
return the same answer, and prints the median milliseconds. The HTTP cache is
NOT involved: these are database times, what a visitor waits for when the
answer is not cached yet.

Run `python scripts/migrate.py` first (it creates the views).
Writes data/results/endpoint_timing.json.
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

from resalewise.routes import insights as ins  # noqa: E402

OUT = os.path.join(ROOT, "data", "results", "endpoint_timing.json")

CASES = [
    # (label, function, args)
    ("Home: price history, 4-room, 20 years",       ins.trend_rows,         ("4 ROOM", 240)),
    ("Home: town ranking, 4-room, 24 months",       ins.town_medians_rows,  ("4 ROOM", 24)),
    ("Insights: summary cards, 4-room, 24 months",  ins.summary_parts,      ("4 ROOM", 24)),
    ("Insights: trend, 4-room, 24 months",          ins.trend_rows,         ("4 ROOM", 24)),
    ("Insights: by flat type, all towns, 24 months", ins.by_flat_type_rows, (24,)),
    ("Insights: price bands, 4-room, 24 months",    ins.price_band_counts,  ("4 ROOM", 24)),
    ("Insights: summary, 3-room, Bedok, 10 years",  ins.summary_parts,      ("3 ROOM", 120, "BEDOK")),
    ("Insights: by flat type, all towns, 10 years", ins.by_flat_type_rows,  (120,)),
]


def timed(fn, repeats):
    fn()                                                    # warm-up, discarded
    ms = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        ms.append((time.perf_counter() - t0) * 1000.0)
    return round(statistics.median(ms), 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repeats", type=int, default=5)
    args = ap.parse_args()
    if not ins.precomputed_ok():
        sys.exit("The precomputed views are missing or stale. Run: python scripts/migrate.py --refresh")

    results, mismatched = [], []
    print(f"{'question':46s} {'live':>9s} {'view':>9s} {'faster':>8s}  same answer")
    for label, fn, a in CASES:
        same = fn(*a) == fn(*a, live=True)
        live_ms = timed(lambda: fn(*a, live=True), args.repeats)
        view_ms = timed(lambda: fn(*a), args.repeats)
        speedup = round(live_ms / view_ms, 1) if view_ms else None
        results.append({"question": label, "live_ms": live_ms, "view_ms": view_ms,
                        "speedup": speedup, "same_answer": same})
        if not same:
            mismatched.append(label)
        print(f"{label:46s} {live_ms:7.1f}ms {view_ms:7.1f}ms {speedup:7.1f}x  {same}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "repeats": args.repeats, "results": results,
                   "note": "median client-observed database milliseconds; HTTP cache not involved"},
                  fh, indent=2)
    print(f"Wrote {os.path.relpath(OUT, ROOT)}")
    if mismatched:
        print("WARNING: different answers for", mismatched)
        sys.exit(1)


if __name__ == "__main__":
    main()

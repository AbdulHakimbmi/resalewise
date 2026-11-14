"""Copy every resale transaction from PostgreSQL into MongoDB as one
denormalised document per sale (collection `transactions`).

    python scripts/load_mongo_transactions.py          # replace the collection
    python scripts/load_mongo_transactions.py --limit 50000   # quick test

Reading from PostgreSQL (not the CSV) guarantees both engines hold exactly
the same rows with the same ids, which the benchmark's equivalence check
relies on. Document shape:

    {_id: 123, month: ISODate("2026-09-01"), town: "TAMPINES",
     flat_type: "4 ROOM", flat_model: "MODEL A", block_id: 42, block_no: "123",
     street: "TAMPINES STREET 11", storey_low: 7, storey_high: 9,
     floor_area_sqm: 92.0, lease_commence: 1985, remaining_lease_months: 700,
     price: 650000.0}

Indexes are built after the bulk insert (faster), mirroring the PostgreSQL
indexes that serve the same queries (see resalewise/mongo_setup.py).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402
from pymongo.errors import BulkWriteError  # noqa: E402

from resalewise import config, db  # noqa: E402
from resalewise.mongo_setup import INDEXES, VALIDATORS  # noqa: E402

BATCH = 10_000

SQL = """
SELECT txn_id, month, town_name, flat_type_name, flat_model_name, block_id, block_no, street_name,
       storey_low, storey_high, floor_area_sqm::float8, lease_commence_date,
       remaining_lease_months, resale_price::float8
FROM v_transactions
ORDER BY txn_id
"""


def to_doc(r):
    (txn_id, month, town, ftype, fmodel, block_id, block_no, street,
     s_lo, s_hi, area, lease_start, rem, price) = r
    return {"_id": int(txn_id), "month": datetime(month.year, month.month, 1),
            "town": town, "flat_type": ftype, "flat_model": fmodel,
            "block_id": block_id, "block_no": block_no, "street": street,
            "storey_low": s_lo, "storey_high": s_hi, "floor_area_sqm": float(area),
            "lease_commence": lease_start, "remaining_lease_months": rem, "price": float(price)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="copy only the first N sales (testing)")
    args = ap.parse_args()

    mdb = db.mongo()
    mdb.transactions.drop()
    mdb.create_collection("transactions", validator=VALIDATORS["transactions"],
                          validationLevel="strict", validationAction="error")
    coll = mdb.transactions

    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    t0 = time.time()
    n = 0
    with conn, conn.cursor(name="txn_stream") as cur:            # server-side cursor: constant memory
        cur.itersize = BATCH
        cur.execute(SQL + (" LIMIT %d" % args.limit if args.limit else ""))
        batch = []
        for r in cur:
            batch.append(to_doc(r))
            if len(batch) >= BATCH:
                coll.insert_many(batch, ordered=False)
                n += len(batch)
                batch = []
                if n % 100_000 == 0:
                    print(f"  {n:,} documents", flush=True)
        if batch:
            try:
                coll.insert_many(batch, ordered=False)
            except BulkWriteError as e:                       # report, don't hide
                sys.exit(f"\nInsert failed: {e.details.get('writeErrors', [])[:3]}")
            n += len(batch)
    conn.close()
    load_s = time.time() - t0
    print(f"  {n:,} documents inserted in {load_s:.1f} s ({n / max(load_s, 1e-9):,.0f} docs/s)")

    t1 = time.time()
    for keys, opts in INDEXES["transactions"]:
        coll.create_index(keys, **opts)
    index_s = time.time() - t1
    print(f"  indexes built in {index_s:.1f} s")

    stats = mdb.command("collStats", "transactions")
    report = {"documents": n, "insert_seconds": round(load_s, 1), "index_seconds": round(index_s, 1),
              "docs_per_second": round(n / max(load_s, 1e-9)),
              "storage_mb": round(stats.get("storageSize", 0) / 1e6, 1),
              "index_mb": round(stats.get("totalIndexSize", 0) / 1e6, 1)}
    os.makedirs(os.path.join(config.ROOT, "data", "results"), exist_ok=True)
    with open(os.path.join(config.ROOT, "data", "results", "mongo_load_report.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    print("Done:", report)


if __name__ == "__main__":
    main()

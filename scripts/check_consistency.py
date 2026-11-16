"""Cross-database consistency check (PostgreSQL <-> MongoDB).

    python scripts/check_consistency.py

Two databases cannot share one transaction, so some facts exist in both and can
drift apart. This script looks for the drift the application can cause:

  The denormalised copy of the sales in MongoDB (`transactions`) versus the
     PostgreSQL table: row counts must match, or the SQL-vs-NoSQL benchmark is
     comparing different data. Reported only (reload with
     scripts/load_mongo_transactions.py).

Exit status 0 when the check passes, 1 otherwise (so it can run in a script).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resalewise import db  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.parse_args()
    ok = True

    n_pg = db.q("SELECT COUNT(*) AS n FROM resale_transactions")[0]["n"]
    n_mongo = db.mongo().transactions.estimated_document_count()
    print(f"Sales: PostgreSQL {n_pg:,} rows, MongoDB `transactions` {n_mongo:,} documents")
    if n_mongo == n_pg:
        print("    ok: the copy is complete")
    elif n_mongo == 0:
        print("    note: the MongoDB copy has not been loaded (python scripts/load_mongo_transactions.py)")
    else:
        ok = False
        print("    PROBLEM: counts differ; reload with python scripts/load_mongo_transactions.py")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

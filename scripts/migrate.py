"""Upgrade an existing database in place (no data is deleted).

    python scripts/migrate.py            # SQL extras + MongoDB validators/indexes
    python scripts/migrate.py --indexes  # also (re)apply sql/indexes.sql
    python scripts/migrate.py --refresh  # refresh the materialized views only

Use this on a database that was loaded earlier (e.g. the team's Neon / Atlas
databases). A fresh `python scripts/load.py --reset` already does all of it.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402

from resalewise import config  # noqa: E402

SQL_DIR = os.path.join(config.ROOT, "sql")
# Precomputed answers (sql/schema_extras.sql). Refresh them after reloading the data;
# until then the API notices they are stale and runs the live queries instead.
MATERIALIZED_VIEWS = ("mv_town_quarter_stats", "mv_quarter_stats", "mv_window_stats")


def run_file(cur, name):
    t0 = time.time()
    with open(os.path.join(SQL_DIR, name), encoding="utf-8") as fh:
        cur.execute(fh.read())
    print(f"  {name:22s} applied in {time.time() - t0:5.1f} s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--indexes", action="store_true", help="also apply sql/indexes.sql")
    ap.add_argument("--refresh", action="store_true", help="only refresh the materialized views")
    args = ap.parse_args()

    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    conn.autocommit = True
    with conn.cursor() as cur:
        if args.refresh:
            for view in MATERIALIZED_VIEWS:
                t0 = time.time()
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                print(f"Refreshed {view:22s} in {time.time() - t0:5.1f} s")
            return
        print("PostgreSQL:")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(80)")
        cur.execute("ALTER TABLE blocks ADD COLUMN IF NOT EXISTS in_property_info BOOLEAN NOT NULL DEFAULT TRUE")
        # The first loader inserted a demo user with an explicit id without
        # moving the sequence on, so the first real sign-up failed. Re-sync it.
        cur.execute("SELECT setval(pg_get_serial_sequence('users', 'user_id'), "
                    "COALESCE((SELECT MAX(user_id) FROM users), 0) + 1, false)")
        if args.indexes:
            run_file(cur, "indexes.sql")
        run_file(cur, "schema_extras.sql")
        for n in conn.notices:
            print("  notice:", n.strip())
    conn.close()

    print("Done.")


if __name__ == "__main__":
    main()

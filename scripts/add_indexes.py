"""Apply the performance indexes from sql/indexes.sql to the existing database.

    python scripts/add_indexes.py

Safe to re-run (every statement is CREATE INDEX IF NOT EXISTS). Building the
indexes on ~1M rows takes a little while on a free-tier database. Uses
DATABASE_URL from .env, like the API. (Equivalent: python scripts/migrate.py --indexes)
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402

from resalewise import config  # noqa: E402

PG = config.database_url()

SQL_FILE = os.path.join(config.ROOT, "sql", "indexes.sql")


def statements():
    text = open(SQL_FILE, encoding="utf-8").read()
    text = "\n".join(line.split("--")[0] for line in text.splitlines())   # drop comments
    return [s.strip() for s in text.split(";") if s.strip()]


if __name__ == "__main__":
    conn = psycopg2.connect(PG, connect_timeout=15)
    conn.autocommit = True
    with conn.cursor() as cur:
        for stmt in statements():
            label = re.sub(r"\s+", " ", stmt)[:78]
            t0 = time.time()
            cur.execute(stmt)
            print(f"{time.time() - t0:6.1f}s  {label}")
    conn.close()
    print("Done. Run the EXPLAIN ANALYZE demo in sql/indexes.sql to see the difference.")

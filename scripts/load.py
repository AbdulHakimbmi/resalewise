"""Build the PostgreSQL database from resale_clean.csv.

    python scripts/load.py --reset                     # uses ./resale_clean.csv (project folder)
    python scripts/load.py path/to/resale_clean.csv --reset

This DROPS and recreates the application tables, so it refuses to run
without --reset and asks you to confirm the target database (skip the prompt
with --yes, e.g. in scripts). Connection settings come from .env
(DATABASE_URL); nothing is hard-coded.

What it does
  1. Reads the CSV and normalises spellings HDB uses inconsistently
     ("MULTI-GENERATION" / "MULTI GENERATION", "IMPROVED-MAISONETTE" ...).
  2. Creates the normalised schema (towns, flat_types, flat_models, blocks,
     resale_transactions, users, bookmarks, user_profiles) and its indexes.
  3. Creates one row per block for EVERY sale. 192 blocks (16,016 sales,
     mostly demolished en-bloc estates) have no match in HDB's property
     information file; they are kept with empty block attributes instead of
     being silently dropped, which is what the first version of this loader did.
  4. Bulk-loads the rows with COPY (an order of magnitude faster than INSERTs).
  5. Applies sql/schema_extras.sql (CHECK constraints, triggers, views,
     materialized view), refreshes statistics and writes a load report to
     data/results/load_report.json.
"""
import argparse
import io
import json
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402

from resalewise import config  # noqa: E402

ROOT = config.ROOT
DEFAULT_CSV = os.path.join(ROOT, "resale_clean.csv")

SCHEMA_SQL = """
DROP MATERIALIZED VIEW IF EXISTS mv_window_stats, mv_quarter_stats, mv_town_quarter_stats CASCADE;
DROP VIEW IF EXISTS v_transactions CASCADE;
DROP TABLE IF EXISTS block_features, block_locations, driver_effects, bookmark_audit,
                     user_profiles, bookmarks, users, resale_transactions, blocks,
                     flat_models, flat_types, towns CASCADE;

CREATE TABLE towns (
    town_id   SERIAL PRIMARY KEY,
    town_name VARCHAR(64) UNIQUE NOT NULL,
    region    VARCHAR(32)
);

CREATE TABLE flat_types (
    flat_type_id   SERIAL PRIMARY KEY,
    flat_type_name VARCHAR(32) UNIQUE NOT NULL
);

CREATE TABLE flat_models (
    flat_model_id   SERIAL PRIMARY KEY,
    flat_model_name VARCHAR(64) UNIQUE NOT NULL
);

CREATE TABLE blocks (
    block_id             SERIAL PRIMARY KEY,
    block_no             VARCHAR(16) NOT NULL,
    street_name          VARCHAR(128) NOT NULL,
    town_id              INT NOT NULL REFERENCES towns(town_id),
    max_floor_lvl        INT,
    year_completed       INT,
    total_dwelling_units INT,
    has_commercial       BOOLEAN,
    has_market_hawker    BOOLEAN,
    has_carpark          BOOLEAN,
    has_pavilion         BOOLEAN,
    in_property_info     BOOLEAN NOT NULL DEFAULT TRUE,   -- FALSE: no match in HDB property information
    UNIQUE (block_no, street_name, town_id)
);

CREATE TABLE resale_transactions (
    txn_id                 BIGSERIAL PRIMARY KEY,
    month                  DATE NOT NULL,
    block_id               INT NOT NULL REFERENCES blocks(block_id),
    flat_type_id           INT NOT NULL REFERENCES flat_types(flat_type_id),
    flat_model_id          INT NOT NULL REFERENCES flat_models(flat_model_id),
    storey_low             INT,
    storey_high            INT,
    floor_area_sqm         NUMERIC(8,2),
    lease_commence_date    INT,
    remaining_lease_months INT,
    resale_price           NUMERIC(12,2) NOT NULL
);

-- ---------- User tables ----------
CREATE TABLE users (
    user_id       SERIAL PRIMARY KEY,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    name          VARCHAR(80),
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);

-- Many-to-many: a user saves many sales; a sale can be saved by many users.
CREATE TABLE bookmarks (
    user_id    INT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    txn_id     BIGINT NOT NULL REFERENCES resale_transactions(txn_id) ON DELETE CASCADE,
    notes      TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP,
    PRIMARY KEY (user_id, txn_id)
);

-- One-to-one with users (optional preferences).
CREATE TABLE user_profiles (
    user_id              INT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    budget_min           NUMERIC(12,2),
    budget_max           NUMERIC(12,2),
    preferred_towns      TEXT[],
    preferred_flat_types TEXT[]
);
"""

# Indexes are built AFTER the bulk load (much faster than maintaining them row
# by row). Every search / insight query filters on these columns or joins
# through these foreign keys; sql/indexes.sql has the EXPLAIN before/after demo.
INDEX_SQL = """
CREATE INDEX idx_txn_month      ON resale_transactions(month);
CREATE INDEX idx_txn_block      ON resale_transactions(block_id);                  -- FK -> blocks
CREATE INDEX idx_txn_flattype   ON resale_transactions(flat_type_id);              -- FK -> flat_types
CREATE INDEX idx_txn_flatmodel  ON resale_transactions(flat_model_id);             -- FK -> flat_models
CREATE INDEX idx_txn_price      ON resale_transactions(resale_price);              -- budget filter
CREATE INDEX idx_txn_type_month ON resale_transactions(flat_type_id, month);       -- insights: type + recent window
CREATE INDEX idx_txn_month_id   ON resale_transactions(month DESC, txn_id DESC);   -- "newest first" + LIMIT
CREATE INDEX idx_blocks_town    ON blocks(town_id);                                -- FK -> towns
CREATE INDEX idx_bookmarks_txn  ON bookmarks(txn_id);                              -- FK; speeds cascade deletes
"""

# Plausibility bounds, identical to the CHECK constraints in sql/schema_extras.sql.
# Rows outside them are reported and skipped rather than failing the load.
BOUNDS = {
    "resale_price":           (1, 9_999_999),
    "floor_area_sqm":         (20, 400),
    "storey_low":             (1, 60),
    "storey_high":            (1, 60),
    "lease_commence_date":    (1960, 2100),
    "remaining_lease_months": (0, 1260),
}

FLAG_COLS = {"commercial": "has_commercial", "market_hawker": "has_market_hawker",
             "multistorey_carpark": "has_carpark", "precinct_pavilion": "has_pavilion"}


def norm(s):
    return s.str.strip().str.replace("-", " ", regex=False).str.upper()


def describe_target(url):
    u = urlparse(url)
    return f"{u.hostname}:{u.port or 5432}/{(u.path or '/').lstrip('/')}"


def copy_df(cur, df, table, columns):
    buf = io.StringIO()
    df[columns].to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    cur.copy_expert(f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv, NULL '')", buf)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv", nargs="?", default=os.environ.get("RESALE_CSV", DEFAULT_CSV))
    ap.add_argument("--reset", action="store_true", help="required: drop and rebuild the tables")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        sys.exit(f"CSV not found: {args.csv}\nPass the path (python scripts/load.py path/to/resale_clean.csv --reset) "
                 "or place resale_clean.csv in the project folder (next to app.py).")
    url = config.database_url()
    target = describe_target(url)
    if not args.reset:
        sys.exit(f"This rebuilds every table in {target} (all users, bookmarks and data are deleted).\n"
                 "Run again with --reset if that is what you want.")
    if not args.yes:
        answer = input(f"Type the database name to confirm the reset of {target}: ").strip()
        if answer != target.rsplit("/", 1)[-1]:
            sys.exit("Not confirmed; nothing was changed.")

    t_all = time.time()
    report = {"csv": os.path.basename(args.csv), "target": target}

    print("Reading CSV ...")
    df = pd.read_csv(args.csv, dtype=str, keep_default_na=False)
    report["rows_read"] = len(df)
    print(f"  {len(df):,} rows, {len(df.columns)} columns")

    df["town"] = df["town"].str.strip().str.upper()
    df["flat_type"] = norm(df["flat_type"])
    df["flat_model"] = norm(df["flat_model"])
    df["block"] = df["block"].str.strip().str.upper()
    df["street_name"] = df["street_name"].str.strip().str.upper()
    df["month"] = pd.to_datetime(df["month"], errors="coerce")

    num = {c: pd.to_numeric(df[src], errors="coerce") for c, src in [
        ("resale_price", "resale_price"), ("floor_area_sqm", "floor_area_sqm"),
        ("storey_low", "storey_low"), ("storey_high", "storey_high"),
        ("lease_commence_date", "lease_commence_date"),
        ("remaining_lease_months", "remaining_lease_total_months")]}
    for c, s in num.items():
        df[c] = s

    # ---------- Quality checks (reported, never silent) ----------
    bad = df["month"].isna() | df["resale_price"].isna() | (df["town"] == "") | (df["block"] == "")
    reasons = {"missing month/price/town/block": int(bad.sum())}
    for c, (lo, hi) in BOUNDS.items():
        out = df[c].notna() & ((df[c] < lo) | (df[c] > hi))
        reasons[f"{c} outside {lo}-{hi}"] = int(out.sum())
        bad |= out
    inverted = df["storey_low"] > df["storey_high"]
    reasons["storey_low > storey_high"] = int(inverted.sum())
    bad |= inverted
    report["rows_rejected"] = {k: v for k, v in reasons.items() if v}
    report["rows_over_99y_lease"] = int((df["remaining_lease_months"] > 99 * 12).sum())
    df = df[~bad].copy()
    print(f"  {int(bad.sum()):,} rows rejected by plausibility checks")

    for c in ("storey_low", "storey_high", "lease_commence_date", "remaining_lease_months"):
        df[c] = df[c].round().astype("Int64")
    df["month"] = df["month"].dt.strftime("%Y-%m-%d")

    conn = psycopg2.connect(url, connect_timeout=15)
    try:
        with conn, conn.cursor() as cur:
            print(f"Creating schema in {target} ...")
            cur.execute(SCHEMA_SQL)

            # ---------- Reference tables ----------
            for table, col, values in (
                ("towns", "town_name", sorted(df["town"].unique())),
                ("flat_types", "flat_type_name", sorted(df["flat_type"].unique())),
                ("flat_models", "flat_model_name", sorted(df["flat_model"].unique())),
            ):
                cur.executemany(f"INSERT INTO {table} ({col}) VALUES (%s)", [(v,) for v in values])
            cur.execute("SELECT town_name, town_id FROM towns");           towns = dict(cur.fetchall())
            cur.execute("SELECT flat_type_name, flat_type_id FROM flat_types");   ftypes = dict(cur.fetchall())
            cur.execute("SELECT flat_model_name, flat_model_id FROM flat_models"); fmodels = dict(cur.fetchall())

            # ---------- Blocks: one per (block, street, town) seen in ANY sale ----------
            print("Building blocks ...")
            has_info = df["blk_no"].str.strip() != ""
            info = (df[has_info].drop_duplicates(["block", "street_name", "town"])
                    .set_index(["block", "street_name", "town"]))
            blocks = df[["block", "street_name", "town"]].drop_duplicates().reset_index(drop=True)
            blocks = blocks.join(info[["max_floor_lvl", "year_completed", "total_dwelling_units",
                                       *FLAG_COLS]], on=["block", "street_name", "town"])
            blocks["in_property_info"] = blocks["max_floor_lvl"].notna()
            for c in ("max_floor_lvl", "year_completed", "total_dwelling_units"):
                blocks[c] = pd.to_numeric(blocks[c], errors="coerce").round().astype("Int64")
            for src, dst in FLAG_COLS.items():
                blocks[dst] = blocks[src].map({"Y": "true", "N": "false"})
            blocks["town_id"] = blocks["town"].map(towns)
            blocks = blocks.rename(columns={"block": "block_no"})
            block_cols = ["block_no", "street_name", "town_id", "max_floor_lvl", "year_completed",
                          "total_dwelling_units", "has_commercial", "has_market_hawker",
                          "has_carpark", "has_pavilion", "in_property_info"]
            copy_df(cur, blocks, "blocks", block_cols)
            cur.execute("SELECT block_no, street_name, town_id, block_id FROM blocks")
            block_ids = {(a, b, c): d for a, b, c, d in cur.fetchall()}
            report["blocks"] = len(block_ids)
            report["blocks_without_property_info"] = int((~blocks["in_property_info"]).sum())
            report["sales_without_property_info"] = int((~has_info).sum())
            print(f"  {len(block_ids):,} blocks ({report['blocks_without_property_info']} without property info)")

            # ---------- Transactions (COPY) ----------
            print("Loading transactions with COPY ...")
            t0 = time.time()
            df["town_id"] = df["town"].map(towns)
            keys = list(zip(df["block"], df["street_name"], df["town_id"]))
            df["block_id"] = [block_ids[k] for k in keys]
            df["flat_type_id"] = df["flat_type"].map(ftypes)
            df["flat_model_id"] = df["flat_model"].map(fmodels)
            txn_cols = ["month", "block_id", "flat_type_id", "flat_model_id", "storey_low", "storey_high",
                        "floor_area_sqm", "lease_commence_date", "remaining_lease_months", "resale_price"]
            # Keep the CSV's order so txn_id is deterministic across reloads.
            copy_df(cur, df, "resale_transactions", txn_cols)
            report["copy_seconds"] = round(time.time() - t0, 1)
            report["rows_loaded"] = len(df)
            print(f"  {len(df):,} rows in {report['copy_seconds']} s")

            print("Building indexes ...")
            t0 = time.time()
            cur.execute(INDEX_SQL)
            report["index_seconds"] = round(time.time() - t0, 1)

        # Extras run in their own transaction so a failure there keeps the data.
        print("Applying sql/schema_extras.sql (constraints, triggers, views) ...")
        t0 = time.time()
        with conn, conn.cursor() as cur:
            with open(os.path.join(ROOT, "sql", "schema_extras.sql"), encoding="utf-8") as fh:
                cur.execute(fh.read())
        report["extras_seconds"] = round(time.time() - t0, 1)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ANALYZE")
            for t in ("towns", "flat_types", "flat_models", "blocks", "resale_transactions",
                      "users", "bookmarks", "mv_town_quarter_stats", "mv_quarter_stats", "mv_window_stats"):
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                n = cur.fetchone()[0]
                report.setdefault("table_counts", {})[t] = n
                print(f"  {t:24s} {n:>9,}")
    finally:
        conn.close()

    report["total_seconds"] = round(time.time() - t_all, 1)
    os.makedirs(os.path.join(ROOT, "data", "results"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "results", "load_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"DONE in {report['total_seconds']} s. Report: data/results/load_report.json")
    print("Next: python scripts/load_mongo_transactions.py   (MongoDB copy for the comparison)")


if __name__ == "__main__":
    main()

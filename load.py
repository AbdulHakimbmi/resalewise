import csv
import os
import sys
import pandas as pd
from sqlalchemy import create_engine, text

# CSV path resolution order:
#   1. First command-line argument:  python load.py path/to/resale_clean.csv
#   2. RESALE_CSV environment variable
#   3. Default: resale_clean.csv sitting next to this script
DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resale_clean.csv")
CSV = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("RESALE_CSV", DEFAULT_CSV))

if not os.path.isfile(CSV):
    sys.exit(
        f"CSV not found: {CSV}\n"
        "Pass the path as an argument (python load.py path/to/resale_clean.csv), "
        "set the RESALE_CSV env var, or place resale_clean.csv next to load.py."
    )

PG  = "postgresql://neondb_owner:npg_IVPBYS42cCKi@ep-round-waterfall-b3vp42ej-pooler.c-4.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

engine = create_engine(PG)

print("Reading CSV (robust csv reader)...")
with open(CSV, encoding='utf-8', errors='replace', newline='') as f:
    reader = csv.reader(f)
    header = next(reader)
    ncols = len(header)
    rows = []
    for r in reader:
        if len(r) < ncols: r = r + [''] * (ncols - len(r))
        elif len(r) > ncols: r = r[:ncols]
        rows.append(r)

df = pd.DataFrame(rows, columns=header)
print("rows:", len(df), "cols:", list(df.columns))
# ---------- Create schema ----------
with engine.begin() as conn:
    conn.execute(text("""
    DROP TABLE IF EXISTS user_profiles, bookmarks, users, resale_transactions, blocks, flat_models, flat_types, towns CASCADE;

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
        town_id              INT REFERENCES towns(town_id),
        max_floor_lvl        INT,
        year_completed       INT,
        total_dwelling_units INT,
        has_commercial       BOOLEAN,
        has_market_hawker    BOOLEAN,
        has_carpark          BOOLEAN,
        has_pavilion         BOOLEAN,
        UNIQUE (block_no, street_name, town_id)
    );

    CREATE TABLE resale_transactions (
        txn_id                BIGSERIAL PRIMARY KEY,
        month                 DATE NOT NULL,
        block_id              INT REFERENCES blocks(block_id),
        flat_type_id          INT REFERENCES flat_types(flat_type_id),
        flat_model_id         INT REFERENCES flat_models(flat_model_id),
        storey_low            INT,
        storey_high           INT,
        floor_area_sqm        NUMERIC(8,2),
        lease_commence_date   INT,
        remaining_lease_months INT,
        resale_price          NUMERIC(12,2)
    );

    CREATE INDEX idx_txn_month     ON resale_transactions(month);
    CREATE INDEX idx_txn_block     ON resale_transactions(block_id);
    CREATE INDEX idx_txn_flattype  ON resale_transactions(flat_type_id);
    CREATE INDEX idx_txn_price     ON resale_transactions(resale_price);

    -- ---------- User tables ----------
    CREATE TABLE users (
        user_id       SERIAL PRIMARY KEY,
        email         VARCHAR(255) UNIQUE NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        created_at    TIMESTAMP NOT NULL DEFAULT now()
    );

    CREATE TABLE bookmarks (
        user_id    INT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        txn_id     BIGINT NOT NULL REFERENCES resale_transactions(txn_id) ON DELETE CASCADE,
        notes      TEXT,
        created_at TIMESTAMP NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, txn_id)
    );

    CREATE TABLE user_profiles (
        user_id              INT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
        budget_min           NUMERIC(12,2),
        budget_max           NUMERIC(12,2),
        preferred_towns      TEXT[],
        preferred_flat_types TEXT[]
    );

    -- Seed a default user (login.html is a stub that hardcodes user_id = 1)
    INSERT INTO users (user_id, email, password_hash)
    VALUES (1, 'demo@resalewise.sg', 'stub-not-a-real-hash');
    """))

# ---------- Reference tables ----------
towns_df = pd.DataFrame({"town_name": sorted(df["town"].dropna().unique())})
towns_df.to_sql("towns", engine, if_exists="append", index=False)

ft_df = pd.DataFrame({"flat_type_name": sorted(df["flat_type"].dropna().unique())})
ft_df.to_sql("flat_types", engine, if_exists="append", index=False)

fm_df = pd.DataFrame({"flat_model_name": sorted(df["flat_model"].dropna().unique())})
fm_df.to_sql("flat_models", engine, if_exists="append", index=False)

# ---------- Build blocks (only rows WITH block metadata) ----------
print("Building blocks...")
blocks_source = df[
    (df["blk_no"].astype(str).str.strip() != "") &
    (df["max_floor_lvl"].astype(str).str.strip() != "") &
    (df["year_completed"].astype(str).str.strip() != "")
].copy()

blocks_df = (
    blocks_source[["blk_no","street_clean","town","max_floor_lvl","year_completed",
                   "total_dwelling_units","commercial","market_hawker",
                   "multistorey_carpark","precinct_pavilion"]]
    .drop_duplicates(subset=["blk_no","street_clean","town"])
    .rename(columns={
        "blk_no":"block_no",
        "street_clean":"street_name",
        "commercial":"has_commercial",
        "market_hawker":"has_market_hawker",
        "multistorey_carpark":"has_carpark",
        "precinct_pavilion":"has_pavilion",
    })
)

# Coerce numeric columns to numbers, non-numeric → NaN
for col in ["max_floor_lvl","year_completed","total_dwelling_units"]:
    blocks_df[col] = pd.to_numeric(blocks_df[col], errors="coerce")

# Coerce Y/N flags to booleans (NaN-safe)
for col in ["has_commercial","has_market_hawker","has_carpark","has_pavilion"]:
    blocks_df[col] = blocks_df[col].map({"Y": True, "N": False, True: True, False: False})

# Drop any block still missing a numeric (shouldn't happen, but safe)
blocks_df = blocks_df.dropna(subset=["max_floor_lvl","year_completed"])

# Map town name → town_id
town_lookup = pd.read_sql("SELECT town_id, town_name FROM towns", engine)
blocks_df = blocks_df.merge(town_lookup, left_on="town", right_on="town_name", how="left")
blocks_df = blocks_df[[
    "block_no","street_name","town_id","max_floor_lvl","year_completed",
    "total_dwelling_units","has_commercial","has_market_hawker",
    "has_carpark","has_pavilion"
]]

print("blocks to insert:", len(blocks_df))
blocks_df.to_sql("blocks", engine, if_exists="append", index=False, chunksize=5000)
print("blocks inserted:", len(blocks_df))

# ---------- Transactions (chunked) ----------
print("Loading transactions (this may take 1–2 minutes)...")
ft_lookup = pd.read_sql("SELECT flat_type_id, flat_type_name FROM flat_types", engine)
fm_lookup = pd.read_sql("SELECT flat_model_id, flat_model_name FROM flat_models", engine)
blocks_lookup = pd.read_sql("SELECT block_id, block_no, street_name, town_id FROM blocks", engine)
blocks_lookup = blocks_lookup.merge(town_lookup, on="town_id")

txn = df[[
    "month","blk_no","street_clean","town","flat_type","flat_model",
    "storey_low","storey_high","floor_area_sqm","lease_commence_date",
    "remaining_lease_total_months","resale_price"
]].rename(columns={"remaining_lease_total_months":"remaining_lease_months"})

txn = txn.merge(blocks_lookup, left_on=["blk_no","street_clean","town"],
                right_on=["block_no","street_name","town_name"], how="left")
txn = txn.merge(ft_lookup, left_on="flat_type", right_on="flat_type_name", how="left")
txn = txn.merge(fm_lookup, left_on="flat_model", right_on="flat_model_name", how="left")

txn["month"] = pd.to_datetime(txn["month"], errors="coerce").dt.date

txn_final = txn[[
    "month","block_id","flat_type_id","flat_model_id",
    "storey_low","storey_high","floor_area_sqm",
    "lease_commence_date","remaining_lease_months","resale_price"
]].copy()

# Coerce numeric columns
for col in ["storey_low","storey_high","floor_area_sqm",
            "lease_commence_date","remaining_lease_months","resale_price"]:
    txn_final[col] = pd.to_numeric(txn_final[col], errors="coerce")

# Convert empties to None so Postgres inserts NULL, not ''
txn_final = txn_final.where(pd.notnull(txn_final), None)

# Only drop rows missing the truly mandatory fields
txn_final = txn_final.dropna(subset=["flat_type_id","flat_model_id","resale_price"])

print("inserting:", len(txn_final), "transactions...")
txn_final.to_sql("resale_transactions", engine, if_exists="append",
                 index=False, chunksize=5000, method="multi")
# ---------- Verify ----------
with engine.connect() as conn:
    for t in ["towns","flat_types","flat_models","blocks","resale_transactions",
              "users","bookmarks","user_profiles"]:
        n = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        print(f"  {t}: {n}")
print("DONE.")
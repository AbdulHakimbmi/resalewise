-- ResaleWise SG — schema extras: integrity constraints, triggers, views.
--
-- Applied by `python scripts/migrate.py` (and automatically at the end of
-- `python scripts/load.py`). Every statement is idempotent, so re-running is safe.
-- Bounds below were checked against the full 985,219-row dataset
-- (see docs/data_profile.md); nothing in the real data violates them.

-- ---------------------------------------------------------------------------
-- 1. Integrity constraints (CHECK), added NOT VALID then validated, so the
--    table is not locked for a full rewrite and an existing database can be
--    upgraded in place.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  c record;
BEGIN
  FOR c IN SELECT * FROM (VALUES
    ('resale_transactions', 'chk_txn_price',      'resale_price > 0 AND resale_price < 10000000'),
    ('resale_transactions', 'chk_txn_area',       'floor_area_sqm BETWEEN 20 AND 400'),
    ('resale_transactions', 'chk_txn_storey',     'storey_low >= 1 AND storey_high <= 60 AND storey_low <= storey_high'),
    ('resale_transactions', 'chk_txn_lease_start','lease_commence_date BETWEEN 1960 AND 2100'),
    ('resale_transactions', 'chk_txn_remaining',  'remaining_lease_months BETWEEN 0 AND 1260'),
    -- HDB reports sales by month: the date must be the 1st of a month from 1990 on.
    ('resale_transactions', 'chk_txn_month',      'month >= DATE ''1990-01-01'' AND EXTRACT(DAY FROM month) = 1'),
    ('blocks',              'chk_block_floors',   'max_floor_lvl IS NULL OR max_floor_lvl BETWEEN 1 AND 60'),
    ('blocks',              'chk_block_year',     'year_completed IS NULL OR year_completed BETWEEN 1930 AND 2100'),
    ('blocks',              'chk_block_units',    'total_dwelling_units IS NULL OR total_dwelling_units >= 0'),
    -- Emails are stored lower-case so the UNIQUE constraint is case-insensitive in effect.
    ('users',               'chk_users_email',    'email = lower(email) AND email ~ ''^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'''),
    ('bookmarks',           'chk_bookmark_notes', 'notes IS NULL OR char_length(notes) <= 500'),
    ('user_profiles',       'chk_profile_budget', 'budget_min IS NULL OR budget_max IS NULL OR budget_min <= budget_max')
  ) AS v(tbl, name, expr)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = c.name) THEN
      EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I CHECK (%s) NOT VALID', c.tbl, c.name, c.expr);
    END IF;
    EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', c.tbl, c.name);
  END LOOP;
END $$;

-- Every sale belongs to a block, type and model once scripts/load.py has run. (A
-- database loaded by the old loader still has sales without a block; then
-- this step is skipped with a notice and scripts/load.py should be re-run.)
DO $$
DECLARE
  col text;
  has_null boolean;
BEGIN
  FOREACH col IN ARRAY ARRAY['block_id', 'flat_type_id', 'flat_model_id', 'resale_price'] LOOP
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM resale_transactions WHERE %I IS NULL)', col) INTO has_null;
    IF has_null THEN
      RAISE NOTICE 'resale_transactions.% has NULLs; NOT NULL skipped (re-run scripts/load.py)', col;
    ELSE
      EXECUTE format('ALTER TABLE resale_transactions ALTER COLUMN %I SET NOT NULL', col);
    END IF;
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Triggers on the shortlist (bookmarks)
-- ---------------------------------------------------------------------------
ALTER TABLE bookmarks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

-- 2a. BEFORE UPDATE: stamp updated_at whenever a note changes.
CREATE OR REPLACE FUNCTION fn_bookmarks_touch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.notes IS DISTINCT FROM OLD.notes THEN
    NEW.updated_at := now();
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_bookmarks_touch ON bookmarks;
CREATE TRIGGER trg_bookmarks_touch
  BEFORE UPDATE ON bookmarks
  FOR EACH ROW EXECUTE FUNCTION fn_bookmarks_touch();

-- 2b. AFTER INSERT/UPDATE/DELETE: an append-only audit trail of shortlist
--     changes (shown as "Recent activity" on the account page). No foreign
--     key on purpose: history must survive the row it describes.
CREATE TABLE IF NOT EXISTS bookmark_audit (
  audit_id   BIGSERIAL PRIMARY KEY,
  user_id    INT    NOT NULL,
  txn_id     BIGINT NOT NULL,
  action     VARCHAR(8) NOT NULL CHECK (action IN ('added', 'edited', 'removed')),
  notes      TEXT,
  changed_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bookmark_audit_user ON bookmark_audit(user_id, changed_at DESC);

CREATE OR REPLACE FUNCTION fn_bookmarks_audit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    INSERT INTO bookmark_audit(user_id, txn_id, action, notes) VALUES (NEW.user_id, NEW.txn_id, 'added', NEW.notes);
  ELSIF TG_OP = 'UPDATE' THEN
    IF NEW.notes IS DISTINCT FROM OLD.notes THEN
      INSERT INTO bookmark_audit(user_id, txn_id, action, notes) VALUES (NEW.user_id, NEW.txn_id, 'edited', NEW.notes);
    END IF;
  ELSE
    INSERT INTO bookmark_audit(user_id, txn_id, action, notes) VALUES (OLD.user_id, OLD.txn_id, 'removed', OLD.notes);
  END IF;
  RETURN NULL;   -- AFTER trigger: return value is ignored
END $$;

DROP TRIGGER IF EXISTS trg_bookmarks_audit ON bookmarks;
CREATE TRIGGER trg_bookmarks_audit
  AFTER INSERT OR UPDATE OR DELETE ON bookmarks
  FOR EACH ROW EXECUTE FUNCTION fn_bookmarks_audit();

-- ---------------------------------------------------------------------------
-- 3. Views
-- ---------------------------------------------------------------------------
-- 3a. One readable row per sale: the 5-way join, normalised names (HDB spells
--     some types/models two ways) and derived columns, in one place.
CREATE OR REPLACE VIEW v_transactions AS
SELECT t.txn_id,
       t.month,
       DATE_TRUNC('quarter', t.month)::date                  AS quarter,
       EXTRACT(YEAR FROM t.month)::int                       AS sale_year,
       tw.town_name,
       b.block_id, b.block_no, b.street_name,
       UPPER(REPLACE(ft.flat_type_name, '-', ' '))           AS flat_type_name,
       UPPER(REPLACE(fm.flat_model_name, '-', ' '))          AS flat_model_name,
       t.storey_low, t.storey_high,
       (t.storey_low + t.storey_high) / 2.0                  AS storey_mid,
       t.floor_area_sqm,
       t.lease_commence_date,
       t.remaining_lease_months,
       t.remaining_lease_months / 12.0                       AS remaining_lease_years,
       t.resale_price,
       t.resale_price / NULLIF(t.floor_area_sqm, 0)          AS price_per_sqm
FROM resale_transactions t
JOIN blocks b       ON b.block_id = t.block_id
JOIN towns tw       ON tw.town_id = b.town_id
JOIN flat_types ft  ON ft.flat_type_id = t.flat_type_id
JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id;

-- 3b. Quarterly medians per town and flat type, pre-computed. Window-function
--     queries (/api/insights/momentum) read ~5k rows instead of sorting 1M.
--     scripts/load.py refreshes it after every reload (REFRESH ... CONCURRENTLY needs
--     the unique index below).
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_town_quarter_stats AS
SELECT town_name,
       flat_type_name,
       quarter,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY resale_price))::int  AS median_price,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_per_sqm))::int AS median_psm,
       COUNT(*)::int                                                          AS txn_count
FROM v_transactions
GROUP BY town_name, flat_type_name, quarter
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_town_quarter
  ON mv_town_quarter_stats(flat_type_name, town_name, quarter);

-- 3c. Quarterly medians for ALL towns per flat type (the national price trend
--     on the home and insights pages). Same idea as 3b: a few hundred rows
--     instead of an exact median over up to ~400k sales on every request.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_quarter_stats AS
SELECT flat_type_name,
       quarter,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY resale_price))::int  AS median_price,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_per_sqm))::int AS median_psm,
       COUNT(*)::int                                                          AS txn_count
FROM v_transactions
GROUP BY flat_type_name, quarter
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_quarter
  ON mv_quarter_stats(flat_type_name, quarter);

-- 3d. Headline numbers for the time windows the website offers (last 12, 24,
--     36, 60 and 120 months of data), per flat type, for every town and for
--     all towns together (town_name = ''). Powers /api/insights/summary,
--     /town-medians, /by-flat-type and /price-bands.
--
--     The windows are anchored on the newest month in the data at refresh time
--     (anchor_month). The API compares it with MAX(month) on every uncached
--     request and falls back to the live query if they differ, so a reload
--     without a refresh gives slower answers, never stale ones.
--     The price brackets must match PRICE_BANDS in resalewise/routes/insights.py.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_window_stats AS
WITH latest AS (SELECT MAX(month) AS m FROM resale_transactions),
     windows(months) AS (VALUES (12), (24), (36), (60), (120))
SELECT w.months,
       l.m                                                            AS anchor_month,
       UPPER(REPLACE(ft.flat_type_name, '-', ' '))                    AS flat_type_name,
       COALESCE(tw.town_name, '')                                     AS town_name,
       MIN(ft.flat_type_id)                                           AS flat_type_id,
       COUNT(*)::int                                                  AS txn_count,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
         ORDER BY (t.resale_price / NULLIF(t.floor_area_sqm, 0))))::int        AS median_psm,
       ROUND(MIN(t.resale_price))::int                                AS min_price,
       ROUND(MAX(t.resale_price))::int                                AS max_price,
       ROUND(AVG(t.floor_area_sqm))::int                              AS avg_area,
       COUNT(*) FILTER (WHERE t.resale_price <  400000)::int                               AS band_0,
       COUNT(*) FILTER (WHERE t.resale_price >= 400000 AND t.resale_price < 500000)::int   AS band_1,
       COUNT(*) FILTER (WHERE t.resale_price >= 500000 AND t.resale_price < 650000)::int   AS band_2,
       COUNT(*) FILTER (WHERE t.resale_price >= 650000 AND t.resale_price < 800000)::int   AS band_3,
       COUNT(*) FILTER (WHERE t.resale_price >= 800000)::int                               AS band_4
FROM windows w
CROSS JOIN latest l
JOIN resale_transactions t ON t.month >= l.m - make_interval(months => w.months)
JOIN blocks b      ON b.block_id = t.block_id
JOIN towns tw      ON tw.town_id = b.town_id
JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
GROUP BY w.months, l.m, UPPER(REPLACE(ft.flat_type_name, '-', ' ')), GROUPING SETS ((tw.town_name), ())
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_window
  ON mv_window_stats(months, flat_type_name, town_name);

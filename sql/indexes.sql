-- ResaleWise SG — query-performance indexes, with a before/after demo.
--
-- Safe to run on the existing shared database (IF NOT EXISTS; nothing is
-- dropped or modified). Run it in psql, or just apply the DDL with:
--     python scripts/add_indexes.py
--
-- WHY: every search / insight query filters on a few columns and joins
-- resale_transactions -> blocks -> towns (and flat_types / flat_models).
-- Without an index on the join keys and filter columns Postgres reads all
-- ~1M transaction rows (a sequential scan) for each request.
--
-- DEMO — run this block FIRST (before the indexes) and note "Execution Time"
-- and the scan types, then create the indexes (section 2) and run it again:
--
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT t.txn_id, t.month, tw.town_name, ft.flat_type_name, t.resale_price
--   FROM resale_transactions t
--   JOIN blocks b      ON b.block_id = t.block_id
--   JOIN towns tw      ON tw.town_id = b.town_id
--   JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
--   WHERE tw.town_name = 'TAMPINES' AND ft.flat_type_name = '4 ROOM'
--   ORDER BY t.month DESC, t.txn_id DESC
--   LIMIT 30;                                -- = GET /api/search?town=TAMPINES&flat_type=4+ROOM
--
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price)
--   FROM resale_transactions t
--   JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
--   WHERE ft.flat_type_name = '4 ROOM'
--     AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - INTERVAL '24 months');
--                                            -- = the insights median query
--
-- Record the numbers (Seq Scan -> Index Scan, ms before -> ms after) in the report.

-- 2. The indexes ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_txn_month      ON resale_transactions(month);
CREATE INDEX IF NOT EXISTS idx_txn_block      ON resale_transactions(block_id);                  -- FK -> blocks
CREATE INDEX IF NOT EXISTS idx_txn_flattype   ON resale_transactions(flat_type_id);              -- FK -> flat_types
CREATE INDEX IF NOT EXISTS idx_txn_flatmodel  ON resale_transactions(flat_model_id);             -- FK -> flat_models
CREATE INDEX IF NOT EXISTS idx_txn_price      ON resale_transactions(resale_price);              -- budget filter
CREATE INDEX IF NOT EXISTS idx_txn_type_month ON resale_transactions(flat_type_id, month);       -- insights: type + recent window
CREATE INDEX IF NOT EXISTS idx_txn_month_id   ON resale_transactions(month DESC, txn_id DESC);   -- "newest first" + LIMIT
CREATE INDEX IF NOT EXISTS idx_blocks_town    ON blocks(town_id);                                -- FK -> towns (every join)
CREATE INDEX IF NOT EXISTS idx_bookmarks_txn  ON bookmarks(txn_id);                              -- FK; cascade deletes

-- Refresh planner statistics so the new indexes are used straight away.
ANALYZE resale_transactions;
ANALYZE blocks;

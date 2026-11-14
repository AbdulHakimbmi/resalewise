-- ResaleWise SG — nearby amenities (parks, hawker centres, gyms, childcare, schools).
--
-- Applied by scripts/load_amenities.py. Builds on sql/geo.sql (PostGIS, block_locations).
-- MongoDB holds the same amenities in the `amenities` collection; PostGIS holds
-- them here so the per-block distances can be computed by BOTH engines and
-- compared (scripts/build_amenity_features.py --compare).
--
--   amenity_kinds ──< amenities                      one kind, many places       (1 : N)
--   blocks ──< block_amenity_features >── amenity_kinds
--                                                    blocks and kinds, M : N, one row per pair
--   blocks ──1 block_location_premium                one row per block, filled by analysis/amenity_model.py
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS amenity_kinds (
  kind        VARCHAR(24) PRIMARY KEY CHECK (kind ~ '^[a-z][a-z_]*$'),
  label       VARCHAR(48) NOT NULL,
  plural      VARCHAR(48) NOT NULL,
  full_m      INT NOT NULL CHECK (full_m >= 0),     -- distance with the top score
  zero_m      INT NOT NULL,                         -- distance with a zero score
  sort_order  INT NOT NULL DEFAULT 0,
  CONSTRAINT chk_kind_scale CHECK (zero_m > full_m)
);

-- One row per place. `geog` is a Point, or a MultiPoint along a park's boundary
-- (distance to the park = distance to its nearest boundary point).
CREATE TABLE IF NOT EXISTS amenities (
  amenity_id  VARCHAR(96) PRIMARY KEY,
  kind        VARCHAR(24) NOT NULL REFERENCES amenity_kinds(kind),
  name        VARCHAR(160) NOT NULL,
  n_points    INT NOT NULL CHECK (n_points >= 1),
  geog        geography NOT NULL,
  centroid    geography(Point, 4326) NOT NULL,
  source      VARCHAR(48) NOT NULL,
  loaded_at   TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT chk_amenity_in_sg CHECK (
    ST_Y(centroid::geometry) BETWEEN 1.15 AND 1.48 AND ST_X(centroid::geometry) BETWEEN 103.6 AND 104.1)
);
CREATE INDEX IF NOT EXISTS gix_amenities ON amenities USING gist (geog);
CREATE INDEX IF NOT EXISTS idx_amenities_kind ON amenities(kind);

-- Per block and kind: how far is the nearest one, and how many are within 500 m, 1 km, 2 km.
CREATE TABLE IF NOT EXISTS block_amenity_features (
  block_id      INT NOT NULL REFERENCES blocks(block_id) ON DELETE CASCADE,
  kind          VARCHAR(24) NOT NULL REFERENCES amenity_kinds(kind),
  nearest_m     NUMERIC(9, 1) CHECK (nearest_m >= 0),
  nearest_name  VARCHAR(160),
  within_500m   INT NOT NULL DEFAULT 0 CHECK (within_500m >= 0),
  within_1km    INT NOT NULL DEFAULT 0,
  within_2km    INT NOT NULL DEFAULT 0,
  computed_by   VARCHAR(32) NOT NULL,
  computed_at   TIMESTAMP NOT NULL DEFAULT now(),
  PRIMARY KEY (block_id, kind),
  -- A wider circle can never hold fewer places than a narrower one.
  CONSTRAINT chk_counts_nested CHECK (within_500m <= within_1km AND within_1km <= within_2km),
  -- If anything is within 2 km, the nearest one is within 2 km (small slack for rounding).
  CONSTRAINT chk_count_vs_nearest CHECK (within_2km = 0 OR nearest_m IS NULL OR nearest_m <= 2000.5)
);
CREATE INDEX IF NOT EXISTS idx_baf_kind_dist ON block_amenity_features(kind, nearest_m);

-- What the price model says about a block's surroundings (analysis/amenity_model.py).
-- An association in past sales, not a cause: see the method notes in that script.
CREATE TABLE IF NOT EXISTS block_location_premium (
  block_id     INT PRIMARY KEY REFERENCES blocks(block_id) ON DELETE CASCADE,
  premium_pct  NUMERIC(6, 2) NOT NULL,
  n_sales      INT NOT NULL CHECK (n_sales > 0),
  model_run    VARCHAR(32) NOT NULL,
  computed_at  TIMESTAMP NOT NULL DEFAULT now()
);

-- ResaleWise SG — geospatial tables (PostgreSQL + PostGIS side).
--
-- Applied by the geospatial scripts (scripts/geocode_blocks.py and friends).
-- MongoDB holds the same points as GeoJSON (collections `places` and
-- `blocks_geo`); PostGIS holds them here so every distance can be computed by
-- BOTH engines and cross-checked (scripts/validate_geo.py, check V4).
CREATE EXTENSION IF NOT EXISTS postgis;

-- Where each HDB block is, from OneMap geocoding (one row per block).
CREATE TABLE IF NOT EXISTS block_locations (
  block_id        INT PRIMARY KEY REFERENCES blocks(block_id) ON DELETE CASCADE,
  lat             DOUBLE PRECISION,
  lon             DOUBLE PRECISION,
  postal          VARCHAR(6),
  matched_address TEXT,
  match_quality   VARCHAR(10) NOT NULL CHECK (match_quality IN ('exact', 'fuzzy', 'unmatched')),
  source          VARCHAR(16) NOT NULL DEFAULT 'onemap',
  geocoded_at     TIMESTAMP NOT NULL DEFAULT now(),
  -- A geocode is either complete (both coordinates, valid ranges) or absent.
  CONSTRAINT chk_loc_pair  CHECK ((lat IS NULL) = (lon IS NULL)),
  CONSTRAINT chk_loc_range CHECK (lat IS NULL OR (lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180)),
  CONSTRAINT chk_loc_match CHECK ((match_quality = 'unmatched') = (lat IS NULL)),
  -- Derived point, always in sync with lat/lon (PostGIS uses lon, lat order).
  geog geography(Point, 4326) GENERATED ALWAYS AS (
    CASE WHEN lat IS NULL THEN NULL
         ELSE ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography END) STORED,
  -- Plausibility flag used by validation check V2 (Singapore's bounding box).
  in_sg_bbox boolean GENERATED ALWAYS AS (
    lat BETWEEN 1.15 AND 1.48 AND lon BETWEEN 103.6 AND 104.1) STORED
);
CREATE INDEX IF NOT EXISTS gix_block_locations ON block_locations USING gist (geog);

-- MRT / LRT stations. One row per station; `geog` is a MultiPoint of its exits
-- (or entrances), so "distance to the station" means "to its nearest exit".
CREATE TABLE IF NOT EXISTS mrt_stations (
  station_id  VARCHAR(64) PRIMARY KEY,
  name        VARCHAR(128) NOT NULL,
  kind        VARCHAR(8)   NOT NULL CHECK (kind IN ('mrt', 'lrt')),
  codes       TEXT[]       NOT NULL DEFAULT '{}',
  opened_year INT CHECK (opened_year IS NULL OR opened_year BETWEEN 1987 AND 2100),
  n_points    INT          NOT NULL CHECK (n_points >= 1),
  geog        geography    NOT NULL,
  centroid    geography(Point, 4326) NOT NULL,
  source      VARCHAR(32)  NOT NULL
);
CREATE INDEX IF NOT EXISTS gix_mrt_stations ON mrt_stations USING gist (geog);

-- Derived per-block features, computed by MongoDB's $geoNear
-- (scripts/build_block_features.py) and stored here so the relational side
-- can join them to transactions for the price-driver analysis.
CREATE TABLE IF NOT EXISTS block_features (
  block_id             INT PRIMARY KEY REFERENCES blocks(block_id) ON DELETE CASCADE,
  nearest_station_id   VARCHAR(64) REFERENCES mrt_stations(station_id) ON DELETE SET NULL,
  nearest_station_name VARCHAR(128),
  nearest_mrt_m        NUMERIC(9, 1) CHECK (nearest_mrt_m >= 0),
  stations_within_1km  INT NOT NULL DEFAULT 0 CHECK (stations_within_1km >= 0),
  mrt_band             VARCHAR(10) CHECK (mrt_band IN ('<=500m', '500m-1km', '>1km')),
  computed_by          VARCHAR(32) NOT NULL DEFAULT 'mongodb $geoNear',
  computed_at          TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_block_features_band ON block_features(mrt_band);

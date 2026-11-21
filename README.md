# ResaleWise SG

ResaleWise SG helps people make sense of Singapore's HDB resale market. You can
search 985,219 real resale transactions (January 1990 to September 2026), see how
a flat is priced against similar flats sold in the same period, find out how far
it is from the MRT, read what residents say about a town, and shortlist the
flats worth a second look.

It is an INF2003 Database Systems project (Team 67) built on **two databases**:
PostgreSQL for the relational core and MongoDB for documents and geospatial
search, with a rigorous side-by-side comparison of the two.

> **Running it:** follow the **Quick start** in [USER_MANUAL.md](USER_MANUAL.md).
> It takes about 20 minutes from a fresh clone. The slow geocoding step (~50
> minutes) is already done and saved in `data/geo/`, so you only load it.

---

## What you can do

- **Find a flat.** Search by town, flat type, budget, remaining lease, floor area,
  storey range and flat model. Every result is judged against the median for the
  same town and flat type **in the same quarter**, so a 1995 sale isn't called a
  bargain just because prices have risen since.
- **See one sale in context.** Each transaction page shows the block on a map
  with its nearest MRT/LRT stations, and a **"What goes with a higher price?"** panel:
  how the flat compares with similar flats sold the same year, and which features
  go with higher prices across the market (with 95% confidence intervals). These
  are associations, not causes, and the page says so.
- **Understand the market.** Median prices and trends, towns side by side, and a
  league table of which towns are rising fastest (window functions).
- **Explore a town.** Median prices, recent sales, a map of every block coloured
  by price per sqm, and residents' reviews.
- **Build a shortlist.** Save flats, add notes, and compare them; the account page
  shows your recent shortlist activity and lets you delete the account.
- **Read and write reviews.** Search them, see the rating spread and most-used
  tags, and edit or delete your own.
- **Administer it.** Signed in as `admin`, `admin.html` queries both databases read-only, turns the sales into an
  OLAP cube, shows an index and concurrent transactions (deadlock, lost update, savepoint) at work, and shows each tool's commands to copy into a terminal.
  See USER_MANUAL.md, section 4A.
- **Look under the hood.** `benchmark.html` shows how the work is split between
  the two databases, runs the same search on both live, and shows the benchmark
  and geospatial validation results.

## How the two databases share the work

| PostgreSQL (relational) | MongoDB (documents) |
|---|---|
| `towns`, `flat_types`, `flat_models`, `blocks`, `resale_transactions` (normalised, 985k rows) | `reviews`: free text + tags, `$jsonSchema` validator, text index, `$facet` |
| `users` ↔ `resale_transactions` many-to-many via `bookmarks`; `user_profiles` (1:1) | `places`: MRT/LRT stations as GeoJSON MultiPoint (one point per exit), 2dsphere |
| CHECK constraints, NOT NULL, foreign keys with cascades | `blocks_geo`: every geocoded block as a GeoJSON Point, 2dsphere |
| Triggers: `updated_at` stamp + an audit trail of shortlist changes | `$geoNear` computes each block's nearest station → written back to PostgreSQL |
| Views `v_transactions`; materialized views `mv_town_quarter_stats`, `mv_quarter_stats`, `mv_window_stats` (precomputed medians); window functions, `GROUPING SETS` | `transactions`: a denormalised copy of the sales for the benchmark |
| PostGIS: `block_locations`, `mrt_stations`, `block_features` (cross-check) | |

Pipeline: HDB CSV → PostgreSQL (`scripts/load.py`) → OneMap geocoding → MongoDB
`blocks_geo` + `places` → `$geoNear` → PostgreSQL `block_features` → price-driver
analysis. Every distance is recomputed in PostGIS and compared (checks V1–V7).

## Project layout

```
app.py                     entry point (python app.py / gunicorn app:app)
resalewise/                the Flask application
  admin.py, concurrency.py     the admin area: console guards, the cube's SQL, the concurrency runs
  config.py                settings from .env (no secrets in code)
  db.py                    PostgreSQL pool + lazy MongoDB client
  web.py, auth.py          error contract, rate limiting, cache, tokens
  benchmarks.py            "value" benchmarks: each sale vs its own period
  geo.py                   geocode matching + distance helpers
  mongo_setup.py           MongoDB validators and indexes
  routes/                  one blueprint per area (search, insights, geo, reviews, ...)
sql/                       schema_extras.sql (constraints, triggers, views), geo.sql, indexes.sql, admin.sql
scripts/                   load.py (PostgreSQL from resale_clean.csv), load_mongo_transactions.py, geocoding,
                           stations, features, validation, check_consistency.py, seed_reviews.py, photos
data/reference/            MRT/LRT station opening years (hand-checked) used by the price-driver analysis
analysis/price_drivers.py  matched comparisons with bootstrap confidence intervals
bench/                     run_bench.py (SQL vs NoSQL, plus --no-index) and run_paging.py (see bench/README.md)
tests/                     unit tests (offline) + integration tests (real databases)
data/results/              outputs used by the site and the report
docker-compose.yml         local PostgreSQL + PostGIS and MongoDB
*.html, assets/            the website (design by Abdul Hakim: assets/css/home.css + site.css;
                           our feature layer: assets/css/features.css + assets/js/features.js)
```

## Tests

```bash
python -m pytest tests                       # the unit tests: no database or network needed
RW_INTEGRATION=1 python -m pytest tests/test_integration.py   # the 12 integration tests, on their own:
                                                         # the unit tests set a fake database address
```

## Data and credits

- Resale prices: [HDB Resale Flat Prices](https://data.gov.sg/dataset/resale-flat-prices), data.gov.sg
  (Singapore Open Data Licence), merged with HDB Property Information (block attributes).
- Geocoding and stations: [OneMap](https://www.onemap.gov.sg/) (Singapore Land Authority);
  optional LTA MRT Station Exit file from data.gov.sg.
- Map tiles: OneMap basemaps © Singapore Land Authority (OpenStreetMap tiles as a fallback). Map library: Leaflet (BSD-2, bundled). The map provider is set in `addTiles()` in `assets/js/features.js`.
- Town and homepage photos: Wikimedia Commons and Flickr (Creative Commons), credited per photo in `assets/images/towns/credits.json` and `assets/images/home/credits.json`; the credits are listed on the About page. A town without a photo shows a drawn block instead.

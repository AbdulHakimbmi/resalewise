# ResaleWise SG — User Manual

This manual takes you from a fresh copy of the project to the running website,
then through the geospatial pipeline, the analysis and the benchmark. Commands
are shown for **Windows (PowerShell)**; macOS/Linux differences are noted.

**Teammates: start with the Quick start below.** The slow part of the geospatial
work (geocoding ~10,000 blocks, about 50 minutes) is **already done and saved in
Git** (`data/geo/geocode_cache.jsonl`), so nobody has to repeat it.

Contents
- Quick start (teammates)
1. What you need
2. One-time setup
3. Start the databases and load the data
4. Run the website
5. The geospatial pipeline (map, distances, validation)
6. Price-driver analysis
7. SQL vs NoSQL benchmark
8. Tests
9. Using the cloud databases (Neon + MongoDB Atlas) instead
10. Troubleshooting
11. What each output is used for

---

## Quick start (teammates)

About 20 minutes, most of it downloads and waiting for the databases. Do the
steps in order; the number in brackets points to the section with more detail.
Type each command in the **VS Code terminal (PowerShell)**, in the project folder.

**A. Get the project**

1. Already have the folder? `git checkout main`, then `git pull`.
   New to it? `git clone https://github.com/AbdulHakimbmi/resalewise-sg.git`, then `cd resalewise-sg`.
2. Make sure Python 3.11–3.13, Git and Docker Desktop are installed, and that
   Docker Desktop is **open and running** [1].
3. Get `resale_clean.csv` from a teammate (it is too big for Git) and put it in
   the project folder next to `app.py`.

**B. Set up Python and your settings**

4. `python -m venv .venv`
5. `.\.venv\Scripts\Activate.ps1` (if it refuses: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then try again)
6. `python -m pip install -r requirements-dev.txt`
7. `copy .env.example .env`
8. `python -c "import secrets; print(secrets.token_urlsafe(32))"`, then open `.env`
   and paste the value after `TOKEN_SECRET=`. Leave the database lines alone [2].

**C. Start the databases and load the data**

9. `docker compose up -d`, then `docker compose ps`. Wait until both say **healthy** [3].
10. `python scripts/load.py --reset --yes` (about 1 minute)
11. `python scripts/load_mongo_transactions.py` (about 1 minute)

**D. Load the map data (about 2 minutes, no geocoding wait)**

13. `python scripts/geocode_blocks.py --apply-only` (puts the saved block locations into both databases; no internet needed)
14. `python scripts/load_stations.py --onemap` (downloads the MRT/LRT station list; needs internet)
15. `python scripts/build_block_features.py` (nearest station for every block)
16. `python analysis/price_drivers.py` (a few seconds: fills the table behind the "What drives this price?" panel) [6]
17. `git restore data/results` (the loaders above rewrite the team's saved result files in `data/results/`;
    this puts them back so you never commit them by accident. It is safe to run any time.)

**E. Run it**

18. `python app.py`, then open **http://localhost:5000** [4].
19. Check it worked: http://localhost:5000/api/health should show `"status": "ok"`, and a
    transaction page (open any result from *Find a flat*) should show a map with the nearest stations.

**Please don't:**
- run `scripts/geocode_blocks.py` **without** `--apply-only` (it would call OneMap for blocks again);
- commit `.env` or `resale_clean.csv` (both are git-ignored; check `git status` before every push);
- re-run `analysis/price_drivers.py`, `scripts/validate_geo.py` or the benchmarks and then
  commit the files in `data/results/`. Those are the team's official results for the report;
  overwriting them from your machine would replace numbers the report quotes.
- push straight to `main`: make your own branch (`git checkout -b my-feature`), push it, and open a Pull request.

---

## 1. What you need

| Tool | Version | Check with |
|---|---|---|
| Python | 3.11, 3.12 or 3.13 | `python --version` |
| Docker Desktop | any recent | `docker --version` (Docker Desktop must be running) |
| Git | any | `git --version` |
| The data file `resale_clean.csv` | 985,219 rows, ~206 MB | in the submission zip; for teammates, ask the team for the shared download link (it is too big for Git) |

About 3 GB of free disk space and 4 GB of free memory for the two database containers.

## 2. One-time setup

```powershell
cd path\to\resalewise-sg
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

If PowerShell refuses to run the activate script, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.

Create your settings file:

```powershell
copy .env.example .env                 # macOS/Linux: cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Open `.env` and paste the printed value after `TOKEN_SECRET=`. The database
lines already point at the local Docker databases; leave them as they are.
`.env` is git-ignored: never commit it.

Put `resale_clean.csv` in the project folder (next to `app.py`).

## 3. Start the databases and load the data

```powershell
docker compose up -d                   # first run downloads the images (~1 GB)
docker compose ps                      # wait until both say "healthy"
```

This starts PostgreSQL 17 with PostGIS on port **5433** and MongoDB 8 on port
**27018** (not the defaults, so they don't clash with anything already installed).

```powershell
python scripts/load.py --reset --yes              # ~1 minute: tables, 985,219 sales, constraints, views
python scripts/load_mongo_transactions.py      # ~1 minute: the MongoDB copy for the comparison
```

Then load the map data. **You do not have to geocode:** the located blocks are
already saved in `data/geo/geocode_cache.jsonl`, which came with the project.

```powershell
python scripts/geocode_blocks.py --apply-only     # ~1 minute, no internet: block locations into both databases
python scripts/load_stations.py --onemap          # ~1 minute, needs internet: MRT/LRT stations
python scripts/build_block_features.py            # ~1 minute: nearest station for every block
python analysis/price_drivers.py                  # a few seconds: the table behind "What drives this price?"
git restore data/results                          # put the team's saved result files back (see Quick start)
```

Skip these steps and the site still works, but the maps and the "What drives
this price?" panels stay empty (section 5 explains each step).

`scripts/load.py` refuses to run without `--reset` because it rebuilds every table
(all accounts and shortlists are deleted). Without `--yes` it asks you to type
the database name. It writes a quality report to `data/results/load_report.json`.


## 4. Run the website

```powershell
python app.py
```

Open **http://localhost:5000**. The Flask app serves both the pages and the
API, so nothing else is needed. Check it with http://localhost:5000/api/health
(should show `"status": "ok"`). Stop it with Ctrl+C.

For development with auto-reload, set `FLASK_DEBUG=1` in `.env` first.

Pages: Home, Find a flat (`search.html`), Towns,
Compare (your shortlist, needs an account), Account, About & methodology, and
**Under the hood** (`benchmark.html`, linked in the footer).

### The loading intro

The first visit to the home page in a browser session plays a short cinematic intro
(`assets/intro/index.html`): full-screen photographs of Singapore's estates, one after another, each
with a headline about what the site does (the sales data, prices in context, the distance to the MRT,
what is nearby, the shortlist), ending on the ResaleWise name with a photograph showing through the
letters, then round again. "Loading Database" and a counter are on screen throughout. It is real: it
makes the same API requests the pages make on first use (see `intro/src/loader.js`), keeps going until
they have all finished, and then takes you to the home page at once. It always plays for at least 5 s
(`MIN_SHOW` in `intro/src/App.jsx`; set it to 0 for an instant jump on a fast server). The counter shows
how many requests have succeeded.

* It plays **once per session**, and is skipped with `?nointro` on the URL, with
  reduced-motion or data-saver on, or when browser storage is blocked.
  "Skip intro" is on screen the whole time. If the API is down it says so and lets you in.
* The photographs are the site's own (`assets/images/towns` and `home`), and each scene shows the credit
  its licence asks for, read from the `credits.json` next to the photos. To change a scene, edit
  `intro/src/scenes.js` (photo, headline, one line of small print: only say what the site really does).
* **The source is `intro/`; the built page in `assets/intro/` is generated.** To change it:
  `cd intro`, `npm install`, `npm run dev` (talks to the Flask app on :5000) or `npm run build`
  (rewrites `assets/intro/`; commit the result, the server needs no Node).
* `?t=5` freezes the intro 5 s in (for screenshots; one scene is 3.4 s, and `window.__goto(9)` in the console
  moves a frozen frame). `?hold=1` stops the redirect, so you can watch the whole sequence on a fast server.
* The bundle is about 255 KB (77 KB gzipped) plus the photographs, which load one by one as the scenes play.

## 5. The geospatial pipeline

The pipeline puts every HDB block on the map, finds the nearest MRT/LRT station
(MongoDB `$geoNear`), cross-checks every distance in PostGIS, and feeds the
price-driver analysis. **For most people only 5.1 to 5.3 are needed, and the
slow part is already done.**

| Step | Needs internet? | Time | Who runs it |
|---|---|---|---|
| 5.1 Block locations | No (`--apply-only` reads the saved cache) | ~1 min | everyone |
| 5.2 Stations | Yes (one OneMap request) | ~1 min | everyone |
| 5.3 Nearest station per block | No | ~1 min | everyone |
| 5.4 Validate (V1–V6) | No | ~1 min | only if you change the pipeline |
| 5.5 Geocode from scratch | Yes (~10,000 OneMap calls) | ~50 min | almost nobody |

### 5.1 Put the saved block locations into the databases

```powershell
python scripts/geocode_blocks.py --apply-only
```

`data/geo/geocode_cache.jsonl` holds the OneMap answer for all 10,010 blocks. This
command reads it (no network) and fills PostgreSQL `block_locations` and
MongoDB `blocks_geo`. It prints how many blocks were located (`exact`, `fuzzy`,
`unmatched`).

### 5.2 Load the MRT/LRT stations

Easiest (everyone): `python scripts/load_stations.py --onemap` asks OneMap for every
station, with no download and no key. The team's official results were produced
from the station list available when they were run; a later `--onemap` run can
return a slightly different list, so small differences in your local maps are normal.

Alternatively, from the official file: download **"LTA MRT Station Exit (GEOJSON)"**
from data.gov.sg, save it under `data/raw/`, then:

```powershell
python scripts/load_stations.py --geojson data/raw/LTAMRTStationExitGEOJSON.geojson
```

The loader reads `data/reference/station_opening_years.csv` (221 station codes,
columns `code,opened_year`) and prints how many stations got an opening year.
Sales made before a station opened are not counted as "near the MRT" (see
`data/reference/README.md`). Anything it lists as "no opening year" is treated as
already open: add its code to the CSV and run the loader again.

### 5.3 Nearest station for every block

```powershell
python scripts/build_block_features.py
```

### 5.4 Validate (checks V1–V6)

Only needed if you change the pipeline. The team's results are already saved in
`data/results/geo_validation.json` and `.md` (including check V3, the 30 blocks
checked by hand against the map) and appear on the About and Under the hood pages.

```powershell
python scripts/validate_geo.py
```

This rewrites those result files, so afterwards run `git restore data/results`
unless you mean to replace the team's results. **Check V3 needs a person:** open
`data/results/v3_ground_truth.csv` in Excel, click each `map_link`, and type `Y`
(the point is on the right block) or `N` in the `correct` column. Save as CSV,
then run `python scripts/validate_geo.py` again to score it.

Restart `python app.py` after the pipeline so the maps appear straight away.

### 5.5 Geocode from scratch (almost nobody needs this)

Only if the saved cache is missing or you want to refresh it. It asks OneMap
(Singapore's official geocoder, no key needed) about every block, so it needs
internet and takes about 50 minutes the first time:

```powershell
python scripts/geocode_blocks.py
```

Progress is saved after every request to `data/geo/geocode_cache.jsonl`, so you
can stop it (Ctrl+C) and run it again later: it continues where it stopped. It
only asks about blocks that are not in the cache yet.

## 6. Price-driver analysis

```powershell
python analysis/price_drivers.py
```

Takes a few seconds. It writes `data/results/drivers.json` and the
`driver_effects` table used by each transaction's "What drives this price?"
panel. Run it **after** the geospatial pipeline (5.1 to 5.3), because it needs the
MRT distances. Run `git restore data/results` afterwards unless you mean to replace
the team's saved numbers.
The method is described in the script's header and on the About page.

## 6A. Nearby places and the location model

This adds parks, hawker centres, gyms, childcare centres and primary/secondary schools
to the geospatial pipeline. It powers the "What's nearby?" panel on each transaction page
(the nearest places by name, also drawn on its map). Run it **after** 5.1 to 5.3 (it needs the saved block
locations). Nothing here changes the MRT pipeline: amenities live in their own
MongoDB collection (`amenities`) and PostgreSQL tables.

**1. The files.** The five files below are already in `data/raw/` in the repository, so you
can skip to step 2. To refresh one, download it (free, data.gov.sg; search the dataset name, choose
GeoJSON or CSV) into `data/raw/`:

| Kind | Dataset to search for | File type |
|---|---|---|
| parks | NParks Parks and Nature Reserves | GeoJSON |
| hawker centres | Hawker Centres (NEA) | GeoJSON |
| gyms | Gyms SG (SportSG) | GeoJSON |
| childcare centres | Child Care Services (ECDA) | GeoJSON |
| schools | General information of schools (MOE) | CSV |

**2. Check each file, then load it.** `--dry-run` reads the file and changes
nothing, so you can see how many places it found and whether the names were
understood:

```powershell
python scripts/load_amenities.py --kind park --geojson data/raw/NParksParksandNatureReserves.geojson --dry-run
python scripts/load_amenities.py --kind park --geojson data/raw/NParksParksandNatureReserves.geojson
python scripts/load_amenities.py --kind hawker --geojson data/raw/HawkerCentresGEOJSON.geojson
python scripts/load_amenities.py --kind gym --geojson data/raw/GymsSGGEOJSON.geojson
python scripts/load_amenities.py --kind childcare --geojson data/raw/ChildCareServices.geojson
python scripts/load_amenities.py --schools --csv "data/raw/General information of schools.csv"
```

Your file names may differ; use whatever you downloaded. If it says most places
have no name it recognises, re-run with `--name-field FIELD` (it lists the fields
it saw). The schools file has postal codes, not coordinates, so each postal code is
looked up on OneMap (about 350 lookups, a few minutes). Answers are saved in
`data/geo/amenity_postal_cache.jsonl`, so a second run is instant and an
interrupted run carries on. Each run replaces only the kinds it loads, so it is
safe to repeat.

**3. Compute the distances** (nearest place of each kind, and how many within
500 m, 1 km and 2 km, for every block):

```powershell
python scripts/build_amenity_features.py --compare
```

`--compare` runs it in MongoDB (`$geoNear`) and in PostGIS, writes the MongoDB
rows, and reports how closely the two agree and how long each took
(`data/results/amenity_features_report.json`): useful for the SQL vs NoSQL part of
the report. Without `--compare` it uses MongoDB only (`--engine postgis` for SQL).

**4. Fit the model** (needs `pip install -r requirements-dev.txt`, which adds
scikit-learn):

```powershell
python analysis/amenity_model.py
```

It predicts each sale's price *relative to the market* (its price per sqm divided
by the median for the same flat type and quarter), trains on older sales and tests
on the most recent months, and compares four feature sets: flat attributes only,
+ MRT, + amenities, and both. It writes `data/results/amenity_model.json` and a
per-block **location premium** (`block_location_premium`) shown on the nearby panel.
Run `git restore data/results` afterwards unless you mean to replace the team's
saved numbers.

How to read the results (and what to say in the report):

* The closeness score is simple: each kind scores 100 within a short distance,
  falling in a straight line to 0 at a longer one (school distances follow MOE's
  1 km and 2 km registration phases). It is not a verdict on value.
* The model finds **associations in past sales, not causes**. Distance features
  are block-level, so the model can partly recognise individual blocks; the
  grouped importance and the held-out test months are what keep this honest.
* Locations are as of each file's last update, not the map on each sale date, so places
  that opened or closed since are not tracked. The gym file was last updated between
  2016 and 2018 and the childcare file in December 2021, so newer gyms and centres are
  missing and some listed ones may have closed. Park distance is to the boundary,
  accurate to about 50 m.
* Distances are straight-line, not walking routes. Walking times on the pages are
  an estimate (about 80 m a minute with a 30 % detour).
* Supermarkets were replaced by gyms and childcare. A teammate's database that still has
  them is cleaned the next time any amenity file is loaded.
* The score scales are judgment calls, set from how far away things really are: the median
  block has a gym 1.0 km away (100 within 500 m, 0 at 2.5 km) and a childcare centre
  118 m away (100 within 100 m, 0 at 400 m), so a wider childcare scale would give almost
  every block 100. Childcare centres that share an address stay separate places.
* MongoDB measures on a sphere and PostGIS on the WGS84 spheroid. On the real data the
  nearest-place distances agree for every block-kind pair (58,590 of them: mean gap 1.9 m,
  median 0.3 %, worst 0.7 %, with MongoDB slightly longer), and the counts within 500 m,
  1 km and 2 km are identical for 89 % of pairs (the dense childcare kind has many places
  near a cutoff). Every difference is explained by that gap: moving each cutoff by 0.75 %
  (about 4 m at 500 m, 15 m at 2 km) makes the two engines' counts consistent for all pairs.

## 7. SQL vs NoSQL benchmark

Close other heavy programs, keep the laptop plugged in, then:

```powershell
python bench/run_bench.py --label "Laptop, Docker, plugged in"
```

About 10–15 minutes. Results go to `data/results/benchmark.json` (shown on the
Under the hood page) and raw timings to `bench/results/raw/`. Use
`--quick` for a one-minute smoke test, and `--skip-geo` if you haven't run the
geospatial pipeline yet. The protocol is explained in `bench/README.md`.

Two shorter experiments back up the report (run them after the full benchmark,
same machine, same conditions):

```powershell
python bench/run_bench.py --no-index --label "Laptop, Docker, plugged in"   # ~5 min -> data/results/benchmark_noindex.json
python bench/run_paging.py --label "Laptop, Docker, plugged in"             # ~1 min -> data/results/paging.json
```

- `--no-index` repeats W0–W3 on copies of the flat table and the MongoDB
  collection that keep **only** the primary key / `_id`, and records how many
  times slower every query becomes. It shows how much of the speed comes from
  the engine and how much from the indexes. The copies are deleted at the end.
- `run_paging.py` times page 1 against deep pages with OFFSET and with keyset
  paging, on the search query the site really uses (this is why the API limits
  a search to its first 10,000 results).

One more, for the speed of the home and insights pages:

```powershell
python scripts/time_endpoints.py      # ~1 min -> data/results/endpoint_timing.json
```

It times each question those pages ask twice: with the original live query
(an exact median over every matching sale) and with the small precomputed
views, and checks that both give the same answer.

## 8. Tests

```powershell
python -m pytest tests                    # unit tests: no databases needed
$env:RW_INTEGRATION="1"; python -m pytest tests; Remove-Item Env:RW_INTEGRATION
```

The integration tests create and delete test users, so point them at
your local Docker databases only.

## 9. Using the cloud databases (Neon + MongoDB Atlas) instead

1. In `.env`, replace `DATABASE_URL` and `MONGO_URI` with your Neon and Atlas
   connection strings (the commented examples show the shape).
2. A database loaded before this version: run `python scripts/migrate.py` (adds
   the constraints, triggers and views without deleting anything). A new database:
   run `python scripts/load.py --reset`.
3. PostGIS on Neon: the geospatial scripts run `CREATE EXTENSION postgis`, which
   Neon supports.

Never put passwords anywhere except `.env`. If a password was ever committed to
Git, change it in the Neon / Atlas dashboard: removing it from the code does not
remove it from the history.

### Keeping the two databases consistent

The sales live in PostgreSQL and, as a copy for the comparison, in MongoDB. To
check that the two still agree:

```powershell
python scripts/check_consistency.py
```

It confirms that the MongoDB copy of the sales has as many documents as
PostgreSQL has rows, so the benchmark compares the same data.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `[config] DATABASE_URL is not set` | `.env` is missing or not in the project folder (section 2). |
| `TOKEN_SECRET is missing or too short` | Generate one (section 2) and paste it into `.env`. |
| `connection refused` on port 5433 / 27018 | Docker Desktop isn't running, or run `docker compose up -d`. |
| A port is already in use | Change the left-hand port in `docker-compose.yml` and in `.env`. |
| `No module named 'psycopg'` | You installed an old requirements file: `pip install -r requirements.txt` again. |
| The site shows "Can't reach the server" | `python app.py` isn't running, or open http://localhost:5000 (not the HTML file directly). |
| Geocoding stops with "Cannot reach OneMap" | Internet dropped: run the same command again, it resumes. (Teammates should not be geocoding at all: use `--apply-only`.) |
| Maps and "What drives this price?" are empty | Run section 5.1 to 5.3 and section 6, then restart `python app.py`. |
| `git status` shows files in `data/results/` as modified | The loaders rewrote them. Run `git restore data/results` before you commit. |
| `geocode_cache.jsonl` not found | `git pull` on `main` (the cache is committed in `data/geo/`). |
| Maps still show the old embed | Run the pipeline (section 5), then restart `python app.py`. |
| Momentum table says to run migrate | `python scripts/migrate.py` (only needed on databases loaded by an old `scripts/load.py`). |
| Insights or home page is slow after reloading the data | The precomputed views are out of date, so the site is using the slower live queries: run `python scripts/migrate.py --refresh`, then restart `python app.py`. |

## 11. What each output is used for

| File | Produced by | Used in |
|---|---|---|
| `data/results/load_report.json` | `scripts/load.py` | report: data quality (rows rejected, blocks without property info) |
| `data/results/mongo_load_report.json` | `load_mongo_transactions.py` | report: MongoDB size and load rate |
| `data/results/geo_validation.json/.md`, `v3_ground_truth.csv` | `validate_geo.py` | About + Under the hood pages; report: validation |
| `data/results/drivers.json` | `price_drivers.py` | transaction pages; report: price drivers |
| `data/results/amenity_features_report.json` | `build_amenity_features.py` | report: MongoDB vs PostGIS agreement and speed, how close blocks are to each kind of place |
| `data/results/amenity_model.json` | `amenity_model.py` | report: model accuracy with/without amenities, importance of each group |
| `data/results/benchmark.json`, `bench/results/raw/*.csv` | `run_bench.py` | Under the hood page; report: SQL vs NoSQL comparison |
| `data/results/benchmark_noindex.json`, `bench/results/raw_noindex/*.csv` | `run_bench.py --no-index` | report: what the indexes contribute |
| `data/results/paging.json` | `run_paging.py` | report: cost of deep pages (OFFSET vs keyset) |
| `data/results/endpoint_timing.json` | `scripts/time_endpoints.py` | report: what the precomputed views save |

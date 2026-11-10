# ResaleWise SG

A Singapore HDB resale flat decision-support web app. Buyers and sellers
search, compare, and analyse historical HDB resale prices, with value scoring,
market insights, town profiles (with maps), and community reviews.

- **Buyers:** find comparable flats, see how each is priced against its town's
  median, and shortlist the best value.
- **Sellers:** gauge a fair asking-price range from comparable transactions.

Built on a hybrid **PostgreSQL + MongoDB** backend with a static HTML/CSS/JS
frontend and a Flask API.

---

## ⚡ Quick start (for teammates)

> The cloud databases (Neon + MongoDB Atlas) are **already loaded and shared**
> by the team. You do **not** need to load any data — just run the app.
> You only need **Apache (for the pages)** and **Flask (for the API)** running.

### 1. Clone into XAMPP's web root

**Windows (XAMPP):**
```bat
cd C:\xampp\htdocs
git clone https://github.com/AbdulHakimbmi/resalewise-sg.git
cd resalewise-sg
```

### 2. Install Python dependencies

```bat
pip install -r requirements.txt
```

### 3. Start Apache

Open the **XAMPP Control Panel** → click **Start** next to **Apache**.
(Do **not** start MySQL — this project uses cloud databases.)

### 4. Start the Flask API

In a **separate** terminal, from the project folder:
```bat
cd C:\xampp\htdocs\resalewise-sg
python app.py
```
Wait until you see: `Running on http://127.0.0.1:5000`

### 5. Open the app

```
http://localhost/resalewise-sg/index.html
```

**Both Apache and Flask must be running** for the data-driven pages to work.
Quick health check — open this in your browser, it should return `{"ok": true}`:
```
http://localhost:5000/api/ping
```

---

## 🍎 macOS / Linux (without XAMPP)

You can serve the static files with Python's built-in server instead of Apache.
Use **two terminals**:

**Terminal 1 — the API:**
```bash
git clone https://github.com/AbdulHakimbmi/resalewise-sg.git
cd resalewise-sg
pip install -r requirements.txt
python app.py            # serves the API on http://127.0.0.1:5000
```

**Terminal 2 — the static pages:**
```bash
cd resalewise-sg
python -m http.server 8080
```
Then open: `http://localhost:8080/index.html`

> Note: the frontend calls the API at `http://localhost:5000`. If you serve the
> pages on a different host/port, that's fine — the API base is hard-coded to
> `localhost:5000` in `assets/js/api.js`.

---

## Requirements

- Python 3.11+ (3.12 recommended)
- XAMPP (Apache only) — or any static file server
- Internet access (Neon + Atlas are cloud services)

Python packages (`requirements.txt`):
```
flask
flask-cors
psycopg2-binary
pymongo
pandas
sqlalchemy
```

---

## Pages

| Page | What it does |
|---|---|
| `index.html` | Home — hero search, town carousel, "How it works" (interactive), live stats |
| `search.html` | Find a flat — filters, charts, value scores, load-more results |
| `town.html` | Town profile — stats, recent transactions, map, and reviews |
| `insights.html` | Market insights — interactive medians & trends by flat type/town/window |
| `reviews.html` | Town reviews — read & write community reviews |
| `compare.html` | Shortlist — saved flats scored for value with a recommended pick |
| `details.html` | Single transaction detail + add to shortlist |
| `login.html` | Sign-in page (accounts feature is planned) |

---

## Flask API endpoints (port 5000)

- `GET  /api/ping` — health check
- `GET  /api/options` — towns, flat types, flat models
- `GET  /api/search?town=&flat_type=&flat_model=&budget=&min_lease=&min_area=&max_area=&storey=&limit=&offset=`
- `GET  /api/search-stats?...` — price distribution, scatter, median (same filters as search)
- `GET  /api/details/<txn_id>`
- `GET  /api/txn-value/<txn_id>` — value score vs town+type median
- `GET  /api/insights/town-medians?flat_type=&months=`
- `GET  /api/insights/trend?flat_type=&town=&months=`
- `GET  /api/insights/summary?flat_type=&town=&months=`
- `GET  /api/bookmarks?user_id=` · `POST /api/bookmarks` · `DELETE /api/bookmarks/<txn_id>?user_id=`
- `GET  /api/reviews?town=&limit=` · `POST /api/reviews`

---

## Loading the data (ONE-TIME — only if setting up a new database)

> ⚠️ Most teammates should **skip this** — the shared cloud database is already
> loaded. Only run this if you are creating a brand-new database from scratch.
> **It drops and recreates all tables**, wiping existing bookmarks/reviews.

`load.py` builds the full schema (reference tables, ~985k transactions, and the
`users` / `bookmarks` / `user_profiles` tables) and seeds a default user.
Provide the cleaned CSV path in any of these ways:

```bash
python load.py path/to/resale_clean.csv        # 1. command-line argument
set RESALE_CSV=path/to/resale_clean.csv         # 2. env var (Windows)
export RESALE_CSV=path/to/resale_clean.csv      # 2. env var (macOS/Linux)
# 3. or drop resale_clean.csv next to load.py and run: python load.py
```

---

## Databases

- **PostgreSQL (Neon):** towns, blocks, flat types/models, resale transactions,
  users, bookmarks, user profiles.
- **MongoDB (Atlas, db `hdb`):** town reviews.

Connection strings live directly in `app.py` and `load.py`. This is intentional
for this **private team repo** so all members share the same cloud databases
without extra setup. **Do not make this repo public** with the credentials in it.

---

## Project layout

```
resalewise-sg/
├── index.html  search.html  town.html  insights.html
├── reviews.html  compare.html  details.html  login.html
├── app.py               ← Flask API
├── load.py              ← one-time ETL (Postgres + Mongo)
├── requirements.txt
└── assets/
    ├── css/style.css
    └── js/
        ├── api.js            ← API helper + shared dropdown/town helpers
        ├── app.js            ← toasts / small shared UI
        ├── mist-background.js← animated WebGL background
        ├── town-coverflow.js ← homepage town carousel
        └── ui-polish.js      ← scroll effects, rotating town, live stats
```

---

## Troubleshooting

- **A page says "Could not load…"** → make sure **Flask is running** on port 5000
  (`python app.py`). Apache alone can't serve the API.
- **`/api/ping` doesn't return `{"ok": true}`** → Flask isn't running, or a
  firewall/proxy is blocking port 5000.
- **Dropdowns are empty / charts blank** → the API can't reach the cloud DB;
  check your internet connection (Neon/Atlas are cloud-hosted).
- **Changed backend code?** → restart `python app.py` to pick up new endpoints.

---

## Datasets

- HDB Resale Flat Prices (data.gov.sg)
- HDB Property Information (data.gov.sg)

# ResaleWise SG

ResaleWise SG helps people make sense of Singapore's HDB resale market. Instead
of scrolling through endless listings, you can search real historical
transactions, see how a flat is priced against others in the same town, read
what residents say about a neighbourhood, and shortlist the flats worth a
second look.

I built this as a decision-support tool, not a listing site — every number you
see comes from official HDB resale records.

---

## What you can do

**Find a flat.** Search by town, flat type, budget, remaining lease, floor area,
storey range and flat model. Each result is scored against the median for its
town and flat type, so a "good value" or "premium" tag tells you at a glance how
the price compares.

**Understand the market.** The insights page shows median prices and price
trends over time, and lets you compare towns side by side for any flat type.

**Explore a town.** Open any town to see its median prices, recent transactions,
an interactive map of the area, and reviews left by residents.

**Build a shortlist.** Create a free account, save the flats you like, and
compare them on the shortlist page — it highlights the best-value option and
explains why.

**Leave a review.** Share your take on a town (amenities, transport, atmosphere)
to help other buyers.

---

## Getting the app running

The databases are hosted in the cloud and shared across the team, so you don't
need to load any data — you only need to run the two parts of the app: the web
pages (served by Apache) and the API (Flask).

### On Windows with XAMPP

1. Clone the project into XAMPP's web root:
   ```bat
   cd C:\xampp\htdocs
   git clone https://github.com/AbdulHakimbmi/resalewise-sg.git
   cd resalewise-sg
   ```

2. Install the Python packages:
   ```bat
   pip install -r requirements.txt
   ```

3. Open the **XAMPP Control Panel** and start **Apache**. (You don't need MySQL —
   this project uses cloud databases.)

4. In a separate terminal, start the API:
   ```bat
   cd C:\xampp\htdocs\resalewise-sg
   python app.py
   ```
   Wait until it prints `Running on http://127.0.0.1:5000`.

5. Open the site in your browser:
   ```
   http://localhost/resalewise-sg/index.html
   ```

Both Apache and the Flask API need to be running for the data-driven pages to
work. If you want a quick check that the API is up, open
`http://localhost:5000/api/ping` — you should see `{"ok": true}`.

### On macOS or Linux (no XAMPP)

Use two terminals — one for the API, one for the pages:

```bash
# Terminal 1 — the API
git clone https://github.com/AbdulHakimbmi/resalewise-sg.git
cd resalewise-sg
pip install -r requirements.txt
python app.py

# Terminal 2 — the web pages
cd resalewise-sg
python -m http.server 8080
```

Then open `http://localhost:8080/index.html`. The pages talk to the API at
`http://localhost:5000` (set in `assets/js/api.js`).

---

## A quick tour for new users

1. **Start on the home page.** Use the search box up top, or scroll to the town
   carousel and click a town to open its profile.
2. **Search for flats.** On "Find a flat", type a town name, pick a flat type,
   and set any budget or lease preferences. Results and the charts update as you
   go. Watch the value tag on each result.
3. **Open a listing.** Click "View details" to see the full record, a map of the
   location, and how the price compares to the town median.
4. **Save what you like.** Log in (or create an account), then use "Add to
   shortlist" on any listing.
5. **Compare your shortlist.** The Compare page lines up your saved flats and
   points out the best value, with the reasoning behind it.
6. **Switch themes.** Use the sun/moon button in the navigation bar to toggle
   light or dark mode — your choice is remembered.

---

## Requirements

- Python 3.11 or newer
- XAMPP (Apache) on Windows, or any static file server
- An internet connection (the databases are cloud-hosted)

Python packages are listed in `requirements.txt`: Flask, Flask-CORS,
psycopg2-binary, pymongo, pandas and SQLAlchemy.

---

## For the team: loading the data (one time only)

You almost certainly do **not** need this — the shared database is already set
up. Only run this if you're building a fresh database from scratch. Note that it
drops and recreates every table, so it will erase existing accounts, bookmarks
and reviews.

```bash
python load.py path/to/resale_clean.csv     # pass the CSV path directly
# or set RESALE_CSV, or drop resale_clean.csv next to load.py and run: python load.py
```

Connection strings live in `app.py` and `load.py`. That's deliberate for this
private team repo so everyone shares the same databases — please keep the repo
private.

To make sure every town has a healthy set of community reviews, you can top up
the review collection (this only adds what's missing, so existing reviews are
left alone):

```bash
python seed_reviews.py        # ensures at least 15 reviews per town
python seed_reviews.py 20     # or a higher minimum
```

---

## How it's built

- **Web pages:** plain HTML, CSS and JavaScript (no framework).
- **API:** Flask (`app.py`), talking to PostgreSQL for transactions/accounts and
  MongoDB for town reviews.
- **Accounts:** email + password with hashed passwords and signed tokens; each
  account has its own shortlist.

```
resalewise-sg/
├── index.html      home
├── search.html     find a flat
├── town.html       town profile + map + reviews
├── insights.html   market insights
├── reviews.html    town reviews
├── compare.html    shortlist & value comparison
├── details.html    single transaction + map
├── login.html / register.html / account.html
├── app.py          API
├── load.py         one-time data loader
├── requirements.txt
└── assets/
    ├── css/style.css
    └── js/  (api, auth, app, mist-background, town-coverflow, ui-polish)
```

---

## Data source

HDB Resale Flat Prices and HDB Property Information, from data.gov.sg.

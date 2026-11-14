"""Search, filter options, transaction details, value context and CSV export
(PostgreSQL)."""
import csv
import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

from .. import db
from ..benchmarks import (_add_months, _quarter_start, benchmarks_for, norm_flat_model, norm_flat_type,
                          value_score, verdict_for)
from ..web import ApiError, arg_float, arg_int, arg_str, cache_json, rate_limit

bp = Blueprint("search", __name__)


@bp.route("/api/ping")
def ping():
    return jsonify({"ok": True})


@bp.route("/api/options")
def options():
    return jsonify({
        "towns": [r["town_name"] for r in db.q("SELECT town_name FROM towns ORDER BY town_name")],
        "flat_types": [r["flat_type_name"] for r in db.q("SELECT UPPER(REPLACE(flat_type_name, '-', ' ')) AS flat_type_name FROM flat_types GROUP BY 1 ORDER BY MIN(flat_type_id)")],
        "flat_models": [r["flat_model_name"] for r in db.q("SELECT UPPER(REPLACE(flat_model_name, '-', ' ')) AS flat_model_name FROM flat_models GROUP BY 1 ORDER BY 1")],
    })


def parse_filters(args):
    """Validate the search filters once; shared by the SQL builder below and the
    MongoDB equivalent in routes/engines.py, so both engines answer the same
    question. A malformed value is a 400 with a clear message, never ignored."""
    f = {
        "town":       arg_str(args, "town"),
        "flat_type":  norm_flat_type(arg_str(args, "flat_type")),
        "flat_model": norm_flat_model(arg_str(args, "flat_model")),
        "budget":     arg_int(args, "budget", lo=0, hi=50_000_000),
        "min_lease":  arg_int(args, "min_lease", lo=0, hi=99),            # YEARS
        "min_area":   arg_float(args, "min_area", lo=0, hi=1000),
        "max_area":   arg_float(args, "max_area", lo=0, hi=1000),
        "storey":     arg_str(args, "storey", max_len=8),                 # band "min-max", e.g. "4-6"
        "storey_lo":  None, "storey_hi": None,
    }
    if f["min_area"] is not None and f["max_area"] is not None and f["min_area"] > f["max_area"]:
        raise ApiError("'min_area' cannot be larger than 'max_area'.", 400, "invalid_parameter")
    if f["storey"]:
        if not re.fullmatch(r"\d{0,2}-\d{0,2}", f["storey"]):
            raise ApiError("'storey' must look like '4-6' or '13-'.", 400, "invalid_parameter")
        parts = f["storey"].split("-")
        f["storey_lo"] = int(parts[0]) if parts[0] else None
        f["storey_hi"] = int(parts[1]) if len(parts) > 1 and parts[1] else None
    return f


def build_search_filters(args, minimal=False):
    """Single source of truth for search filters, shared by /api/search,
    /api/search-stats and the CSV export so results and charts always agree.
    Returns (from_where_sql, params, any_filter). Values are always bound as
    parameters, never interpolated into the SQL text.

    minimal=True keeps only the joins the filters actually use. Counts,
    histograms and samples never read the lookup tables, and the foreign keys
    guarantee that every sale has a block, flat type and flat model, so leaving
    an unused join out cannot change a result; it only saves work (an unfiltered
    count over ~1M sales: about 215 ms with four joins, 37 ms without)."""
    f = parse_filters(args)
    fw = "\n      FROM resale_transactions t"
    if not minimal or f["town"]:
        fw += "\n      JOIN blocks b       ON b.block_id = t.block_id\n      JOIN towns tw       ON tw.town_id = b.town_id"
    if not minimal or f["flat_type"]:
        fw += "\n      JOIN flat_types ft  ON ft.flat_type_id = t.flat_type_id"
    if not minimal or f["flat_model"]:
        fw += "\n      JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id"
    fw += "\n      WHERE 1=1\n    "
    p = []
    any_filter = False
    if f["town"]:       fw += " AND tw.town_name = %s";              p.append(f["town"]);             any_filter = True
    if f["flat_type"]:  fw += " AND UPPER(REPLACE(ft.flat_type_name, '-', ' ')) = %s";  p.append(f["flat_type"]);  any_filter = True
    if f["flat_model"]: fw += " AND UPPER(REPLACE(fm.flat_model_name, '-', ' ')) = %s"; p.append(f["flat_model"]); any_filter = True
    if f["budget"]:     fw += " AND t.resale_price <= %s";           p.append(f["budget"]);           any_filter = True
    if f["min_lease"]:  fw += " AND t.remaining_lease_months >= %s"; p.append(f["min_lease"] * 12);   any_filter = True
    if f["min_area"]:   fw += " AND t.floor_area_sqm >= %s";         p.append(f["min_area"]);         any_filter = True
    if f["max_area"]:   fw += " AND t.floor_area_sqm <= %s";         p.append(f["max_area"]);         any_filter = True
    # Storey band: "lo-hi" (e.g. "4-6") or "13-" for 13-and-above.
    if f["storey_lo"] is not None:
        fw += " AND t.storey_high >= %s"; p.append(f["storey_lo"]); any_filter = True
    if f["storey_hi"] is not None:
        fw += " AND t.storey_low <= %s";  p.append(f["storey_hi"]); any_filter = True
    return fw, p, any_filter


def annotate_value(rows):
    """Judge every row against sales from ITS OWN period (not the whole result
    set, which would mix 1990 and 2026 prices and call old sales "bargains")."""
    benches = benchmarks_for([(r["town_name"], r["flat_type_name"], r["month"]) for r in rows]) if rows else []
    for r, bm in zip(rows, benches):
        r["resale_price"] = float(r["resale_price"])
        r["floor_area_sqm"] = float(r["floor_area_sqm"])
        r["month"] = r["month"].isoformat()
        if bm:
            diff = round((r["resale_price"] - bm["median_price"]) / bm["median_price"] * 100, 1)
            r.update({"benchmark_median": bm["median_price"], "benchmark_n": bm["n"],
                      "benchmark_label": bm["label"], "price_diff_pct": diff,
                      "verdict": verdict_for(diff)})
        else:
            r.update({"benchmark_median": None, "benchmark_n": 0, "benchmark_label": None,
                      "price_diff_pct": None, "verdict": verdict_for(None)})
    return rows


SEARCH_COLUMNS = """
      SELECT t.txn_id, t.month, tw.town_name, UPPER(REPLACE(ft.flat_type_name, '-', ' ')) AS flat_type_name,
             b.block_no, b.street_name,
             t.storey_low, t.storey_high, t.floor_area_sqm,
             UPPER(REPLACE(fm.flat_model_name, '-', ' ')) AS flat_model_name, t.remaining_lease_months,
             t.resale_price
"""


# Deepest result a person can page to. Paging with OFFSET reads and discards every
# earlier row, so its cost grows with the page number (measured on ~1M sales:
# ~3 ms for page 1, ~40 ms at 10,000 rows, ~750 ms at 900,000). Capping the window
# keeps every page fast; beyond it the person narrows the filters or downloads
# the CSV (also capped at 10,000 rows).
MAX_WINDOW = 10_000


@bp.route("/api/search")
@cache_json(600)
def search():
    limit = arg_int(request.args, "limit", 20, lo=1, hi=200)
    page = arg_int(request.args, "page", None, lo=1, hi=10_000_000)
    offset = (page - 1) * limit if page else arg_int(request.args, "offset", 0, lo=0)

    # Total matching rows: counted without the joins the filters do not need.
    count_from, count_params, _ = build_search_filters(request.args, minimal=True)
    total = db.q("SELECT COUNT(*) AS n " + count_from, count_params)[0]["n"]
    window = min(total, MAX_WINDOW)
    pages = max(1, -(-window // limit))             # ceiling division
    offset = min(offset, (pages - 1) * limit)       # a page past the end (old link) shows the last page

    # Newest sales first, with txn_id as the tie-breaker so a page never repeats
    # or skips a row. This is exactly the order of index idx_txn_month_id, so the
    # database reads the first rows of the index and stops.
    rows = []
    if offset < total:
        from_where, filters, _ = build_search_filters(request.args)
        rows = db.q(SEARCH_COLUMNS + from_where + " ORDER BY t.month DESC, t.txn_id DESC LIMIT %s OFFSET %s",
                    filters + [limit, offset])
        annotate_value(rows)

    return jsonify({
        "rows": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "page": offset // limit + 1,
        "pages": pages,
        "window": window,
        "capped": total > MAX_WINDOW,
        "has_more": (offset + len(rows)) < window,
    })


# The HDB data only changes when scripts/load.py reloads it (restart the app after that), so
# the answer for a given set of filters can be kept for hours instead of minutes.
SEARCH_STATS_TTL = 6 * 3600


@bp.route("/api/search-stats")
@cache_json(SEARCH_STATS_TTL)
def search_stats():
    """Aggregate stats for the current filters (charts on the search page).
    Uses the SAME filter builder as /api/search so charts and results agree.

    The unfiltered page is the expensive one (about a million rows, two exact
    medians that each need a sort: ~1.3 s one after another). The count, the
    medians, the histogram and the scatter sample do not depend on each other
    once the price range is known, so they run on separate pooled connections
    at the same time (~0.8 s cold, instant once cached)."""
    fw, p, _ = build_search_filters(request.args, minimal=True)

    stats = db.q("""
      SELECT COUNT(*)::int AS n,
             ROUND(MIN(t.resale_price))::int AS min_price,
             ROUND(MAX(t.resale_price))::int AS max_price
    """ + fw, p)[0]

    n = stats["n"] or 0
    if n == 0:
        return jsonify({"n": 0, "median_price": None, "buckets": [], "scatter": []})

    # Price distribution: NB evenly-spaced buckets between min and max.
    lo = stats["min_price"] or 0
    hi = stats["max_price"] or 0
    NB = 12
    width = max((hi - lo) / NB, 1)
    # Scatter sample: about 400 points of (floor_area, price) for the cloud. Every
    # step-th sale by id (step = n / 400) is an even spread over time and needs no
    # sort; ordering a million rows by a hash for the same job took ~1 s.
    step = -(-n // 400)

    jobs = {
        "median_price": ("SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS v"
                         + fw, p),
        "median_psm": ("SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP ("
                       "ORDER BY (t.resale_price / NULLIF(t.floor_area_sqm, 0))))::int AS v" + fw, p),
        "buckets": ("""SELECT LEAST(FLOOR((t.resale_price - %s) / %s), %s)::int AS bucket,
                              COUNT(*)::int AS count""" + fw + " GROUP BY bucket ORDER BY bucket",
                    [lo, width, NB - 1] + p),
        "scatter": ("""SELECT ROUND(t.floor_area_sqm)::int AS area, ROUND(t.resale_price)::int AS price"""
                    + fw + " AND mod(t.txn_id, %s) = 0 LIMIT 400", p + [step]),
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {k: pool.submit(db.q, sql, params) for k, (sql, params) in jobs.items()}
        out = {k: f.result() for k, f in futures.items()}      # an error in any query surfaces here

    counts = {r["bucket"]: r["count"] for r in out["buckets"]}
    buckets = []
    for i in range(NB):
        buckets.append({"low": round(lo + i * width), "high": round(lo + (i + 1) * width),
                        "count": counts.get(i, 0)})

    return jsonify({
        "n": n,
        "min_price": stats["min_price"],
        "max_price": stats["max_price"],
        "median_price": out["median_price"][0]["v"],
        "median_psm": out["median_psm"][0]["v"],
        "buckets": buckets,
        "scatter": out["scatter"],
    })


@bp.route("/api/details/<int:txn_id>")
def details(txn_id):
    rows = db.q("""
      SELECT t.txn_id, t.month, tw.town_name, tw.region, b.block_id,
             UPPER(REPLACE(ft.flat_type_name, '-', ' ')) AS flat_type_name, UPPER(REPLACE(fm.flat_model_name, '-', ' ')) AS flat_model_name,
             b.block_no, b.street_name, b.max_floor_lvl,
             b.year_completed, b.total_dwelling_units,
             t.storey_low, t.storey_high,
             t.floor_area_sqm, t.lease_commence_date,
             t.remaining_lease_months, t.resale_price
      FROM resale_transactions t
      JOIN blocks b       ON b.block_id = t.block_id
      JOIN towns tw       ON tw.town_id = b.town_id
      JOIN flat_types ft  ON ft.flat_type_id = t.flat_type_id
      JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id
      WHERE t.txn_id = %s
    """, [txn_id])
    if not rows:
        raise ApiError("That transaction could not be found.", 404, "not_found")
    r = rows[0]
    r["resale_price"]   = float(r["resale_price"])
    r["floor_area_sqm"] = float(r["floor_area_sqm"])
    r["month"]          = r["month"].isoformat()
    area = r["floor_area_sqm"] or 0
    r["price_per_sqm"]  = round(r["resale_price"] / area, 2) if area else None
    return jsonify(r)


@bp.route("/api/txn-value/<int:txn_id>")
def txn_value(txn_id):
    """Value context for one transaction: its price vs the median for the same
    town + flat type in the same period, price-per-sqm, and an explainable
    value score. Powers the compare page's 'best value' recommendation."""
    base = db.q("""
      SELECT t.txn_id, t.month, t.resale_price, t.floor_area_sqm,
             t.remaining_lease_months, tw.town_name, UPPER(REPLACE(ft.flat_type_name, '-', ' ')) AS flat_type_name
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE t.txn_id = %s
    """, [txn_id])
    if not base:
        raise ApiError("That transaction could not be found.", 404, "not_found")
    r = base[0]
    price = float(r["resale_price"])
    area  = float(r["floor_area_sqm"] or 0)
    psm   = round(price / area, 2) if area else None

    bench = benchmarks_for([(r["town_name"], r["flat_type_name"], r["month"])])[0] \
        or {"median_price": None, "median_psm": None, "n": 0, "label": None}
    median_price = bench["median_price"]
    median_psm   = bench["median_psm"]

    price_diff_pct = round((price - median_price) / median_price * 100, 1) if median_price else None
    psm_diff_pct   = round((psm - median_psm) / median_psm * 100, 1) if (median_psm and psm) else None

    # Peers' lease, not a fixed 80 years: in 1990 almost every flat had 90+
    # years left, so a fixed pivot would flatter every old sale.
    lease_years = (r["remaining_lease_months"] or 0) / 12.0
    peer_lease_years = (bench.get("median_lease_months") or 80 * 12) / 12.0
    score = value_score(price_diff_pct, lease_years, peer_lease_years)

    return jsonify({
        "txn_id": r["txn_id"],
        "town_name": r["town_name"],
        "flat_type_name": r["flat_type_name"],
        "resale_price": price,
        "price_per_sqm": psm,
        "median_price": median_price,
        "median_psm": median_psm,
        "price_diff_pct": price_diff_pct,
        "psm_diff_pct": psm_diff_pct,
        "lease_years": round(lease_years, 1),
        "peer_lease_years": round(peer_lease_years, 1),
        "value_score": score,
        "verdict": verdict_for(price_diff_pct),
        "benchmark_n": bench["n"],
        "benchmark_label": bench["label"],
        "month": r["month"].isoformat(),
    })


@bp.route("/api/txn-peers/<int:txn_id>")
@cache_json(3600)
def txn_peers(txn_id):
    """The matched sales a transaction is judged against (same group the value
    label uses: town + flat type + quarter, widened to the year or to all towns
    when there are too few), with every price, for the home page's dot strip."""
    base = db.q("""
      SELECT t.txn_id, t.month, t.resale_price, tw.town_name,
             UPPER(REPLACE(ft.flat_type_name, '-', ' ')) AS flat_type_name
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE t.txn_id = %s
    """, [txn_id])
    if not base:
        raise ApiError("That transaction could not be found.", 404, "not_found")
    r = base[0]
    bench = benchmarks_for([(r["town_name"], r["flat_type_name"], r["month"])])[0]
    price = float(r["resale_price"])
    if not bench:
        return jsonify({"txn_id": txn_id, "price": price, "n": 0, "prices": []})
    qs, level = _quarter_start(r["month"]), bench["level"]
    lo, hi = (qs.replace(month=1), qs.replace(year=qs.year + 1, month=1)) if level == 2 else (qs, _add_months(qs, 3))
    where = "UPPER(REPLACE(ft.flat_type_name, '-', ' ')) = %s AND t.month >= %s AND t.month < %s"
    params = [r["flat_type_name"], lo, hi]
    if level != 3:
        where = "tw.town_name = %s AND " + where
        params = [r["town_name"]] + params
    prices = [float(x["p"]) for x in db.q("""
      SELECT t.resale_price AS p
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE """ + where + " ORDER BY t.resale_price", params)]
    n = len(prices)
    lower = sum(1 for x in prices if x < price)
    shown = prices if n <= 600 else [prices[round(i * (n - 1) / 599)] for i in range(600)]
    return jsonify({"txn_id": txn_id, "price": price, "n": n, "level": level,
                    "label": bench["label"], "median_price": bench["median_price"],
                    "pct_priced_lower": round(100 * lower / n) if n else None,
                    "prices": shown})


CSV_COLUMNS = [
    ("txn_id", "txn_id"), ("month", "month"), ("town", "town_name"),
    ("flat_type", "flat_type_name"), ("block", "block_no"), ("street", "street_name"),
    ("storey_low", "storey_low"), ("storey_high", "storey_high"),
    ("floor_area_sqm", "floor_area_sqm"), ("flat_model", "flat_model_name"),
    ("remaining_lease_months", "remaining_lease_months"), ("resale_price", "resale_price"),
]


def _csv_cell(v):
    """Neutralise spreadsheet formula injection (cells starting = + - @)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


@bp.route("/api/search.csv")
@rate_limit(10)
def search_csv():
    """Download the current search as CSV: exactly the same filters as
    /api/search (shared build_search_filters), newest transactions first."""
    limit = arg_int(request.args, "limit", 5000, lo=1, hi=10000)
    from_where, filters, _ = build_search_filters(request.args)
    rows = db.q(SEARCH_COLUMNS + from_where + " ORDER BY t.month DESC, t.txn_id DESC LIMIT %s",
                filters + [limit])

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in CSV_COLUMNS])
    for r in rows:
        w.writerow([_csv_cell(r["month"].isoformat() if k == "month" else r[k])
                    for _, k in CSV_COLUMNS])
    fname = "resalewise-search-%s.csv" % datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})

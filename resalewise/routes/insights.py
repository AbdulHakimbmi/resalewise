"""Market insights (PostgreSQL aggregates and window functions)."""
from flask import Blueprint, jsonify, request

from .. import db
from ..benchmarks import norm_flat_type
from ..web import ApiError, arg_int, arg_str, cache_get, cache_json, cache_put, cached, rate_limit

bp = Blueprint("insights", __name__)


def _latest_data_month():
    """The newest transaction month in the dataset. Anchoring 'last N months'
    windows to the data (not CURRENT_DATE) keeps a static dataset meaningful."""
    row = db.q("SELECT MAX(month) AS m FROM resale_transactions")[0]
    return row["m"]


# ---------------------------------------------------------------------------
# Speed: the dataset only changes when it is reloaded, so these answers are
# cached for a day and, for the windows the website offers, read from small
# materialized views (sql/schema_extras.sql, 3c and 3d) instead of computing
# exact medians over up to ~400k sales per request. Every endpoint keeps its
# live query as the fallback, and tests/test_integration.py checks that both
# paths return the same numbers.
# ---------------------------------------------------------------------------
STATIC_TTL = 24 * 3600
PRECOMPUTED_WINDOWS = (12, 24, 36, 60, 120)      # = the months in mv_window_stats

WINDOW_SQL = "t.month >= ((SELECT MAX(month) FROM resale_transactions) - (%s || ' months')::interval)"


def precomputed_ok():
    """True when the precomputed views exist AND were built from the data that
    is loaded now. A reload without `python scripts/migrate.py --refresh` makes
    this False, so the API falls back to live queries: slower, never stale."""
    def check():
        if not (db.has_relation("mv_window_stats") and db.has_relation("mv_quarter_stats")
                and db.has_relation("mv_town_quarter_stats")):
            return False
        rows = db.q("SELECT (SELECT MAX(anchor_month) FROM mv_window_stats) AS a, "
                    "(SELECT MAX(month) FROM resale_transactions) AS m")
        return bool(rows) and rows[0]["a"] is not None and rows[0]["a"] == rows[0]["m"]
    return cached("precomputed_ok", 60, check)


def _use_window_view(months, live):
    return not live and months in PRECOMPUTED_WINDOWS and precomputed_ok()


def _live_flag():
    """?source=live forces the original query (used by the equivalence tests
    and the timing script; harmless for anyone else)."""
    return request.args.get("source") == "live"


# --- town medians -----------------------------------------------------------
def town_medians_rows(flat_type, months, live=False):
    if _use_window_view(months, live):
        return db.q("""
          SELECT town_name, median_price, median_psm, txn_count
          FROM mv_window_stats
          WHERE months = %s AND flat_type_name = %s AND town_name <> '' AND txn_count >= 5
          ORDER BY median_price DESC, town_name
        """, [months, flat_type])
    return db.q("""
      WITH recent AS (
        SELECT t.txn_id, t.resale_price, t.floor_area_sqm, b.town_id
        FROM resale_transactions t
        JOIN blocks b      ON b.block_id = t.block_id
        JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
        WHERE UPPER(REPLACE(ft.flat_type_name, '-', ' ')) = %s
          AND """ + WINDOW_SQL + """
      )
      SELECT tw.town_name,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r.resale_price))::int AS median_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY (r.resale_price / NULLIF(r.floor_area_sqm, 0))))::int AS median_psm,
             COUNT(*)::int AS txn_count
      FROM recent r
      JOIN towns tw ON tw.town_id = r.town_id
      GROUP BY tw.town_name
      HAVING COUNT(*) >= 5
      ORDER BY median_price DESC, tw.town_name
    """, [flat_type, months])


@bp.route("/api/insights/town-medians")
@cache_json(STATIC_TTL)
def town_medians():
    flat_type = norm_flat_type(arg_str(request.args, "flat_type", "4 ROOM"))
    months    = arg_int(request.args, "months", 24, lo=1, hi=240)
    rows = town_medians_rows(flat_type, months, _live_flag())
    return jsonify({"flat_type": flat_type, "months": months, "rows": rows})


# --- quarterly trend --------------------------------------------------------
def trend_rows(flat_type, months, town=None, live=False):
    """Quarterly median price. 'Last N months' means the last N/3 WHOLE
    quarters of data, so the first point is never a partial quarter built
    from a handful of sales."""
    first_q = ("(DATE_TRUNC('quarter', (SELECT MAX(month) FROM resale_transactions))"
               " - make_interval(months => GREATEST(%s, 3)) + INTERVAL '3 months')::date")
    if not live and precomputed_ok():
        if town:
            sql = ("SELECT quarter, median_price, txn_count FROM mv_town_quarter_stats "
                   "WHERE flat_type_name = %s AND town_name = %s AND quarter >= " + first_q +
                   " ORDER BY quarter")
            p = [flat_type, town, months]
        else:
            sql = ("SELECT quarter, median_price, txn_count FROM mv_quarter_stats "
                   "WHERE flat_type_name = %s AND quarter >= " + first_q + " ORDER BY quarter")
            p = [flat_type, months]
        return db.q(sql, p)
    sql = """
      SELECT DATE_TRUNC('quarter', t.month)::date AS quarter,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             COUNT(*)::int AS txn_count
      FROM resale_transactions t
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      WHERE UPPER(REPLACE(ft.flat_type_name, '-', ' ')) = %s
        AND t.month >= """ + first_q
    p = [flat_type, months]
    if town:
        sql += " AND tw.town_name = %s"
        p.append(town)
    sql += " GROUP BY quarter ORDER BY quarter ASC"
    return db.q(sql, p)


@bp.route("/api/insights/trend")
@cache_json(STATIC_TTL)
def trend():
    flat_type = norm_flat_type(arg_str(request.args, "flat_type", "4 ROOM"))
    town      = arg_str(request.args, "town")       # optional; UPPERCASE if given
    months    = arg_int(request.args, "months", 60, lo=1, hi=240)
    rows = trend_rows(flat_type, months, town, _live_flag())
    for r in rows:
        r["quarter"] = r["quarter"].isoformat()
    return jsonify({"flat_type": flat_type, "town": town, "months": months, "rows": rows})


# --- headline summary -------------------------------------------------------
SUMMARY_KEYS = ("txn_count", "median_price", "min_price", "max_price", "median_psm", "avg_area")


def _extremes(by_town):
    """Cheapest / priciest town among towns with 5+ sales (whole-market view)."""
    by_town = [r for r in by_town if r["txn_count"] >= 5]
    if not by_town:
        return {"cheapest_town": None, "cheapest_price": None, "priciest_town": None, "priciest_price": None}
    lo = min(by_town, key=lambda r: (r["median_price"], r["town_name"]))
    hi = max(by_town, key=lambda r: (r["median_price"], r["town_name"]))
    return {"cheapest_town": lo["town_name"], "cheapest_price": lo["median_price"],
            "priciest_town": hi["town_name"], "priciest_price": hi["median_price"]}


def summary_parts(flat_type, months, town=None, live=False):
    if _use_window_view(months, live):
        rows = db.q("SELECT town_name, " + ", ".join(SUMMARY_KEYS) + " FROM mv_window_stats "
                    "WHERE months = %s AND flat_type_name = %s", [months, flat_type])
        mine = next((r for r in rows if r["town_name"] == (town or "")), None)
        agg = {k: (mine[k] if mine else (0 if k == "txn_count" else None)) for k in SUMMARY_KEYS}
        return agg, _extremes([r for r in rows if r["town_name"]])

    where = """
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE UPPER(REPLACE(ft.flat_type_name, '-', ' ')) = %s
        AND """ + WINDOW_SQL
    p = [flat_type, months]
    if town:
        where += " AND tw.town_name = %s"
        p.append(town)
    agg = db.q("""
      SELECT COUNT(*)::int AS txn_count,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             ROUND(MIN(t.resale_price))::int AS min_price,
             ROUND(MAX(t.resale_price))::int AS max_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY (t.resale_price / NULLIF(t.floor_area_sqm, 0))))::int AS median_psm,
             ROUND(AVG(t.floor_area_sqm))::int AS avg_area
    """ + where, p)[0]
    return agg, _extremes(town_medians_rows(flat_type, months, live=True))


@bp.route("/api/insights/summary")
@cache_json(STATIC_TTL)
def insights_summary():
    """Headline numbers for a flat type (optionally one town) within the last N
    months of the dataset. Powers the stat cards and the home-page hero."""
    flat_type = norm_flat_type(arg_str(request.args, "flat_type", "4 ROOM"))
    town      = arg_str(request.args, "town")
    months    = arg_int(request.args, "months", 24, lo=1, hi=240)
    agg, extremes = summary_parts(flat_type, months, town, _live_flag())
    latest = _latest_data_month()
    return jsonify({
        "flat_type": flat_type,
        "town": town,
        "months": months,
        "latest_month": latest.isoformat() if latest else None,
        "summary": agg,
        "extremes": extremes,
    })


@bp.route("/api/insights/sparklines")
@rate_limit(60)
def sparklines():
    """Quarterly median price (last 36 months) for several town + flat type
    pairs in ONE query — drives the little trend line on each result card.

        GET /api/insights/sparklines?pair=TAMPINES|4 ROOM&pair=BEDOK|3 ROOM
    """
    raw_pairs = request.args.getlist("pair")
    if not raw_pairs:
        raise ApiError("Provide at least one 'pair' as TOWN|FLAT TYPE.", 400, "invalid_parameter")
    if len(raw_pairs) > 40:
        raise ApiError("At most 40 pairs per request.", 400, "invalid_parameter")

    pairs = []
    for p in dict.fromkeys(raw_pairs):                      # de-dupe, keep order
        parts = p.split("|")
        if len(parts) != 2 or not all(x.strip() and len(x) <= 64 for x in parts):
            raise ApiError("Each 'pair' must look like TOWN|FLAT TYPE.", 400, "invalid_parameter")
        pairs.append((parts[0].strip().upper(), norm_flat_type(parts[1].strip().upper())))

    SPARK_TTL = 6 * 3600
    out, missing = {}, []
    for pr in pairs:
        key = "%s|%s" % pr
        found, series = cache_get("spark:" + key, SPARK_TTL)
        if found:
            out[key] = series
        else:
            missing.append(pr)

    if missing:
        rows = db.q("""
          SELECT tw.town_name, UPPER(REPLACE(ft.flat_type_name, '-', ' ')) AS flat_type_name,
                 DATE_TRUNC('quarter', t.month)::date AS quarter,
                 ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price
          FROM resale_transactions t
          JOIN blocks b      ON b.block_id = t.block_id
          JOIN towns tw      ON tw.town_id = b.town_id
          JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
          WHERE (tw.town_name, UPPER(REPLACE(ft.flat_type_name, '-', ' '))) IN %s
            AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - INTERVAL '36 months')
          GROUP BY 1, 2, quarter
          ORDER BY 1, 2, quarter
        """, [tuple(missing)])
        fresh = {"%s|%s" % pr: [] for pr in missing}
        for r in rows:
            fresh["%s|%s" % (r["town_name"], r["flat_type_name"])].append(
                {"quarter": r["quarter"].isoformat(), "median_price": r["median_price"]})
        for key, series in fresh.items():
            cache_put("spark:" + key, series)
            out[key] = series
    return jsonify({"series": out})


TOWN_CARDS_SQL = """
WITH latest AS (SELECT MAX(month) AS m FROM resale_transactions),
block_sales AS (                       -- sales per block, last 24 months
  SELECT t.block_id, COUNT(*)::int AS n
  FROM resale_transactions t, latest
  WHERE t.month > latest.m - INTERVAL '24 months'
  GROUP BY t.block_id
),
ranked AS (                            -- each town's most-sold block (ties: lowest id)
  SELECT b.town_id, b.block_no, b.street_name, b.max_floor_lvl, b.year_completed,
         b.total_dwelling_units, b.has_commercial, s.n,
         ROW_NUMBER() OVER (PARTITION BY b.town_id ORDER BY s.n DESC, b.block_id) AS rk
  FROM block_sales s
  JOIN blocks b USING (block_id)
  WHERE b.max_floor_lvl IS NOT NULL
),
town_sales AS (                        -- all sales per town, last 12 months
  SELECT b.town_id, COUNT(*)::int AS n12
  FROM resale_transactions t
  JOIN blocks b USING (block_id), latest
  WHERE t.month > latest.m - INTERVAL '12 months'
  GROUP BY b.town_id
)
SELECT tw.town_name, r.block_no, r.street_name, r.max_floor_lvl AS storeys,
       r.year_completed, r.total_dwelling_units AS units, COALESCE(r.has_commercial, FALSE) AS has_commercial,
       r.n AS block_sales_24m, COALESCE(ts.n12, 0) AS town_sales_12m
FROM ranked r
JOIN towns tw USING (town_id)
LEFT JOIN town_sales ts USING (town_id)
WHERE r.rk = 1
ORDER BY tw.town_name
"""


@bp.route("/api/town-cards")
@cache_json(STATIC_TTL)
def town_cards():
    """One real block per town for the home-page town cards: the block with the
    most resales in the last 24 months, with the HDB property facts used to
    draw it (storeys, year completed, dwelling units, shops at ground level)."""
    rows = db.q(TOWN_CARDS_SQL)
    return jsonify({"rows": rows})

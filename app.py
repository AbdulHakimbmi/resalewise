from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2, psycopg2.extras

app = Flask(__name__)
CORS(app)

PG = "postgresql://neondb_owner:npg_IVPBYS42cCKi@ep-round-waterfall-b3vp42ej-pooler.c-4.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"  # <-- EDIT

from pymongo import MongoClient
MONGO = "mongodb+srv://abdulhakimbmi_db_user:x88qNSieCF08vln7@hdbresale.yteparb.mongodb.net/hdb?appName=hdbresale"
mongo = MongoClient(MONGO)
db = mongo["hdb"]

def q(sql, params=None):
    with psycopg2.connect(PG) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or [])
        return cur.fetchall()

def _ensure_schema():
    """Lightweight, idempotent migrations so the app works against an existing
    shared database without re-running the full ETL. Safe to run every start."""
    try:
        with psycopg2.connect(PG) as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(80)")
    except Exception as e:
        print("[schema] migration skipped:", e)

_ensure_schema()

# ---------------------------------------------------------------------------
# Authentication (token-based)
# ---------------------------------------------------------------------------
# We use signed bearer tokens (not cookies) because the frontend (Apache :80)
# and this API (:5000) are different origins — cross-origin session cookies are
# unreliable over plain-HTTP localhost. Tokens live in the browser's
# localStorage and are sent as "Authorization: Bearer <token>".
#
# Token format:  base64url(payload_json) . base64url(hmac_sha256(payload))
# Passwords are hashed with Werkzeug (ships with Flask) — never stored plain.
import hmac, hashlib, base64, json, time, functools
from werkzeug.security import generate_password_hash, check_password_hash

# Secret for signing tokens. For this team project it's fine inline; in a real
# deployment this would come from an environment variable.
TOKEN_SECRET = b"resalewise-sg-dev-secret-change-me"
TOKEN_TTL    = 60 * 60 * 24 * 30   # 30 days

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def make_token(user_id: int, email: str) -> str:
    # Name is not embedded in the token (it can change); it's returned in the
    # login/register/me response bodies instead.
    payload = {"uid": user_id, "email": email, "exp": int(time.time()) + TOKEN_TTL}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig  = _b64(hmac.new(TOKEN_SECRET, body.encode(), hashlib.sha256).digest())
    return body + "." + sig

def verify_token(token: str):
    """Return the payload dict if the token is valid and unexpired, else None."""
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(TOKEN_SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None

def current_user():
    """Resolve the logged-in user from the Authorization header, or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = verify_token(auth[7:])
    if not payload:
        return None
    rows = q("SELECT user_id, email, name, created_at FROM users WHERE user_id = %s", [payload["uid"]])
    return rows[0] if rows else None

def require_auth(fn):
    """Decorator: 401 unless a valid token is present. Injects `user` kwarg."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "authentication required"}), 401
        return fn(*args, user=user, **kwargs)
    return wrapper


@app.route("/api/register", methods=["POST"])
def register():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    pw    = data.get("password") or ""
    name  = (data.get("name") or "").strip()[:80]

    if not name:
        return jsonify({"error": "Please enter your name."}), 400
    if "@" not in email or "." not in email:
        return jsonify({"error": "A valid email is required."}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    if q("SELECT 1 FROM users WHERE email = %s", [email]):
        return jsonify({"error": "An account with this email already exists."}), 409

    pw_hash = generate_password_hash(pw)
    with psycopg2.connect(PG) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, password_hash, name) VALUES (%s, %s, %s) RETURNING user_id",
            [email, pw_hash, name]
        )
        uid = cur.fetchone()[0]
    token = make_token(uid, email)
    return jsonify({"token": token, "user": {"user_id": uid, "email": email, "name": name}}), 201


@app.route("/api/login", methods=["POST"])
def login():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    pw    = data.get("password") or ""

    rows = q("SELECT user_id, email, name, password_hash FROM users WHERE email = %s", [email])
    if not rows or not check_password_hash(rows[0]["password_hash"], pw):
        return jsonify({"error": "Incorrect email or password."}), 401

    u = rows[0]
    token = make_token(u["user_id"], u["email"])
    return jsonify({"token": token, "user": {"user_id": u["user_id"], "email": u["email"], "name": u.get("name")}})


@app.route("/api/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({"user_id": user["user_id"], "email": user["email"], "name": user.get("name")})


@app.route("/api/me", methods=["PATCH"])
@require_auth
def update_me(user):
    """Update the logged-in user's display name and/or password."""
    data = request.get_json(silent=True) or {}
    new_name = data.get("name")
    new_pw   = data.get("new_password")
    cur_pw   = data.get("current_password")

    updates, params = [], []

    if new_name is not None:
        new_name = new_name.strip()[:80]
        if not new_name:
            return jsonify({"error": "Name cannot be empty."}), 400
        updates.append("name = %s"); params.append(new_name)

    if new_pw:
        if len(new_pw) < 6:
            return jsonify({"error": "New password must be at least 6 characters."}), 400
        # Require the current password to change it.
        row = q("SELECT password_hash FROM users WHERE user_id = %s", [user["user_id"]])
        if not row or not check_password_hash(row[0]["password_hash"], cur_pw or ""):
            return jsonify({"error": "Your current password is incorrect."}), 400
        updates.append("password_hash = %s"); params.append(generate_password_hash(new_pw))

    if not updates:
        return jsonify({"error": "Nothing to update."}), 400

    params.append(user["user_id"])
    with psycopg2.connect(PG) as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET " + ", ".join(updates) + " WHERE user_id = %s", params)

    fresh = q("SELECT user_id, email, name FROM users WHERE user_id = %s", [user["user_id"]])[0]
    return jsonify({"ok": True, "user": {"user_id": fresh["user_id"], "email": fresh["email"], "name": fresh.get("name")}})


@app.route("/api/ping")
def ping():
    return jsonify({"ok": True})

@app.route("/api/options")
def options():
    return jsonify({
        "towns": [r["town_name"] for r in q("SELECT town_name FROM towns ORDER BY town_name")],
        "flat_types": [r["flat_type_name"] for r in q("SELECT flat_type_name FROM flat_types ORDER BY flat_type_id")],
        "flat_models": [r["flat_model_name"] for r in q("SELECT flat_model_name FROM flat_models ORDER BY flat_model_name")],
    })


def build_search_filters(args):
    """Single source of truth for search filters, shared by /api/search and
    /api/search-stats so results and charts always agree. Returns
    (from_where_sql, params, any_filter)."""
    town       = args.get("town")
    flat_type  = args.get("flat_type")
    flat_model = args.get("flat_model")
    budget     = args.get("budget", type=int)
    min_lease  = args.get("min_lease", type=int)   # YEARS
    min_area   = args.get("min_area", type=float)
    max_area   = args.get("max_area", type=float)
    storey     = args.get("storey")                # band string "min-max", e.g. "4-6"

    fw = """
      FROM resale_transactions t
      JOIN blocks b       ON b.block_id = t.block_id
      JOIN towns tw       ON tw.town_id = b.town_id
      JOIN flat_types ft  ON ft.flat_type_id = t.flat_type_id
      JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id
      WHERE 1=1
    """
    p = []
    any_filter = False
    if town:       fw += " AND tw.town_name = %s";              p.append(town);            any_filter = True
    if flat_type:  fw += " AND ft.flat_type_name = %s";         p.append(flat_type);       any_filter = True
    if flat_model: fw += " AND fm.flat_model_name = %s";        p.append(flat_model);      any_filter = True
    if budget:     fw += " AND t.resale_price <= %s";           p.append(budget);          any_filter = True
    if min_lease:  fw += " AND t.remaining_lease_months >= %s"; p.append(min_lease*12);    any_filter = True
    if min_area:   fw += " AND t.floor_area_sqm >= %s";         p.append(min_area);        any_filter = True
    if max_area:   fw += " AND t.floor_area_sqm <= %s";         p.append(max_area);        any_filter = True
    # Storey band: "lo-hi" (e.g. "4-6") or "13-" for 13-and-above.
    if storey:
        try:
            parts = storey.split("-")
            lo = int(parts[0]) if parts[0] else None
            hi = int(parts[1]) if len(parts) > 1 and parts[1] else None
            if lo is not None:
                fw += " AND t.storey_high >= %s"; p.append(lo); any_filter = True
            if hi is not None:
                fw += " AND t.storey_low <= %s";  p.append(hi); any_filter = True
        except (ValueError, IndexError):
            pass
    return fw, p, any_filter


@app.route("/api/search")
def search():
    limit  = min(request.args.get("limit", 50, type=int), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)

    from_where, filters, any_filter = build_search_filters(request.args)

    # Total matching rows (for "showing X of Y" + knowing when to stop).
    total = q("SELECT COUNT(*) AS n " + from_where, filters)[0]["n"]

    # Ordering:
    #  - With filters: newest transactions first (recency matters once the
    #    user has narrowed things down).
    #  - Without filters: a deterministic pseudo-random spread by txn_id, so
    #    the landing view shows a VARIED mix of towns/flat types instead of
    #    whatever clusters at the newest month — while staying stable across
    #    pages so "Load more" never repeats or skips a row.
    order_by = " ORDER BY t.month DESC, t.txn_id DESC " if any_filter \
               else " ORDER BY md5(t.txn_id::text) "

    # The requested page of results.
    page_sql = ("""
      SELECT t.txn_id, t.month, tw.town_name, ft.flat_type_name,
             b.block_no, b.street_name,
             t.storey_low, t.storey_high, t.floor_area_sqm,
             fm.flat_model_name, t.remaining_lease_months,
             t.resale_price
    """ + from_where + order_by + " LIMIT %s OFFSET %s")
    rows = q(page_sql, filters + [limit, offset])

    for r in rows:
        r["resale_price"] = float(r["resale_price"])
        r["floor_area_sqm"] = float(r["floor_area_sqm"])
        r["month"] = r["month"].isoformat()

    return jsonify({
        "rows": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + len(rows)) < total,
    })


@app.route("/api/search-stats")
def search_stats():
    """Aggregate stats for the current search filters, used to draw charts and
    compute per-result value scores on the search page. Uses the SAME filter
    builder as /api/search so charts and results always match."""
    fw, p, _ = build_search_filters(request.args)

    # Overall stats + median (the median powers the value score).
    stats = q("""
      SELECT COUNT(*)::int AS n,
             ROUND(MIN(t.resale_price))::int AS min_price,
             ROUND(MAX(t.resale_price))::int AS max_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY (t.resale_price / NULLIF(t.floor_area_sqm, 0))))::int AS median_psm
    """ + fw, p)[0]

    n = stats["n"] or 0
    if n == 0:
        return jsonify({"n": 0, "median_price": None, "buckets": [], "scatter": []})

    # Price distribution: NB evenly-spaced buckets between min and max.
    lo = stats["min_price"] or 0
    hi = stats["max_price"] or 0
    NB = 12
    width = max((hi - lo) / NB, 1)
    # Compute the bucket index per row in a subquery, then group.
    bucket_rows = q("""
      SELECT LEAST(FLOOR((t.resale_price - %s) / %s), %s)::int AS bucket,
             COUNT(*)::int AS count
    """ + fw + """
      GROUP BY bucket
      ORDER BY bucket
    """, [lo, width, NB - 1] + p)

    # Turn bucket indices into labelled ranges, filling empty buckets with 0.
    counts = {r["bucket"]: r["count"] for r in bucket_rows}
    buckets = []
    for i in range(NB):
        b_lo = round(lo + i * width)
        b_hi = round(lo + (i + 1) * width)
        buckets.append({"low": b_lo, "high": b_hi, "count": counts.get(i, 0)})

    # Scatter sample: up to 400 points of (floor_area, price) for the cloud.
    scatter = q("""
      SELECT ROUND(t.floor_area_sqm)::int AS area,
             ROUND(t.resale_price)::int AS price
    """ + fw + """
      ORDER BY md5(t.txn_id::text)
      LIMIT 400
    """, p)

    return jsonify({
        "n": n,
        "min_price": stats["min_price"],
        "max_price": stats["max_price"],
        "median_price": stats["median_price"],
        "median_psm": stats["median_psm"],
        "buckets": buckets,
        "scatter": scatter,
    })

@app.route("/api/details/<int:txn_id>")
def details(txn_id):
    rows = q("""
      SELECT t.txn_id, t.month, tw.town_name, tw.region,
             ft.flat_type_name, fm.flat_model_name,
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
        return jsonify({"error": "not found"}), 404
    r = rows[0]
    r["resale_price"]   = float(r["resale_price"])
    r["floor_area_sqm"] = float(r["floor_area_sqm"])
    r["month"]          = r["month"].isoformat()
    area = r["floor_area_sqm"] or 0
    r["price_per_sqm"]  = round(r["resale_price"] / area, 2) if area else None
    return jsonify(r)


@app.route("/api/txn-value/<int:txn_id>")
def txn_value(txn_id):
    """Value context for a single transaction: its price vs the median for the
    same town + flat type (recent window), price-per-sqm, and an explainable
    value score. Powers the compare page's 'best value' recommendation."""
    base = q("""
      SELECT t.txn_id, t.resale_price, t.floor_area_sqm,
             t.remaining_lease_months, tw.town_name, ft.flat_type_name
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE t.txn_id = %s
    """, [txn_id])
    if not base:
        return jsonify({"error": "not found"}), 404
    r = base[0]
    price = float(r["resale_price"])
    area  = float(r["floor_area_sqm"] or 0)
    psm   = round(price / area, 2) if area else None

    # Median price + median price/sqm for the same town + flat type
    # (last 24 months of data). This is the benchmark the flat is scored against.
    bench = q("""
      SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY (t.resale_price / NULLIF(t.floor_area_sqm, 0))))::int AS median_psm,
             COUNT(*)::int AS n
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE tw.town_name = %s AND ft.flat_type_name = %s
        AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - INTERVAL '24 months')
    """, [r["town_name"], r["flat_type_name"]])[0]

    median_price = bench["median_price"]
    median_psm   = bench["median_psm"]

    # % difference vs the town+type median (negative = cheaper = better value)
    price_diff_pct = round((price - median_price) / median_price * 100, 1) if median_price else None
    psm_diff_pct   = round((psm - median_psm) / median_psm * 100, 1) if (median_psm and psm) else None

    # Explainable value score 0-100: 50 = exactly at median; below median and
    # more remaining lease push it up. Clamped to [0,100].
    score = 50.0
    if price_diff_pct is not None:
        score -= price_diff_pct * 1.6          # cheaper than median => higher score
    lease_years = (r["remaining_lease_months"] or 0) / 12.0
    score += max(min((lease_years - 80) * 0.4, 12), -12)   # lease bonus/penalty around 80y
    score = max(0, min(100, round(score)))

    if price_diff_pct is None:
        verdict = "No benchmark available"
    elif price_diff_pct <= -8:  verdict = "Great value"
    elif price_diff_pct < -2:   verdict = "Good value"
    elif price_diff_pct <= 2:   verdict = "Around market rate"
    elif price_diff_pct <= 8:   verdict = "Above market rate"
    else:                        verdict = "Premium"

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
        "value_score": score,
        "verdict": verdict,
        "benchmark_n": bench["n"],
    })


def _latest_data_month():
    """The most recent transaction month in the dataset.

    The data is static historical HDB data, so anchoring the 'last N months'
    windows to the newest row in the DB (rather than CURRENT_DATE) keeps the
    insights meaningful no matter how much time has passed since it was loaded.
    """
    row = q("SELECT MAX(month) AS m FROM resale_transactions")[0]
    return row["m"]  # a date, or None if empty


@app.route("/api/insights/town-medians")
def town_medians():
    flat_type = request.args.get("flat_type", "4 ROOM")
    months    = request.args.get("months", 24, type=int)
    rows = q("""
      WITH recent AS (
        SELECT t.txn_id, t.resale_price, t.floor_area_sqm, b.town_id
        FROM resale_transactions t
        JOIN blocks b      ON b.block_id = t.block_id
        JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
        WHERE ft.flat_type_name = %s
          AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - (%s || ' months')::interval)
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
      ORDER BY median_price DESC
    """, [flat_type, months])
    return jsonify({"flat_type": flat_type, "months": months, "rows": rows})


@app.route("/api/insights/trend")
def trend():
    flat_type = request.args.get("flat_type", "4 ROOM")
    town      = request.args.get("town")            # optional; UPPERCASE if given
    months    = request.args.get("months", 60, type=int)

    sql = """
      SELECT DATE_TRUNC('quarter', t.month)::date AS quarter,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             COUNT(*)::int AS txn_count
      FROM resale_transactions t
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      WHERE ft.flat_type_name = %s
        AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - (%s || ' months')::interval)
    """
    p = [flat_type, months]
    if town:
        sql += " AND tw.town_name = %s"
        p.append(town)
    sql += " GROUP BY quarter ORDER BY quarter ASC"

    rows = q(sql, p)
    for r in rows:
        r["quarter"] = r["quarter"].isoformat()
    return jsonify({"flat_type": flat_type, "town": town, "months": months, "rows": rows})


@app.route("/api/insights/summary")
def insights_summary():
    """Headline numbers for a flat type (optionally a single town) within the
    last N months of the dataset. Powers the interactive stat cards."""
    flat_type = request.args.get("flat_type", "4 ROOM")
    town      = request.args.get("town")
    months    = request.args.get("months", 24, type=int)

    where = """
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE ft.flat_type_name = %s
        AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - (%s || ' months')::interval)
    """
    p = [flat_type, months]
    if town:
        where += " AND tw.town_name = %s"
        p.append(town)

    agg = q("""
      SELECT COUNT(*)::int AS txn_count,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             ROUND(MIN(t.resale_price))::int AS min_price,
             ROUND(MAX(t.resale_price))::int AS max_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY (t.resale_price / NULLIF(t.floor_area_sqm, 0))))::int AS median_psm,
             ROUND(AVG(t.floor_area_sqm))::int AS avg_area
    """ + where, p)[0]

    # Cheapest / priciest town for this flat type (whole-market view, ignores
    # the town filter so the "where to look" hint stays useful).
    extremes = q("""
      WITH recent AS (
        SELECT t.resale_price, b.town_id
        FROM resale_transactions t
        JOIN blocks b      ON b.block_id = t.block_id
        JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
        WHERE ft.flat_type_name = %s
          AND t.month >= ((SELECT MAX(month) FROM resale_transactions) - (%s || ' months')::interval)
      ), by_town AS (
        SELECT tw.town_name,
               ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r.resale_price))::int AS median_price,
               COUNT(*)::int AS n
        FROM recent r JOIN towns tw ON tw.town_id = r.town_id
        GROUP BY tw.town_name HAVING COUNT(*) >= 5
      )
      SELECT
        (SELECT town_name FROM by_town ORDER BY median_price ASC  LIMIT 1) AS cheapest_town,
        (SELECT median_price FROM by_town ORDER BY median_price ASC LIMIT 1) AS cheapest_price,
        (SELECT town_name FROM by_town ORDER BY median_price DESC LIMIT 1) AS priciest_town,
        (SELECT median_price FROM by_town ORDER BY median_price DESC LIMIT 1) AS priciest_price
    """, [flat_type, months])[0]

    latest = _latest_data_month()
    return jsonify({
        "flat_type": flat_type,
        "town": town,
        "months": months,
        "latest_month": latest.isoformat() if latest else None,
        "summary": agg,
        "extremes": extremes,
    })


@app.route("/api/insights/by-flat-type")
def insights_by_flat_type():
    """Median resale price and median price/sqm for EACH flat type, within the
    window (and optional town). Shows how much stepping up in size costs."""
    town   = request.args.get("town")
    months = request.args.get("months", 24, type=int)

    where = """
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE t.month >= ((SELECT MAX(month) FROM resale_transactions) - (%s || ' months')::interval)
    """
    p = [months]
    if town:
        where += " AND tw.town_name = %s"
        p.append(town)

    rows = q("""
      SELECT ft.flat_type_name,
             ft.flat_type_id,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY (t.resale_price / NULLIF(t.floor_area_sqm, 0))))::int AS median_psm,
             COUNT(*)::int AS txn_count
    """ + where + """
      GROUP BY ft.flat_type_name, ft.flat_type_id
      HAVING COUNT(*) >= 5
      ORDER BY ft.flat_type_id
    """, p)
    return jsonify({"town": town, "months": months, "rows": rows})

@app.route("/api/bookmarks", methods=["GET"])
@require_auth
def list_bookmarks(user):
    rows = q("""
      SELECT b.txn_id, b.notes, b.created_at,
             t.month, tw.town_name, ft.flat_type_name, fm.flat_model_name,
             bl.block_no, bl.street_name,
             t.storey_low, t.storey_high, t.floor_area_sqm,
             t.remaining_lease_months, t.resale_price
      FROM bookmarks b
      JOIN resale_transactions t ON t.txn_id = b.txn_id
      JOIN blocks bl      ON bl.block_id = t.block_id
      JOIN towns tw       ON tw.town_id = bl.town_id
      JOIN flat_types ft  ON ft.flat_type_id = t.flat_type_id
      JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id
      WHERE b.user_id = %s
      ORDER BY b.created_at DESC
    """, [user["user_id"]])
    for r in rows:
        r["resale_price"]   = float(r["resale_price"])
        r["floor_area_sqm"] = float(r["floor_area_sqm"])
        r["month"]          = r["month"].isoformat()
        r["created_at"]     = r["created_at"].isoformat()
    return jsonify(rows)


@app.route("/api/bookmarks", methods=["POST"])
@require_auth
def add_bookmark(user):
    data    = request.get_json(silent=True) or {}
    txn_id  = int(data.get("txn_id", 0))
    notes   = data.get("notes")
    if not txn_id:
        return jsonify({"error": "txn_id required"}), 400
    with psycopg2.connect(PG) as conn, conn.cursor() as cur:
        cur.execute("""
          INSERT INTO bookmarks (user_id, txn_id, notes)
          VALUES (%s, %s, %s)
          ON CONFLICT (user_id, txn_id) DO UPDATE SET notes = EXCLUDED.notes
        """, [user["user_id"], txn_id, notes])
    return jsonify({"ok": True, "txn_id": txn_id})


@app.route("/api/bookmarks/<int:txn_id>", methods=["DELETE"])
@require_auth
def remove_bookmark(txn_id, user):
    with psycopg2.connect(PG) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM bookmarks WHERE user_id = %s AND txn_id = %s",
                    [user["user_id"], txn_id])
    return jsonify({"ok": True})

@app.route("/api/reviews", methods=["GET"])
def list_reviews():
    town = request.args.get("town")
    limit = min(request.args.get("limit", 50, type=int), 200)
    filt = {}
    if town:
        filt["town"] = town.upper()
    rows = list(db.reviews.find(filt, {"_id": 0}).sort("created_at", -1).limit(limit))
    for r in rows:
        if hasattr(r.get("created_at"), "isoformat"):
            r["created_at"] = r["created_at"].isoformat()
    return jsonify(rows)


@app.route("/api/reviews", methods=["POST"])
def add_review():
    data = request.get_json(silent=True) or {}
    town     = (data.get("town") or "").strip().upper()
    rating   = int(data.get("rating") or 0)
    comment  = (data.get("comment") or "").strip()
    tags     = data.get("tags") or []
    author   = (data.get("author") or "Anonymous").strip()

    if not town or not (1 <= rating <= 5) or not comment:
        return jsonify({"error": "town, rating (1-5), and comment are required"}), 400

    from datetime import datetime
    doc = {
        "town": town,
        "rating": rating,
        "comment": comment,
        "tags": tags,
        "author": author,
        "created_at": datetime.utcnow(),
    }
    db.reviews.insert_one(doc)
    return jsonify({"ok": True}), 201

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
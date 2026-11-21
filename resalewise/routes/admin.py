"""API for admin.html. Every route needs an admin token.

    /api/admin/overview      what is in the two databases, and how to open a terminal on each
    /api/admin/sql           read-only SQL console
    /api/admin/mongo         read-only MongoDB aggregation console
    /api/admin/cube          the sales as an OLAP cube
    /api/admin/indexes       one query with and without its index
    /api/admin/concurrency   two transactions on real database sessions (concurrency.py)

None of it changes the site's data: the consoles only read, and the rest only writes to its
own sample table (concurrency_accounts).
"""
import json
import time

import psycopg2
from bson import json_util
from flask import Blueprint, current_app, jsonify
from pymongo.errors import OperationFailure

from .. import admin, db, concurrency
from ..auth import require_admin
from ..web import ApiError, cached, json_body, rate_limit

bp = Blueprint("admin", __name__)


def _pg_message(e):
    """The database's own error message, which is what the person at the console needs."""
    diag = getattr(e, "diag", None)
    text = getattr(diag, "message_primary", None) or str(e).strip().splitlines()[0]
    if isinstance(e, psycopg2.errors.QueryCanceled):
        return "That query ran past the time limit and was stopped."
    if isinstance(e, psycopg2.errors.InsufficientPrivilege) and "users" in text:
        return text + ". The console may read users(user_id, email, name, created_at, is_admin), never the password hashes."
    if isinstance(e, psycopg2.errors.ReadOnlySqlTransaction):
        return "The console is read-only: " + text
    hint = getattr(diag, "message_hint", None)
    return text + (". " + hint if hint else "")


def _enter_sandbox(cur):
    """Switch to the read-only role for the rest of this transaction. Returns which guard is in force."""
    cur.execute("SAVEPOINT sandbox")
    try:
        cur.execute(f"SET LOCAL ROLE {admin.READONLY_ROLE}")
        return "role"
    except psycopg2.Error:
        cur.execute("ROLLBACK TO SAVEPOINT sandbox")       # no such role here, the other guards still apply
        return "basic"


# overview
@bp.route("/api/admin/overview")
@require_admin
def overview(user):
    tables = db.q("""
        SELECT c.relname AS name,
               CASE c.relkind WHEN 'm' THEN 'materialized view' ELSE 'table' END AS kind,
               GREATEST(c.reltuples, 0)::bigint AS rows,
               pg_total_relation_size(c.oid) AS bytes,
               (SELECT COUNT(*) FROM pg_index i WHERE i.indrelid = c.oid)::int AS indexes
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'm') AND c.relname <> 'spatial_ref_sys'
        ORDER BY pg_total_relation_size(c.oid) DESC""")
    version = db.q("SHOW server_version")[0]["server_version"]
    # only true if this login can actually switch to the read-only role
    sandbox = bool(db.q("SELECT 1 FROM pg_roles WHERE rolname = %s AND pg_has_role(current_user, oid, 'MEMBER')",
                        [admin.READONLY_ROLE]))
    collections, mongo_version = [], None
    try:
        m = db.mongo()
        mongo_version = m.client.server_info().get("version")
        for name in sorted(m.list_collection_names()):
            collections.append({"name": name, "docs": m[name].estimated_document_count(),
                                "indexes": len(m[name].index_information())})
    except Exception as e:
        current_app.logger.warning("admin overview: mongo not read: %s", e)
    return jsonify({"postgres": {"version": version, "tables": tables, "readonly_role": sandbox},
                    "mongo": {"version": mongo_version, "collections": collections},
                    "admin": {"email": user["email"], "name": user.get("name")},
                    "terminal": admin.terminal_commands()})


# SQL console
@bp.route("/api/admin/sql", methods=["POST"])
@rate_limit(40)
@require_admin
def run_sql(user):
    sql = admin.check_sql(json_body().get("sql"))
    is_query = sql.split(None, 1)[0].lower() in ("select", "with", "values", "table")
    with db.pg_conn() as conn, conn.cursor() as cur:
        try:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = 8000")
            sandbox = _enter_sandbox(cur)
            if sandbox == "basic":
                admin.check_basic_mode(sql)
            plan = None
            if is_query:
                cur.execute("EXPLAIN " + sql)
                plan = [r[0] for r in cur.fetchall()]
            t0 = time.perf_counter()
            # the database applies the LIMIT, so a million-row query only sends back 200
            cur.execute(f"SELECT * FROM (\n{sql}\n) AS console_result LIMIT {admin.MAX_ROWS + 1}" if is_query else sql)
            rows = cur.fetchall()
            ms = (time.perf_counter() - t0) * 1000
            columns = [d.name for d in cur.description]
        except psycopg2.Error as e:
            raise ApiError(_pg_message(e), 400, "sql_error")
    truncated = len(rows) > admin.MAX_ROWS
    rows = [[admin.jsonable(v) for v in r] for r in rows[:admin.MAX_ROWS]]
    return jsonify({"columns": columns, "rows": rows, "truncated": truncated, "ms": round(ms, 1),
                    "plan": plan, "sandbox": sandbox})


# MongoDB console
@bp.route("/api/admin/mongo", methods=["POST"])
@rate_limit(40)
@require_admin
def run_mongo(user):
    data = json_body()
    name = str(data.get("collection") or "")
    m = db.mongo()
    if name not in m.list_collection_names():
        raise ApiError("Choose one of the collections.", 400, "validation_error")
    pipeline = admin.check_pipeline(data.get("pipeline"))
    as_text = json.dumps(pipeline)
    pipeline = json_util.loads(as_text)                      # turns {"$date": ...} into real dates
    t0 = time.perf_counter()
    try:
        docs = list(m[name].aggregate(pipeline + [{"$limit": admin.MAX_ROWS + 1}], maxTimeMS=8000))
    except OperationFailure as e:
        raise ApiError(str(e).split(", full error")[0], 400, "pipeline_error")
    ms = (time.perf_counter() - t0) * 1000
    truncated = len(docs) > admin.MAX_ROWS
    return jsonify({"docs": [admin.jsonable(d) for d in docs[:admin.MAX_ROWS]], "truncated": truncated, "ms": round(ms, 1)})


# OLAP cube
@bp.route("/api/admin/cube/meta")
@require_admin
def cube_meta(user):
    return jsonify({
        "dims": [{"key": k, "label": v[0], "numeric": v[2] is int} for k, v in admin.CUBE_DIMS.items()],
        "hierarchies": admin.CUBE_HIERARCHIES,
        "measures": [{"key": k, "label": v[0]} for k, v in admin.CUBE_MEASURES.items()],
    })


@bp.route("/api/admin/cube", methods=["POST"])
@rate_limit(60)
@require_admin
def cube(user):
    data = json_body()
    dims, measure = data.get("dims"), str(data.get("measure") or "")
    totals, filters = str(data.get("totals") or "rollup"), data.get("filters") or {}
    if not isinstance(filters, dict):
        raise ApiError("Filters must be an object.", 400, "validation_error")
    sql, params = admin.cube_sql(dims, measure, totals, filters)

    def load():
        with db.pg_conn() as conn, conn.cursor() as cur:
            try:
                cur.execute("SET LOCAL statement_timeout = 25000")
                shown = cur.mogrify(sql, params).decode("utf-8")
                t0 = time.perf_counter()
                cur.execute(sql, params)
                rows = cur.fetchmany(admin.CUBE_MAX_CELLS + 1)
                ms = (time.perf_counter() - t0) * 1000
            except psycopg2.Error as e:
                raise ApiError(_pg_message(e), 400, "sql_error")
        if len(rows) > admin.CUBE_MAX_CELLS:
            raise ApiError("That view has too many cells to show. Slice it first (for example, pick one town before "
                           "drilling down to streets).", 400, "too_many_cells")
        return {"sql": shown, "ms": round(ms, 1), "rows": [[admin.jsonable(v) for v in r] for r in rows]}

    key = "admin-cube:" + json.dumps([dims, measure, totals, filters], sort_keys=True)
    out = dict(cached(key, 900, load))
    out.update({"dims": [{"key": d, "label": admin.CUBE_DIMS[d][0]} for d in dims],
                "measure": {"key": measure, "label": admin.CUBE_MEASURES[measure][0]}, "totals": totals})
    return jsonify(out)


# index analysis
INDEX_CASES = {
    "point": {
        "label": "One sale by its id", "kind": "Equality search",
        "sql": "SELECT * FROM resale_transactions WHERE txn_id = 500000",
        "takeaway": "An equality search on a key walks the B+ tree from the root to one leaf: a handful of pages, "
                  "however large the table is. Without the index every page of the table is read to find one row."},
    "range": {
        "label": "Sales in three months", "kind": "Range search",
        "sql": "SELECT COUNT(*), ROUND(AVG(resale_price)) FROM resale_transactions "
               "WHERE month BETWEEN DATE '2024-01-01' AND DATE '2024-03-01'",
        "takeaway": "A B+ tree keeps its leaves in key order and linked together, so a range is one walk down the tree "
                  "and then a scan along the leaves. (A hash index could not do this: it only answers equality.)"},
    "rare": {
        "label": "Flats sold for $1.5m or more", "kind": "A rare value",
        "sql": "SELECT COUNT(*), ROUND(AVG(floor_area_sqm)) FROM resale_transactions WHERE resale_price >= 1500000",
        "takeaway": "Very few sales match, so the index on resale_price pays off: the database jumps to the top end of "
                  "the tree and fetches just those rows. Compare this with the next case, which uses the same column."},
    "common": {
        "label": "Flats sold for $300k or more", "kind": "A common value",
        "sql": "SELECT COUNT(*), ROUND(AVG(floor_area_sqm)) FROM resale_transactions WHERE resale_price >= 300000",
        "takeaway": "The same column and the same index, but about half of all sales match. Fetching half the table "
                  "through an index would cost more than reading it straight through, so the planner ignores the "
                  "index on its own. An index is not free speed, which is why we do not index every column."},
    "newest": {
        "label": "The 20 newest sales", "kind": "ORDER BY … LIMIT",
        "sql": "SELECT txn_id, month, resale_price FROM resale_transactions ORDER BY month DESC, txn_id DESC LIMIT 20",
        "takeaway": "The index is already in the order asked for, so the database reads its first 20 entries and stops. "
                  "Without it, all 985,219 rows are read and sorted to keep 20."},
    "all": {
        "label": "Every sale, no WHERE", "kind": "Full read",
        "sql": "SELECT COUNT(*), ROUND(AVG(resale_price)) FROM resale_transactions",
        "takeaway": "Every row is needed, so there is nothing for an index to skip: both runs read the whole table. "
                  "An index helps a query find few rows among many, not all of them."},
}
_INDEX_SWITCHES = ("enable_indexscan", "enable_indexonlyscan", "enable_bitmapscan")


def _plan_summary(plan):
    """Pick out the parts of the EXPLAIN JSON that the page shows."""
    nodes, indexes = [], []

    def walk(node, depth):
        label = node["Node Type"]
        if node.get("Index Name"):
            label += " using " + node["Index Name"]
            if node["Index Name"] not in indexes:
                indexes.append(node["Index Name"])
        elif node.get("Relation Name"):
            label += " on " + node["Relation Name"]
        nodes.append({"depth": depth, "node": label, "rows": node.get("Actual Rows"),
                      "loops": node.get("Actual Loops"), "ms": round(node.get("Actual Total Time") or 0, 3)})
        for child in node.get("Plans") or []:
            walk(child, depth + 1)
    root = plan["Plan"]
    walk(root, 0)
    return {"ms": round(plan.get("Execution Time") or 0, 3), "planning_ms": round(plan.get("Planning Time") or 0, 3),
            "pages": (root.get("Shared Hit Blocks") or 0) + (root.get("Shared Read Blocks") or 0),
            "nodes": nodes, "indexes": indexes}


def _explain(cur, sql, use_indexes):
    cur.execute("SAVEPOINT probe")
    if not use_indexes:
        for switch in _INDEX_SWITCHES:                     # tell the planner to act as if there were no index
            cur.execute(f"SET LOCAL {switch} = off")
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
    plan = cur.fetchone()[0]
    cur.execute("ROLLBACK TO SAVEPOINT probe")               # puts the planner switches back
    return _plan_summary((json.loads(plan) if isinstance(plan, str) else plan)[0])


def _index_facts(cur, name):
    cur.execute("""
        SELECT c.relpages, GREATEST(c.reltuples, 0)::bigint, pg_relation_size(c.oid), pg_get_indexdef(c.oid), am.amname
        FROM pg_class c JOIN pg_am am ON am.oid = c.relam
        WHERE c.relname = %s AND c.relkind = 'i'""", [name])
    row = cur.fetchone()
    if not row:
        return None
    facts = {"name": name, "pages": row[0], "entries": row[1], "bytes": row[2], "definition": row[3],
             "method": row[4], "height": None}
    cur.execute("SAVEPOINT facts")
    try:                                                   # real tree height, if pageinspect is installed
        cur.execute("SELECT level FROM bt_metap(%s)", [name])
        facts["height"] = cur.fetchone()[0] + 1
    except psycopg2.Error:
        cur.execute("ROLLBACK TO SAVEPOINT facts")
    return facts


@bp.route("/api/admin/indexes")
@require_admin
def index_cases(user):
    return jsonify([{"key": k, "label": c["label"], "kind": c["kind"], "sql": c["sql"]} for k, c in INDEX_CASES.items()])


@bp.route("/api/admin/indexes", methods=["POST"])
@rate_limit(20)
@require_admin
def index_analysis(user):
    key = str(json_body().get("case") or "")
    case = INDEX_CASES.get(key)
    if not case:
        raise ApiError("Unknown case.", 400, "validation_error")
    with db.pg_conn() as conn, conn.cursor() as cur:
        try:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = 20000")
            cur.execute(case["sql"])                       # untimed warm-up run so both timed runs find the pages in memory
            cur.fetchall()
            with_index = _explain(cur, case["sql"], True)
            without = _explain(cur, case["sql"], False)
            facts = [f for f in (_index_facts(cur, name) for name in with_index["indexes"]) if f]
        except psycopg2.Error as e:
            raise ApiError(_pg_message(e), 400, "sql_error")
    return jsonify({"case": key, "label": case["label"], "kind": case["kind"], "sql": case["sql"],
                    "takeaway": case["takeaway"], "with_index": with_index, "without_index": without, "indexes": facts})


# concurrency
@bp.route("/api/admin/concurrency")
@require_admin
def txn_scenarios(user):
    return jsonify(concurrency.catalogue())


@bp.route("/api/admin/concurrency", methods=["POST"])
@rate_limit(20)
@require_admin
def txn_run(user):
    out = concurrency.run(str(json_body().get("scenario") or ""))
    return jsonify(out)

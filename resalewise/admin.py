"""Rules for the admin pages, kept out of the routes so they can be tested without a database.

The admin logs in on the normal login page as `admin`. The password is ADMIN_PASSWORD from the
environment, or the demo one below when that isn't set, so set it on any public deployment.
"""
import datetime as dt
import decimal
import json
import os
import re
from urllib.parse import urlparse

from werkzeug.security import generate_password_hash

from . import config, db
from .web import ApiError

ADMIN_USERNAME = "admin"
ADMIN_EMAIL = "admin@resalewise.local"          # users.email must look like an email (chk_users_email)
_DEMO_PASSWORD = "admintest"


def admin_password():
    return os.environ.get("ADMIN_PASSWORD", "").strip() or _DEMO_PASSWORD


def admin_enabled():
    """On Render the demo password is never used: without ADMIN_PASSWORD there is no admin account."""
    return bool(os.environ.get("ADMIN_PASSWORD", "").strip()) or not os.environ.get("RENDER")


def login_identifier(raw):
    """The email to look up for what was typed in the login box (`admin` means the admin account)."""
    ident = str(raw or "").strip().lower()[:254]
    return ADMIN_EMAIL if ident == ADMIN_USERNAME else ident


# set-up
def ensure_admin(cur):
    """Make the admin tables, the admin account and the two sample accounts if they are missing.
    The demo password only goes on a new account. If ADMIN_PASSWORD is set, the admin password
    is updated to match it on every start. On Render with no ADMIN_PASSWORD no admin is made."""
    with open(os.path.join(config.ROOT, "sql", "admin.sql"), encoding="utf-8") as fh:
        cur.execute(fh.read())
    if admin_enabled():
        cur.execute(
            "INSERT INTO users (email, password_hash, name, is_admin) VALUES (%s, %s, %s, TRUE) "
            "ON CONFLICT (email) DO NOTHING",
            [ADMIN_EMAIL, generate_password_hash(admin_password()), "Administrator"])
    if os.environ.get("ADMIN_PASSWORD", "").strip():
        cur.execute("UPDATE users SET password_hash = %s WHERE email = %s AND is_admin",
                    [generate_password_hash(admin_password()), ADMIN_EMAIL])
    cur.execute("INSERT INTO concurrency_accounts (name, balance) VALUES ('Alice', 200), ('Bob', 200) "
                "ON CONFLICT (name) DO NOTHING")


# guards
MAX_ROWS = 200
READONLY_ROLE = "resalewise_readonly"

_SQL_START = re.compile(r"^\s*(select|with|explain|values|table|show)\b", re.I)
# functions that read files, run other SQL, change settings or touch other sessions.
# The read-only transaction and role do the real work, this list just closes gaps
_SQL_DENY = re.compile(
    r"\b(password_hash|set_config|\w+_to_xml\w*|dblink\w*|lo_\w+|pg_read\w*|pg_ls_\w+|pg_stat_file"
    r"|pg_sleep\w*|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_advisory\w*)\b", re.I)


def check_sql(sql):
    """Give back the statement if it is one read-only query, otherwise raise ApiError with the reason."""
    sql = str(sql or "").strip().rstrip(";").strip()
    if not sql:
        raise ApiError("Type a query first.", 400, "validation_error")
    if len(sql) > 4000:
        raise ApiError("That query is too long (4,000 characters at most).", 400, "validation_error")
    if ";" in sql:
        raise ApiError("One statement at a time: remove the semicolon.", 400, "sql_not_allowed")
    if not _SQL_START.match(sql):
        raise ApiError("The console is read-only: start with SELECT, WITH, VALUES, TABLE, EXPLAIN or SHOW.",
                       400, "sql_not_allowed")
    hit = _SQL_DENY.search(sql)
    if hit:
        raise ApiError(f"“{hit.group(1)}” is not available in the console.", 400, "sql_not_allowed")
    return sql


# stages that write ($out, $merge), run JavaScript, or look at other sessions
_MONGO_DENY = {"$out", "$merge", "$function", "$accumulator", "$where", "$currentOp", "$listSessions",
               "$listLocalSessions", "$planCacheStats"}


def check_pipeline(pipeline):
    """A MongoDB aggregation pipeline that only reads."""
    if isinstance(pipeline, str):
        try:
            pipeline = json.loads(pipeline)
        except ValueError as e:
            raise ApiError(f"The pipeline is not valid JSON: {e}", 400, "validation_error")
    if not isinstance(pipeline, list) or not pipeline or not all(isinstance(s, dict) and len(s) == 1 for s in pipeline):
        raise ApiError('A pipeline is a JSON list of stages, e.g. [{"$group": {...}}].', 400, "validation_error")
    if len(pipeline) > 12:
        raise ApiError("At most 12 stages.", 400, "validation_error")

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _MONGO_DENY:
                    raise ApiError(f"The stage or operator {key} is not available in the console.",
                                   400, "pipeline_not_allowed")
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(pipeline)
    return pipeline


POSTGRES_CONTAINER = "resalewise-postgres"        # container_name in docker-compose.yml
MONGO_CONTAINER = "resalewise-mongo"
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def terminal_commands():
    """Commands that open psql and mongosh, so the admin page's steps can be repeated by hand.
    Local databases go through docker exec. Cloud ones get the plain client command with no
    password in it (the client asks for that)."""
    pg, mongo = urlparse(config.database_url()), urlparse(config.mongo_uri())
    local_pg, local_mongo = pg.hostname in _LOCAL_HOSTS, mongo.hostname in _LOCAL_HOSTS
    pg_db = (pg.path or "/").lstrip("/") or "postgres"
    if local_pg:
        psql = f"docker exec -it {POSTGRES_CONTAINER} psql -U {pg.username or 'postgres'} -d {pg_db}"
    else:
        port = ":" + str(pg.port) if pg.port else ""
        query = "?" + pg.query if pg.query else ""
        psql = f'psql "postgresql://{pg.username}@{pg.hostname}{port}/{pg_db}{query}"'
    if local_mongo:
        mongosh = f"docker exec -it {MONGO_CONTAINER} mongosh {config.mongo_db_name()}"
    else:
        user = mongo.username + "@" if mongo.username else ""
        mongosh = f'mongosh "{mongo.scheme}://{user}{mongo.hostname}/{config.mongo_db_name()}"'
    return {"postgres": psql, "mongo": mongosh, "local": local_pg and local_mongo}


_USERS_TABLE = re.compile(r"\busers\b", re.I)


def check_basic_mode(sql):
    """Without the read-only role (a cloud login that can't create roles) the database doesn't hide
    the users table, which holds password hashes. So there, any statement that mentions it is refused."""
    if _USERS_TABLE.search(sql):
        raise ApiError("On this server the console cannot read the users table, because it holds password hashes. "
                       "The read-only role that would allow a safe view of it could not be set up here.",
                       400, "sql_not_allowed")


def jsonable(v):
    """Turn a database value into something JSON and a table cell can take."""
    if v is None or isinstance(v, (bool, int, str)):
        return v if not isinstance(v, str) or len(v) <= 300 else v[:300] + "…"
    if isinstance(v, float):
        return v if v == v and abs(v) != float("inf") else None
    if isinstance(v, decimal.Decimal):
        return (int(v) if v == v.to_integral_value() else float(v)) if v.is_finite() else None
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, memoryview)):
        return "\\x" + bytes(v)[:24].hex() + ("…" if len(v) > 24 else "")
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return str(v)[:300]


# the OLAP cube
# fact table is resale_transactions, v_transactions is it joined to towns, blocks, flat types etc.
# All the SQL comes from the lists below. Only filter values come from the request, as parameters.
CUBE_DIMS = {
    "decade":     ("Decade",      "(sale_year / 10 * 10)", int),
    "year":       ("Year",        "sale_year", int),
    "quarter":    ("Quarter",     "to_char(quarter, 'YYYY \"Q\"Q')", str),
    "town":       ("Town",        "town_name", str),
    "street":     ("Street",      "street_name", str),
    "flat_type":  ("Flat type",   "flat_type_name", str),
    "flat_model": ("Flat model",  "flat_model_name", str),
    "storey":     ("Storey band", "CASE WHEN storey_mid <= 5 THEN '01 to 05' WHEN storey_mid <= 10 THEN '06 to 10' "
                                  "WHEN storey_mid <= 20 THEN '11 to 20' ELSE '21 and up' END", str),
}
# coarse to fine: drill down goes right, roll up goes left
CUBE_HIERARCHIES = {"time": ["decade", "year", "quarter"], "place": ["town", "street"],
                    "flat": ["flat_type", "flat_model"], "height": ["storey"]}
CUBE_MEASURES = {
    "sales":        ("Number of sales",   "COUNT(*)"),
    "median_price": ("Median price",      "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY resale_price)"),
    "avg_price":    ("Average price",     "ROUND(AVG(resale_price))"),
    "avg_psm":      ("Average $ per sqm", "ROUND(AVG(price_per_sqm))"),
    "max_price":    ("Highest price",     "MAX(resale_price)"),
    "total_value":  ("Total value sold",  "SUM(resale_price)"),
}
CUBE_TOTALS = {"none": "{0}", "rollup": "ROLLUP ({0})", "cube": "CUBE ({0})"}
CUBE_MAX_CELLS = 4000


def cube_sql(dims, measure, totals="rollup", filters=None):
    """Build the cube query and return (sql, params). Anything not in the lists raises ApiError."""
    dims = list(dims or [])
    if not 1 <= len(dims) <= 2 or len(set(dims)) != len(dims) or any(d not in CUBE_DIMS for d in dims):
        raise ApiError("Choose one or two different dimensions.", 400, "validation_error")
    if measure not in CUBE_MEASURES:
        raise ApiError("Unknown measure.", 400, "validation_error")
    if totals not in CUBE_TOTALS:
        raise ApiError("Unknown kind of subtotal.", 400, "validation_error")

    where, params = [], []
    for key, value in (filters or {}).items():
        if key not in CUBE_DIMS:
            raise ApiError(f"Unknown dimension “{key}”.", 400, "validation_error")
        _, expr, cast = CUBE_DIMS[key]
        values = value if isinstance(value, list) else [value]
        if not values or len(values) > 40:
            raise ApiError("A filter needs between 1 and 40 values.", 400, "validation_error")
        try:
            values = [cast(v) if cast is int else str(v)[:80] for v in values]
        except (TypeError, ValueError):
            raise ApiError(f"“{CUBE_DIMS[key][0]}” takes whole numbers.", 400, "validation_error")
        if len(values) == 1:                       # slice: one value
            where.append(f"{expr} = %s")
            params.append(values[0])
        else:                                      # dice: several values
            where.append(f"{expr} = ANY(%s)")
            params.append(values)

    exprs = [CUBE_DIMS[d][1] for d in dims]
    select = [f'{e} AS "{d}"' for d, e in zip(dims, exprs)]
    select.append(f"{CUBE_MEASURES[measure][1]} AS value")
    select.append("COUNT(*) AS sales")
    select.append(f"GROUPING({', '.join(exprs)}) AS level" if totals != "none" else "0 AS level")
    sql = ("SELECT " + ",\n       ".join(select) +
           "\nFROM v_transactions" +
           ("\nWHERE " + "\n  AND ".join(where) if where else "") +
           "\nGROUP BY " + CUBE_TOTALS[totals].format(", ".join(exprs)) +
           "\nORDER BY level, " + ", ".join(str(i + 1) for i in range(len(dims))))
    return sql, params

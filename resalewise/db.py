"""Database access for both engines.

PostgreSQL: a small pool of warm psycopg2 connections (one TCP+TLS handshake
to a remote database costs hundreds of milliseconds, so reusing connections
keeps pages fast). `q()` runs a parameterised query and returns dict rows.

MongoDB: the client is created lazily on first use. Creating it at import
time made every import (including the test suite) depend on DNS reaching the
Atlas cluster.

Routes always call `db.q(...)` / `db.mongo()` through this module, so tests
can swap in fakes with `mock.patch.object(db, "q", ...)`.
"""
import threading
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from pymongo import MongoClient

from . import config

# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
_POOL_MAX = config.env_int("PG_POOL_MAX", 10)
_pool = None
_pool_lock = threading.Lock()
_pool_slots = threading.BoundedSemaphore(_POOL_MAX)
_last_used = {}            # id(conn) -> time it last went back to the pool
_IDLE_PROBE_SECS = 20      # re-check a connection that sat idle this long


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg2 import pool as pgpool
                _pool = pgpool.ThreadedConnectionPool(
                    1, _POOL_MAX, config.database_url(), connect_timeout=10,
                    keepalives=1, keepalives_idle=30,
                    keepalives_interval=10, keepalives_count=3)
    return _pool


@contextmanager
def pg_conn():
    """Borrow a pooled Postgres connection: commit on success, roll back on
    error, and always hand it back (closing it if it broke)."""
    if not _pool_slots.acquire(timeout=15):
        raise psycopg2.OperationalError("database connection pool is busy")
    pool = None
    conn = None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        # Idle connections can be dropped by the server; probe those (only) and
        # swap in a fresh one so the request doesn't fail.
        if conn.closed or time.time() - _last_used.get(id(conn), 0) > _IDLE_PROBE_SECS:
            try:
                if conn.closed:
                    raise psycopg2.OperationalError("closed")
                with conn.cursor() as c:
                    c.execute("SELECT 1")
                conn.rollback()
            except psycopg2.Error:
                _last_used.pop(id(conn), None)
                pool.putconn(conn, close=True)
                conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    finally:
        if conn is not None:
            broken = bool(conn.closed)
            if broken:
                _last_used.pop(id(conn), None)
            else:
                _last_used[id(conn)] = time.time()
            try:
                pool.putconn(conn, close=broken)
            except Exception:
                pass
        _pool_slots.release()


def q(sql, params=None):
    """Run one parameterised query; return a list of dict rows."""
    with pg_conn() as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or [])
        return cur.fetchall() if cur.description else []


def execute(sql, params=None):
    """Run one write statement; return (rowcount, first row or None)."""
    with pg_conn() as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or [])
        row = cur.fetchone() if cur.description else None
        return cur.rowcount, row


_feature_cache = {}


def has_relation(name):
    """True if a table / view / materialized view exists (cached for 60 s).
    Lets optional features (geo, drivers, engines) degrade gracefully before
    their loaders have been run."""
    hit = _feature_cache.get(name)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    rows = q("SELECT to_regclass(%s) IS NOT NULL AS ok", [name])
    ok = bool(rows and rows[0].get("ok"))
    _feature_cache[name] = (time.monotonic(), ok)
    return ok


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
_mongo_client = None
_mongo_lock = threading.Lock()


def mongo_client():
    global _mongo_client
    if _mongo_client is None:
        with _mongo_lock:
            if _mongo_client is None:
                _mongo_client = MongoClient(config.mongo_uri(), serverSelectionTimeoutMS=5000,
                                            appname="resalewise-sg")
    return _mongo_client


def mongo():
    """The application's Mongo database (default name: hdb)."""
    return mongo_client()[config.mongo_db_name()]

"""HTTP plumbing shared by every blueprint: the JSON error contract, rate
limiting, a tiny TTL cache, input validation and response headers."""
import functools
import os
import threading
import time
from collections import deque
from urllib.parse import urlencode

import psycopg2
from flask import Response, jsonify, make_response, request
from pymongo.errors import PyMongoError
from werkzeug.exceptions import HTTPException

from . import config

# ---------------------------------------------------------------------------
# Errors — every failure from /api/* is JSON shaped {"error": "...", "code": "..."}
# ---------------------------------------------------------------------------
# `error` is a human-readable sentence the UI can show as-is; `code` is a
# stable machine-readable string (invalid_parameter, not_found, rate_limited,
# db_unavailable, ...) the front end can branch on.
class ApiError(Exception):
    def __init__(self, message, status=400, code="bad_request", headers=None, **extra):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code
        self.headers, self.extra = headers or {}, extra


def error_response(message, status, code, headers=None, **extra):
    resp = jsonify({"error": message, "code": code, **extra})
    resp.status_code = status
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


_STATUS_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 413: "payload_too_large",
    415: "unsupported_media_type", 429: "rate_limited",
}


def _wants_json():
    return request.path.startswith("/api/") or request.path == "/api"


def register_error_handlers(app):
    @app.errorhandler(ApiError)
    def _handle_api_error(e):
        return error_response(e.message, e.status, e.code, e.headers, **e.extra)

    @app.errorhandler(HTTPException)
    def _handle_http_error(e):
        if _wants_json():
            headers = {k: v for k, v in e.get_response().headers if k in ("Allow", "Retry-After")}
            return error_response(e.description or e.name, e.code,
                                  _STATUS_CODES.get(e.code, "http_error"), headers)
        page = os.path.join(config.ROOT, "404.html")
        if e.code == 404 and os.path.isfile(page):
            # Served from the site root here, so a <base> makes the page's relative
            # asset links work even when the missing URL is nested (/a/b/c).
            with open(page, encoding="utf-8") as fh:
                html = fh.read().replace("<head>", '<head>\n    <base href="/">', 1)
            resp = make_response(html, 404)
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            return resp
        return e

    @app.errorhandler(psycopg2.IntegrityError)
    def _handle_integrity(e):
        # A constraint said no (duplicate key, CHECK, foreign key). Report it as
        # a conflict instead of a 500; the constraint name helps debugging.
        name = getattr(getattr(e, "diag", None), "constraint_name", None)
        app.logger.warning("Integrity error (%s): %s", name, e)
        return error_response("That change conflicts with existing data.", 409, "conflict",
                              constraint=name)

    @app.errorhandler(psycopg2.OperationalError)
    @app.errorhandler(PyMongoError)
    def _handle_db_down(e):
        app.logger.error("Database unavailable: %s", e)
        return error_response("The database is temporarily unavailable. Please try again in a moment.",
                              503, "db_unavailable")

    @app.errorhandler(config.ConfigError)
    def _handle_config(e):
        app.logger.error("Configuration error: %s", e)
        return error_response("The server is not configured yet (see USER_MANUAL.md).",
                              503, "not_configured")

    @app.errorhandler(Exception)
    def _handle_unexpected(e):
        app.logger.exception("Unhandled error on %s %s", request.method, request.path)
        if _wants_json():
            return error_response("Something went wrong on our side. Please try again.", 500, "internal_error")
        return "Internal Server Error", 500


# ---------------------------------------------------------------------------
# Rate limiting — small in-memory sliding window per client IP
# ---------------------------------------------------------------------------
# Plenty for normal browsing (RATE_LIMIT_PER_MIN, default 300 API calls/min);
# write and export endpoints carry tighter per-route limits via @rate_limit.
# State lives in this process, which is fine for a single worker. Behind
# several workers each one counts separately — swap in Redis if that matters.
class RateLimiter:
    WINDOW = 60.0

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def hit(self, bucket, per_minute):
        """Record one request. Returns 0 if allowed, else seconds until retry."""
        now = time.monotonic()
        key = (bucket, request.remote_addr or "unknown")
        with self._lock:
            if len(self._hits) > 20000:                       # keep memory bounded
                self._hits = {k: d for k, d in self._hits.items()
                              if d and now - d[-1] < self.WINDOW}
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] >= self.WINDOW:
                dq.popleft()
            if len(dq) >= per_minute:
                return max(1, int(self.WINDOW - (now - dq[0])) + 1)
            dq.append(now)
            return 0


limiter = RateLimiter()
DEFAULT_RATE_LIMIT = config.env_int("RATE_LIMIT_PER_MIN", 300)
SLOW_MS = config.env_int("SLOW_REQUEST_MS", 400)


def too_many(wait):
    return ApiError("Too many requests. Please wait a moment and try again.", 429, "rate_limited",
                    headers={"Retry-After": str(wait)}, retry_after=wait)


def rate_limit(per_minute, bucket=None):
    """Decorator: cap a single route at `per_minute` requests per client IP."""
    def deco(fn):
        name = bucket or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = limiter.hit(name, per_minute)
            if wait:
                raise too_many(wait)
            return fn(*args, **kwargs)
        return wrapper
    return deco


def register_request_hooks(app):
    @app.before_request
    def _start_timer():
        request._rw_t0 = time.perf_counter()

    @app.before_request
    def _default_rate_limit():
        if request.method == "OPTIONS" or not request.path.startswith("/api/") \
                or request.path == "/api/health":
            return
        wait = limiter.hit("default", DEFAULT_RATE_LIMIT)   # module attribute: patchable in tests
        if wait:
            raise too_many(wait)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        t0 = getattr(request, "_rw_t0", None)
        if t0 is not None and request.path.startswith("/api/"):
            ms = (time.perf_counter() - t0) * 1000
            resp.headers["Server-Timing"] = "app;dur=%.0f" % ms   # visible in browser dev tools
            if ms >= SLOW_MS:
                print("[slow] %s %s took %.0f ms" % (request.method, request.full_path.rstrip("?"), ms), flush=True)
        return resp


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------
def arg_int(args, name, default=None, lo=None, hi=None):
    raw = args.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(str(raw).strip())
    except ValueError:
        raise ApiError(f"'{name}' must be a whole number.", 400, "invalid_parameter")
    if (lo is not None and val < lo) or (hi is not None and val > hi):
        raise ApiError(f"'{name}' must be between {lo} and {hi}.", 400, "invalid_parameter")
    return val


def arg_float(args, name, default=None, lo=None, hi=None):
    raw = args.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(str(raw).strip())
    except ValueError:
        raise ApiError(f"'{name}' must be a number.", 400, "invalid_parameter")
    if val != val or (lo is not None and val < lo) or (hi is not None and val > hi):  # val != val: NaN
        raise ApiError(f"'{name}' must be between {lo} and {hi}.", 400, "invalid_parameter")
    return val


def arg_str(args, name, default=None, max_len=64):
    raw = args.get(name)
    if raw is None or raw.strip() == "":
        return default
    raw = raw.strip()
    if len(raw) > max_len:
        raise ApiError(f"'{name}' is too long (max {max_len} characters).", 400, "invalid_parameter")
    return raw


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Request body must be a JSON object.", 400, "invalid_json")
    return data


# ---------------------------------------------------------------------------
# A tiny TTL cache (the HDB dataset is static between loads)
# ---------------------------------------------------------------------------
_cache, _cache_lock = {}, threading.Lock()
_CACHE_MAX = 800


def cache_get(key, ttl_seconds):
    """Return (found, value) from the tiny TTL cache."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl_seconds:
            return True, hit[1]
    return False, None


def cache_put(key, value):
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:          # keep memory bounded: drop the oldest quarter
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:_CACHE_MAX // 4]:
                _cache.pop(k, None)
        _cache[key] = (time.monotonic(), value)


def cache_clear():
    with _cache_lock:
        _cache.clear()


def cached(key, ttl_seconds, loader):
    """Memoise `loader()` for values that barely change (the dataset is static)."""
    found, value = cache_get(key, ttl_seconds)
    if found:
        return value
    value = loader()
    cache_put(key, value)
    return value


def cache_json(ttl_seconds):
    """Cache a GET endpoint's JSON by its full URL (path + query string).
    Only for endpoints that are a pure function of the query and the (static)
    HDB dataset: search, search-stats and the insights aggregates. Saves
    re-running million-row sorts and percentile queries for repeat requests."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Same question, same key: "4+ROOM" and "4%20ROOM", or the same
            # parameters in another order, must not be cached twice.
            key = "http:" + request.path + "?" + urlencode(sorted(request.args.items(multi=True)))
            found, body = cache_get(key, ttl_seconds)
            if found:
                resp = Response(body, mimetype="application/json")
            else:
                resp = fn(*args, **kwargs)
                if getattr(resp, "status_code", None) != 200 or getattr(resp, "direct_passthrough", False):
                    return resp
                cache_put(key, resp.get_data())
            # Let the browser reuse it too (repeat visits / Back button skip the network).
            resp.headers["Cache-Control"] = "public, max-age=%d" % min(ttl_seconds, 300)
            return resp
        return wrapper
    return deco


def iso(v):
    """Dates/datetimes -> ISO strings (naive datetimes are UTC, marked with Z)."""
    if hasattr(v, "isoformat"):
        s = v.isoformat()
        if hasattr(v, "tzinfo") and getattr(v, "hour", None) is not None and v.tzinfo is None:
            s += "Z"
        return s
    return v

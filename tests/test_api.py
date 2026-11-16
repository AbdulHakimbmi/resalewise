"""API tests — run with:  python -m pytest tests  (or: python -m unittest discover -s tests -v)

These never touch the real databases: Postgres access (`resalewise.db.q`) and
the MongoDB database (`resalewise.db.mongo`) are replaced with in-memory
fakes, so they run anywhere the packages from requirements.txt are installed,
with no network and no .env file.
"""
import contextlib
import logging
import os
import sys
import unittest
from datetime import date
from unittest import mock

os.environ["RW_SKIP_STARTUP_MIGRATIONS"] = "1"          # don't migrate any database on import
# Dummy settings: nothing connects to these (all database access is faked).
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:1")
os.environ.setdefault("TOKEN_SECRET", "unit-test-secret-not-for-production")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2          # noqa: E402
import app as appmod     # noqa: E402
from resalewise import benchmarks, web          # noqa: E402
from resalewise import db as dbm                # noqa: E402

logging.disable(logging.CRITICAL)                        # keep expected-error tracebacks out of the output

TOWNS = [{"town_name": "TAMPINES"}, {"town_name": "BEDOK"}, {"town_name": "YISHUN"}]


class FakeReviews:
    """Just enough of a pymongo collection for the review endpoints, including
    a small interpreter for the $match / $group / $sort stages we use."""

    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def create_index(self, *a, **k):
        pass

    def insert_one(self, doc):
        self.docs.append(dict(doc))
        return mock.Mock(inserted_id=None)

    def update_many(self, filt, update):
        n = 0
        for d in self.docs:
            owner = filt.get("user_id")
            hit = (d.get("user_id") in owner["$in"]) if isinstance(owner, dict) else all(d.get(k) == v for k, v in filt.items())
            if hit:
                d.update(update.get("$set", {}))
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                n += 1
        return mock.Mock(modified_count=n)

    def find(self, filt, proj=None):
        rows = [d for d in self.docs
                if all(d.get(k) == v for k, v in filt.items() if not k.startswith("$"))]

        class Cursor(list):
            def sort(self, key, direction=None):
                if isinstance(key, list):                      # [(field, direction), ...]
                    out = list(self)
                    for field, d in reversed(key):
                        out.sort(key=lambda doc: doc.get(field, 0), reverse=d < 0)
                    return Cursor(out)
                return Cursor(sorted(self, key=lambda d: d[key], reverse=direction < 0))

            def skip(self, n):
                return Cursor(self[n:])

            def limit(self, n):
                return Cursor(self[:n])
        return Cursor(rows)

    def count_documents(self, filt):
        return len(self.find(filt))

    def aggregate(self, pipeline):
        rows = list(self.docs)
        for stage in pipeline:
            (op, spec), = stage.items()
            if op == "$match":
                rows = [d for d in rows if all(d.get(k) == v for k, v in spec.items())]
            elif op == "$group":
                groups = {}
                for d in rows:
                    key = None if spec["_id"] is None else d[spec["_id"].lstrip("$")]
                    groups.setdefault(key, []).append(d)
                rows = []
                for key, ds in groups.items():
                    out = {"_id": key}
                    for field, acc in spec.items():
                        if field == "_id":
                            continue
                        (aop, aarg), = acc.items()
                        if aop == "$sum":
                            out[field] = sum(aarg for _ in ds)
                        elif aop == "$avg":
                            vals = [d[aarg.lstrip("$")] for d in ds]
                            out[field] = sum(vals) / len(vals)
                    rows.append(out)
            elif op == "$sort":
                for key, direction in reversed(list(spec.items())):
                    rows.sort(key=lambda d: d[key], reverse=direction < 0)
        return rows


def fake_q_factory(extra=None):
    """A stand-in for app.q that answers by recognising the SQL text."""
    calls = []

    def fake_q(sql, params=None):
        calls.append((" ".join(sql.split()), list(params or [])))
        if extra:
            got = extra(sql, params)
            if got is not None:
                return got
        if "SELECT town_name FROM towns" in sql:
            return TOWNS
        if "COUNT(*) AS n" in sql:
            return [{"n": 1}]
        if "SELECT 1" in sql:
            return [{"?column?": 1}]
        return []
    fake_q.calls = calls
    return fake_q


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        web.limiter._hits.clear()
        web._cache.clear()
        dbm._feature_cache.clear()
        self.reviews = FakeReviews()
        self.mongo_db = mock.Mock(reviews=self.reviews)
        self.db_patch = mock.patch.object(dbm, "mongo", lambda: self.mongo_db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.client = appmod.app.test_client()

    def patch_q(self, extra=None):
        fake = fake_q_factory(extra)
        p = mock.patch.object(dbm, "q", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def assertError(self, resp, status, code):
        self.assertEqual(resp.status_code, status, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(set(body) >= {"error", "code"}, True, body)
        self.assertEqual(body["code"], code, body)
        self.assertTrue(body["error"] and isinstance(body["error"], str))
        return body


class ErrorContractTests(ApiTestCase):
    def test_unknown_api_route_is_json_404(self):
        self.assertError(self.client.get("/api/nope"), 404, "not_found")

    def test_wrong_method_is_json_405(self):
        self.assertError(self.client.delete("/api/search"), 405, "method_not_allowed")

    def test_unexpected_exception_is_json_500_without_leaking_details(self):
        with mock.patch.object(dbm, "q", side_effect=RuntimeError("secret internal detail")):
            resp = self.client.get("/api/options")
        body = self.assertError(resp, 500, "internal_error")
        self.assertNotIn("secret", body["error"])

    def test_database_down_is_503(self):
        with mock.patch.object(dbm, "q", side_effect=psycopg2.OperationalError("connection refused")):
            self.assertError(self.client.get("/api/options"), 503, "db_unavailable")

    def test_auth_required_is_401(self):
        self.patch_q()
        self.assertError(self.client.get("/api/bookmarks"), 401, "unauthorized")

    def test_bad_json_body_is_400(self):
        resp = self.client.post("/api/login", data="not json", content_type="application/json")
        self.assertError(resp, 400, "invalid_json")


class SearchValidationTests(ApiTestCase):
    def test_rejects_bad_values(self):
        self.patch_q()
        for qs in ("budget=abc", "budget=-5", "min_lease=500", "min_area=big", "limit=0",
                   "limit=5000", "offset=-1", "storey=abc", "min_area=90&max_area=70"):
            with self.subTest(qs=qs):
                self.assertError(self.client.get("/api/search?" + qs), 400, "invalid_parameter")

    def test_error_names_the_bad_parameter(self):
        self.patch_q()
        body = self.assertError(self.client.get("/api/search?budget=abc"), 400, "invalid_parameter")
        self.assertIn("budget", body["error"])

    def test_valid_filters_are_parameterised_in_order(self):
        fake = self.patch_q()
        resp = self.client.get("/api/search?town=TAMPINES&flat_type=4+ROOM&budget=600000&storey=4-6&limit=10")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(set(data), {"rows", "total", "offset", "limit", "page", "pages", "window", "capped", "has_more"})
        page_sql, page_params = fake.calls[-1]
        self.assertEqual(page_params, ["TAMPINES", "4 ROOM", 600000, 4, 6, 10, 0])
        self.assertNotIn("600000", page_sql)           # values are bound, never interpolated

    def paged_fake(self, total):
        """A fake database holding `total` matching sales: answers the count and
        returns only as many rows as the requested LIMIT/OFFSET leaves."""
        calls = []

        def fake_q(sql, params=None):
            calls.append((sql, list(params or [])))
            if "COUNT(*) AS n" in sql:
                return [{"n": total}]
            if "LIMIT %s OFFSET %s" in sql:
                limit, offset = params[-2], params[-1]
                n = max(0, min(limit, total - offset))
                return [{"txn_id": offset + i, "month": date(2026, 8, 1), "town_name": "BEDOK",
                         "flat_type_name": "4 ROOM", "block_no": "1", "street_name": "X ST",
                         "storey_low": 1, "storey_high": 3, "floor_area_sqm": 90,
                         "flat_model_name": "MODEL A", "remaining_lease_months": 900,
                         "resale_price": 500000} for i in range(n)]
            return []
        fake_q.calls = calls
        return fake_q

    def test_page_number_becomes_limit_and_offset(self):
        fake = self.paged_fake(250)
        with mock.patch.object(dbm, "q", fake):
            data = self.client.get("/api/search?page=3&limit=20").get_json()
        self.assertEqual((data["page"], data["pages"], data["total"], data["offset"]), (3, 13, 250, 40))
        self.assertEqual(len(data["rows"]), 20)
        page_call = next(c for c in fake.calls if "LIMIT %s OFFSET %s" in c[0])
        self.assertEqual(page_call[1][-2:], [20, 40])                  # LIMIT 20 OFFSET 40
        self.assertFalse(data["capped"])
        self.assertTrue(data["has_more"])

    def test_last_page_is_short_and_a_page_past_the_end_shows_the_last_page(self):
        with mock.patch.object(dbm, "q", self.paged_fake(250)):
            last = self.client.get("/api/search?page=13&limit=20").get_json()
            beyond = self.client.get("/api/search?page=99&limit=20").get_json()
        self.assertEqual(len(last["rows"]), 10)
        self.assertFalse(last["has_more"])
        self.assertEqual((beyond["page"], len(beyond["rows"])), (13, 10))

    def test_deep_results_are_capped_at_the_window(self):
        with mock.patch.object(dbm, "q", self.paged_fake(985_219)):
            data = self.client.get("/api/search?page=500&limit=20").get_json()
        self.assertEqual((data["window"], data["pages"], data["capped"]), (10_000, 500, True))
        self.assertEqual((data["page"], len(data["rows"]), data["has_more"]), (500, 20, False))

    def test_no_matches_gives_one_empty_page(self):
        with mock.patch.object(dbm, "q", self.paged_fake(0)):
            data = self.client.get("/api/search?page=4").get_json()
        self.assertEqual((data["rows"], data["total"], data["pages"], data["page"]), ([], 0, 1, 1))

    def test_unfiltered_count_skips_joins_but_filtered_count_keeps_the_ones_it_needs(self):
        fake = self.paged_fake(5)
        with mock.patch.object(dbm, "q", fake):
            self.client.get("/api/search")
            web._cache.clear()
            self.client.get("/api/search?town=BEDOK")
        unfiltered_count = next(sql for sql, _ in fake.calls if "COUNT(*) AS n" in sql)
        self.assertNotIn("JOIN", unfiltered_count)
        filtered_count = [sql for sql, _ in fake.calls if "COUNT(*) AS n" in sql][-1]
        self.assertIn("JOIN towns", filtered_count)
        self.assertNotIn("JOIN flat_types", filtered_count)

    def test_search_is_always_newest_first(self):
        fake = self.paged_fake(50)
        with mock.patch.object(dbm, "q", fake):
            self.client.get("/api/search")
        page_sql = next(sql for sql, _ in fake.calls if "LIMIT %s OFFSET %s" in sql)
        self.assertIn("ORDER BY t.month DESC, t.txn_id DESC", page_sql)
        self.assertNotIn("md5", page_sql)

    def test_insights_months_is_bounded(self):
        self.patch_q()
        self.assertError(self.client.get("/api/insights/trend?months=0"), 400, "invalid_parameter")
        self.assertError(self.client.get("/api/insights/summary?months=9999"), 400, "invalid_parameter")


class FakePg:
    """A stand-in for db.pg_conn(): records the statements and whether the
    transaction was committed or rolled back."""

    def __init__(self):
        self.statements, self.committed, self.rolled_back = [], False, False

    @contextlib.contextmanager
    def conn(self):
        pg = self

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None): pg.statements.append(" ".join(sql.split()))
            def fetchone(self): return (True,)

        class Conn:
            def cursor(self): return Cur()
        try:
            yield Conn()
            pg.committed = True
        except Exception:
            pg.rolled_back = True
            raise


class AccountDeleteTests(ApiTestCase):
    """Deleting an account needs the password and removes it in one transaction."""

    def setUp(self):
        super().setUp()
        from werkzeug.security import generate_password_hash
        from resalewise.auth import make_token
        self.headers = {"Authorization": "Bearer " + make_token(7, "a@b.c")}
        pw_hash = generate_password_hash("secret-pw-1")

        def fake_q(sql, params=None):
            if "password_hash FROM users" in sql:
                return [{"password_hash": pw_hash}]
            if "FROM users WHERE user_id" in sql:
                return [{"user_id": 7, "email": "a@b.c", "name": "Wei Ming", "created_at": None}]
            return []
        self.pg = FakePg()
        for target, value in ((dbm, "q"), (dbm, "pg_conn")):
            p = mock.patch.object(target, value, fake_q if value == "q" else self.pg.conn)
            p.start()
            self.addCleanup(p.stop)

    def delete(self, password="secret-pw-1"):
        return self.client.delete("/api/me", json={"password": password}, headers=self.headers)

    def test_delete_removes_the_account(self):
        resp = self.delete()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(self.pg.committed and not self.pg.rolled_back)
        self.assertTrue(any(sql.startswith("DELETE FROM users") for sql in self.pg.statements))

    def test_wrong_password_changes_nothing_anywhere(self):
        self.assertError(self.delete("wrong"), 400, "invalid_credentials")
        self.assertEqual(self.pg.statements, [])


class RateLimitTests(ApiTestCase):
    def test_default_limit_returns_429_with_retry_after(self):
        self.patch_q()
        with mock.patch.object(web, "DEFAULT_RATE_LIMIT", 5):
            codes = [self.client.get("/api/options").status_code for _ in range(7)]
            resp = self.client.get("/api/options")
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])
        body = self.assertError(resp, 429, "rate_limited")
        self.assertGreaterEqual(int(resp.headers["Retry-After"]), 1)
        self.assertEqual(body["retry_after"], int(resp.headers["Retry-After"]))

    def test_login_is_limited_much_more_tightly(self):
        self.patch_q()
        codes = [self.client.post("/api/login", json={"email": "a@b.co", "password": "x"}).status_code
                 for _ in range(12)]
        self.assertEqual(codes.count(401), 10)
        self.assertEqual(codes.count(429), 2)

    def test_health_is_never_rate_limited(self):
        self.patch_q()
        with mock.patch.object(web, "DEFAULT_RATE_LIMIT", 1):
            self.assertEqual({self.client.get("/api/health").status_code for _ in range(5)}, {200})

    def test_limits_are_per_client(self):
        self.patch_q()
        with mock.patch.object(web, "DEFAULT_RATE_LIMIT", 2):
            for _ in range(3):
                self.client.get("/api/options", environ_overrides={"REMOTE_ADDR": "10.0.0.1"})
            other = self.client.get("/api/options", environ_overrides={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(other.status_code, 200)


class OperationsTests(ApiTestCase):
    def test_health_ok_and_degraded(self):
        self.patch_q()
        ok = self.client.get("/api/health")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["checks"]["postgres"], "ok")
        self.assertEqual(ok.headers["Cache-Control"], "no-store")

        with mock.patch.object(dbm, "q", side_effect=psycopg2.OperationalError("down")):
            bad = self.client.get("/api/health")
        self.assertEqual(bad.status_code, 503)
        self.assertEqual(bad.get_json()["status"], "degraded")

    def test_meta_reports_latest_month_and_is_cached(self):
        def extra(sql, params):
            if "MAX(month)" in sql:
                return [{"m": date(2026, 9, 1)}]
            if "MIN(month)" in sql:
                return [{"m": date(1990, 1, 1)}]
            if "COUNT(*)::bigint" in sql:
                return [{"n": 985219}]
        fake = self.patch_q(extra)
        first = self.client.get("/api/meta").get_json()
        self.assertEqual(first["earliest_month"], "1990-01-01")
        self.assertEqual(first["latest_month"], "2026-09-01")
        self.assertEqual(first["record_count"], 985219)
        n_calls = len(fake.calls)
        self.client.get("/api/meta")
        self.assertEqual(len(fake.calls), n_calls, "second request should be served from cache")

    def test_csv_export(self):
        row = {"txn_id": 1, "month": date(2026, 9, 1), "town_name": "TAMPINES", "flat_type_name": "4 ROOM",
               "block_no": "123", "street_name": "=HYPERLINK(\"evil\")", "storey_low": 4, "storey_high": 6,
               "floor_area_sqm": 92, "flat_model_name": "Improved", "remaining_lease_months": 900,
               "resale_price": 600000}
        fake = self.patch_q(lambda sql, p: [row] if "ORDER BY t.month DESC" in sql else None)
        resp = self.client.get("/api/search.csv?town=TAMPINES")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.mimetype == "text/csv")
        self.assertRegex(resp.headers["Content-Disposition"], r'attachment; filename="resalewise-search-\d{8}\.csv"')
        lines = resp.get_data(as_text=True).strip().splitlines()
        self.assertEqual(lines[0], "txn_id,month,town,flat_type,block,street,storey_low,storey_high,"
                                   "floor_area_sqm,flat_model,remaining_lease_months,resale_price")
        self.assertIn("2026-09-01", lines[1])
        self.assertIn("'=HYPERLINK", lines[1], "formula cells must be neutralised")
        self.assertEqual(fake.calls[-1][1], ["TAMPINES", 5000])

    def test_csv_validates_filters_like_search(self):
        self.patch_q()
        self.assertError(self.client.get("/api/search.csv?budget=abc"), 400, "invalid_parameter")

    def test_sparklines(self):
        rows = [{"town_name": "TAMPINES", "flat_type_name": "4 ROOM", "quarter": date(2026, 1, 1), "median_price": 600000},
                {"town_name": "TAMPINES", "flat_type_name": "4 ROOM", "quarter": date(2026, 4, 1), "median_price": 610000}]
        fake = self.patch_q(lambda sql, p: rows if "DATE_TRUNC('quarter'" in sql else None)

        data = self.client.get("/api/insights/sparklines?pair=tampines|4 room&pair=TAMPINES|4 ROOM").get_json()
        self.assertEqual(list(data["series"]), ["TAMPINES|4 ROOM"])
        self.assertEqual([p["median_price"] for p in data["series"]["TAMPINES|4 ROOM"]], [600000, 610000])

        n = len(fake.calls)
        again = self.client.get("/api/insights/sparklines?pair=TAMPINES|4 ROOM").get_json()
        self.assertEqual(again, data)
        self.assertEqual(len(fake.calls), n, "cached pair must not hit the database again")

    def test_sparklines_validation(self):
        self.patch_q()
        self.assertError(self.client.get("/api/insights/sparklines"), 400, "invalid_parameter")
        self.assertError(self.client.get("/api/insights/sparklines?pair=TAMPINES"), 400, "invalid_parameter")
        many = "&".join("pair=T%d|4 ROOM" % i for i in range(41))
        self.assertError(self.client.get("/api/insights/sparklines?" + many), 400, "invalid_parameter")


class SiteServingTests(ApiTestCase):
    def status(self, path):
        resp = self.client.get(path)
        resp.close()                                  # send_from_directory keeps the file open
        return resp.status_code

    def test_pages_and_assets_are_served(self):
        for path in ("/", "/search.html", "/about.html", "/assets/css/style.css"):
            with self.subTest(path=path):
                self.assertEqual(self.status(path), 200)

    def test_source_files_are_never_served(self):
        for path in ("/app.py", "/load.py", "/scripts/load.py", "/requirements.txt", "/.git/config",
                     "/assets/../app.py", "/assets/%2e%2e/app.py", "/tests/test_api.py"):
            with self.subTest(path=path):
                self.assertEqual(self.status(path), 404)

    def test_unknown_page_gets_the_styled_404_page(self):
        resp = self.client.get("/definitely-missing.html")
        html = resp.get_data(as_text=True)
        resp.close()
        self.assertEqual(resp.status_code, 404)
        self.assertIn("text/html", resp.content_type)
        self.assertIn("Page not found", html)


if __name__ == "__main__":
    unittest.main()


class CacheTests(ApiTestCase):
    def test_repeat_search_hits_cache_not_database(self):
        calls = []

        def fake_q(sql, params=None):
            calls.append(sql)
            return [{"n": 0}] if "COUNT(*)" in sql else []
        with mock.patch.object(dbm, "q", side_effect=fake_q):
            first = self.client.get("/api/search?town=BEDOK&limit=5")
            n_first = len(calls)
            second = self.client.get("/api/search?town=BEDOK&limit=5")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.get_json(), second.get_json())
            self.assertEqual(len(calls), n_first, "second identical request must not query the DB")
            self.client.get("/api/search?town=TAMPINES&limit=5")
            self.assertGreater(len(calls), n_first, "a different query must not reuse the cache")

    def test_same_query_in_another_spelling_shares_one_cache_entry(self):
        calls = []

        def fake_q(sql, params=None):
            calls.append(sql)
            return [{"n": 0}] if "COUNT(*)" in sql else []
        with mock.patch.object(dbm, "q", side_effect=fake_q):
            self.client.get("/api/search?town=BEDOK&flat_type=4%20ROOM&limit=5")
            n_first = len(calls)
            self.client.get("/api/search?limit=5&flat_type=4+ROOM&town=BEDOK")
            self.assertEqual(len(calls), n_first, "'+' vs '%20' or another order must hit the same entry")

    def test_errors_are_not_cached(self):
        with mock.patch.object(dbm, "q", side_effect=psycopg2.OperationalError("down")):
            self.assertEqual(self.client.get("/api/search?town=BEDOK").status_code, 503)
        with mock.patch.object(dbm, "q", side_effect=lambda s, p=None: [{"n": 0}] if "COUNT(*)" in s else []):
            self.assertEqual(self.client.get("/api/search?town=BEDOK").status_code, 200)


class PrecomputedViewTests(ApiTestCase):
    """Insights read small materialized views only while they match the data."""

    @staticmethod
    def reads_view(sql):
        return "FROM mv_window_stats" in sql and "anchor_month" not in sql   # not the freshness check itself

    def fake(self, anchor, latest):
        from datetime import date

        def extra(sql, params):
            if "to_regclass" in sql:
                return [{"ok": True}]
            if "MAX(anchor_month)" in sql:
                return [{"a": anchor, "m": latest}]
            if "FROM mv_window_stats" in sql:
                return [{"town_name": "BEDOK", "median_price": 500000, "median_psm": 5000, "txn_count": 50}]
            if "SELECT MAX(month) AS m" in sql:
                return [{"m": date(2026, 9, 1)}]
            return None
        return self.patch_q(extra)

    def test_current_views_are_used(self):
        from datetime import date
        fake = self.fake(date(2026, 9, 1), date(2026, 9, 1))
        r = self.client.get("/api/insights/town-medians?flat_type=4%20ROOM&months=24")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["rows"][0]["town_name"], "BEDOK")
        self.assertTrue(any(self.reads_view(sql) for sql, _ in fake.calls))
        self.assertFalse(any("PERCENTILE_CONT" in sql for sql, _ in fake.calls))

    def test_stale_views_fall_back_to_live_queries(self):
        from datetime import date
        fake = self.fake(date(2026, 8, 1), date(2026, 9, 1))      # data reloaded, views not refreshed
        self.assertEqual(self.client.get("/api/insights/town-medians?flat_type=4%20ROOM&months=24").status_code, 200)
        self.assertFalse(any(self.reads_view(sql) for sql, _ in fake.calls))
        self.assertTrue(any("PERCENTILE_CONT" in sql for sql, _ in fake.calls))

    def test_windows_without_a_view_use_live_queries(self):
        from datetime import date
        fake = self.fake(date(2026, 9, 1), date(2026, 9, 1))
        self.assertEqual(self.client.get("/api/insights/town-medians?flat_type=4%20ROOM&months=18").status_code, 200)
        self.assertFalse(any(self.reads_view(sql) for sql, _ in fake.calls))


class PeriodBenchmarkTests(ApiTestCase):
    """A 1995 sale must be judged against 1995 prices, not today's."""

    def fake_q(self, sql, params=None):
        self.calls.append(sql)
        if "COUNT(*) AS n" in sql and "DATE_TRUNC" not in sql:
            return [{"n": 2}]
        if "'year'" in sql:                                   # level 2: same calendar year
            return [{"town_name": "BEDOK", "flat_type_name": "4 ROOM", "period": date(1995, 1, 1),
                     "median_price": 150000, "median_psm": 2000, "median_lease_months": 1100, "n": 50}]
        if "NULL::text" in sql:                               # level 3: all towns
            return []
        if "DATE_TRUNC('quarter'" in sql:                     # level 1: same quarter
            return [{"town_name": "TAMPINES", "flat_type_name": "4 ROOM", "period": date(2026, 7, 1),
                     "median_price": 600000, "median_psm": 6500, "median_lease_months": 800, "n": 20},
                    {"town_name": "BEDOK", "flat_type_name": "4 ROOM", "period": date(1995, 4, 1),
                     "median_price": 140000, "median_psm": 1900, "median_lease_months": 1100, "n": 2}]
        if "WHERE t.txn_id" in sql:
            return [{"txn_id": 7, "month": date(1995, 5, 1), "resale_price": 150000, "floor_area_sqm": 90,
                     "remaining_lease_months": 1120, "town_name": "BEDOK", "flat_type_name": "4 ROOM"}]
        return [self.row(1, date(1995, 5, 1), "BEDOK", 150000), self.row(2, date(2026, 8, 1), "TAMPINES", 600000)]

    @staticmethod
    def row(i, month, town, price):
        return {"txn_id": i, "month": month, "town_name": town, "flat_type_name": "4 ROOM", "block_no": "1",
                "street_name": "X ST", "storey_low": 1, "storey_high": 3, "floor_area_sqm": 90,
                "flat_model_name": "Model A", "remaining_lease_months": 900, "resale_price": price}

    def setUp(self):
        super().setUp()
        self.calls = []

    def test_search_rows_are_judged_against_their_own_period(self):
        with mock.patch.object(dbm, "q", side_effect=self.fake_q):
            rows = self.client.get("/api/search?limit=5").get_json()["rows"]
        old, new = rows
        # Both sit at their own period's median -> neither is a "bargain"/"premium",
        # even though 150k vs 600k would have made the 1995 flat look 75% cheaper.
        self.assertEqual(old["verdict"], "Around market rate")
        self.assertEqual(new["verdict"], "Around market rate")
        self.assertEqual(old["benchmark_label"], "Bedok 4 Room, 1995")        # thin quarter -> widened to the year
        self.assertEqual(new["benchmark_label"], "Tampines 4 Room, Q3 2026")
        self.assertEqual(old["benchmark_median"], 150000)
        self.assertEqual(new["price_diff_pct"], 0.0)

    def test_unfiltered_cheapness_is_relative_to_period_not_absolute(self):
        # Same absolute price, different eras: only the one below ITS era's median is "good".
        self.assertEqual(benchmarks.verdict_for(-10.0), "Great value")
        self.assertEqual(benchmarks.verdict_for(0.0), "Around market rate")
        self.assertEqual(benchmarks.verdict_for(None), "No comparison available")

    def test_txn_value_uses_sale_period_and_peer_lease(self):
        with mock.patch.object(dbm, "q", side_effect=self.fake_q):
            v = self.client.get("/api/txn-value/7").get_json()
        self.assertEqual(v["median_price"], 150000)
        self.assertEqual(v["benchmark_label"], "Bedok 4 Room, 1995")
        self.assertEqual(v["verdict"], "Around market rate")
        # lease 93.3y vs peers 91.7y -> tiny bonus, so the score stays near 50 (NOT inflated to ~100)
        self.assertTrue(48 <= v["value_score"] <= 53, v["value_score"])

    def test_no_comparable_sales_gives_no_verdict(self):
        def empty_q(sql, params=None):
            if "COUNT(*) AS n" in sql and "DATE_TRUNC" not in sql:
                return [{"n": 1}]
            if "DATE_TRUNC" in sql:
                return []
            return [self.row(1, date(1995, 5, 1), "BEDOK", 150000)]
        with mock.patch.object(dbm, "q", side_effect=empty_q):
            r = self.client.get("/api/search?limit=5").get_json()["rows"][0]
        self.assertIsNone(r["price_diff_pct"])
        self.assertEqual(r["verdict"], "No comparison available")


class NormalisationTests(ApiTestCase):
    """HDB spells some values several ways; the site must treat them as one."""

    def test_helpers(self):
        self.assertEqual(benchmarks.norm_flat_type("MULTI-GENERATION"), "MULTI GENERATION")
        self.assertEqual(benchmarks.norm_flat_type("Multi Generation"), "MULTI GENERATION")
        self.assertEqual(benchmarks.norm_flat_model("Improved-Maisonette"), "IMPROVED MAISONETTE")
        self.assertIsNone(benchmarks.norm_flat_type(None))

    def test_search_filters_use_normalised_values_and_sql(self):
        seen = []

        def fake_q(sql, params=None):
            seen.append((sql, list(params or [])))
            return [{"n": 0}] if "COUNT(*) AS n" in sql else []
        with mock.patch.object(dbm, "q", side_effect=fake_q):
            r = self.client.get("/api/search?flat_type=multi-generation&flat_model=Model-A")
        self.assertEqual(r.status_code, 200)
        sql, params = seen[0]
        self.assertIn("MULTI GENERATION", params)
        self.assertIn("MODEL A", params)
        self.assertIn("UPPER(REPLACE(ft.flat_type_name", sql)   # matches BOTH spellings in the data
        self.assertIn("UPPER(REPLACE(fm.flat_model_name", sql)

    def test_options_query_deduplicates(self):
        sqls = []

        def fake_q(sql, params=None):
            sqls.append(sql)
            return []
        with mock.patch.object(dbm, "q", side_effect=fake_q):
            self.client.get("/api/options")
        joined = " ".join(sqls)
        self.assertIn("UPPER(REPLACE(flat_type_name", joined)
        self.assertIn("UPPER(REPLACE(flat_model_name", joined)
        self.assertIn("GROUP BY 1", joined)

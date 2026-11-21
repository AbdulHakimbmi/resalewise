"""Tests for the admin page: the console guards, the cube SQL and who can call the routes.
No network or database needed, the routes run against fakes.

    python -m unittest discover -s tests
"""
import logging
import os
import sys
import unittest
from unittest import mock

os.environ["RW_SKIP_STARTUP_MIGRATIONS"] = "1"
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:1")
os.environ.setdefault("TOKEN_SECRET", "unit-test-secret-not-for-production")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod                       # noqa: E402
from resalewise import admin, auth         # noqa: E402
from resalewise import db as dbm           # noqa: E402
from resalewise.web import ApiError        # noqa: E402

logging.disable(logging.CRITICAL)


class SqlConsoleGuard(unittest.TestCase):
    def refused(self, sql):
        with self.assertRaises(ApiError, msg=sql) as ctx:
            admin.check_sql(sql)
        self.assertEqual(ctx.exception.status, 400)
        return ctx.exception

    def test_reads_pass_and_a_trailing_semicolon_is_dropped(self):
        for sql in ("SELECT 1", "  select town_name, COUNT(*) FROM v_transactions GROUP BY 1;  ",
                    "WITH x AS (SELECT 1) SELECT * FROM x", "EXPLAIN ANALYZE SELECT 1", "SHOW server_version", "VALUES (1)"):
            self.assertEqual(admin.check_sql(sql), sql.strip().rstrip(";").strip())

    def test_anything_that_is_not_a_read_is_refused(self):
        for sql in ("DELETE FROM users", "UPDATE concurrency_accounts SET balance = 0", "DROP TABLE towns", "INSERT INTO towns VALUES (1)",
                    "TRUNCATE users", "COPY users TO PROGRAM 'x'", "CREATE ROLE evil", "GRANT ALL ON users TO public", "CALL f()", ""):
            self.refused(sql)

    def test_one_statement_only(self):
        self.refused("SELECT 1; DROP TABLE users")
        self.refused("SELECT 1; SELECT 2")

    def test_password_hashes_and_dangerous_functions_are_named_and_refused(self):
        for sql in ("SELECT password_hash FROM users", "select PASSWORD_HASH from users",
                    "SELECT pg_sleep(60)", "SELECT pg_read_file('/etc/passwd')", "SELECT lo_import('/etc/passwd')",
                    "SELECT set_config('role', 'postgres', false)", "SELECT query_to_xml('select 1', true, true, '')",
                    "SELECT pg_terminate_backend(1)", "SELECT * FROM dblink('x', 'y') AS t(a int)"):
            self.refused(sql)

    def test_a_very_long_query_is_refused(self):
        self.refused("SELECT " + "1," * 3000 + "1")


class MongoConsoleGuard(unittest.TestCase):
    def test_reads_pass_as_a_list_or_as_json_text(self):
        self.assertEqual(admin.check_pipeline([{"$match": {"a": 1}}, {"$group": {"_id": "$t"}}]), [{"$match": {"a": 1}}, {"$group": {"_id": "$t"}}])
        self.assertEqual(admin.check_pipeline('[{"$limit": 3}]'), [{"$limit": 3}])

    def test_writing_and_code_stages_are_refused_even_when_nested(self):
        for stage in ({"$out": "x"}, {"$merge": {"into": "x"}}, {"$match": {"$where": "sleep(5000)"}},
                      {"$addFields": {"f": {"$function": {"body": "x", "args": [], "lang": "js"}}}},
                      {"$group": {"_id": 1, "a": {"$accumulator": {}}}}):
            with self.assertRaises(ApiError, msg=str(stage)):
                admin.check_pipeline([{"$match": {}}, stage])

    def test_the_shape_of_a_pipeline_is_checked(self):
        for bad in ("not json", "{}", [], [{"$match": {}, "$limit": 1}], ["$match"], [{"$limit": 1}] * 13):
            with self.assertRaises(ApiError, msg=str(bad)):
                admin.check_pipeline(bad)


class CubeSql(unittest.TestCase):
    def test_a_rollup_over_two_dimensions(self):
        sql, params = admin.cube_sql(["year", "flat_type"], "median_price", "rollup", {"town": "TAMPINES"})
        self.assertIn("GROUP BY ROLLUP (sale_year, flat_type_name)", sql)
        self.assertIn("PERCENTILE_CONT(0.5)", sql)
        self.assertIn("GROUPING(sale_year, flat_type_name) AS level", sql)
        self.assertEqual(params, ["TAMPINES"])

    def test_cube_and_none_change_only_the_group_by(self):
        self.assertIn("GROUP BY CUBE (town_name)", admin.cube_sql(["town"], "sales", "cube")[0])
        sql = admin.cube_sql(["town"], "sales", "none")[0]
        self.assertIn("GROUP BY town_name", sql)
        self.assertNotIn("ROLLUP", sql)

    def test_a_slice_is_one_value_and_a_dice_is_several(self):
        sql, params = admin.cube_sql(["town"], "sales", "none", {"year": 2024})
        self.assertIn("sale_year = %s", sql)
        self.assertEqual(params, [2024])
        sql, params = admin.cube_sql(["town"], "sales", "none", {"town": ["BEDOK", "BISHAN"]})
        self.assertIn("town_name = ANY(%s)", sql)
        self.assertEqual(params, [["BEDOK", "BISHAN"]])

    def test_filter_values_never_reach_the_sql_text(self):
        evil = "x'; DROP TABLE users; --"
        sql, params = admin.cube_sql(["town"], "sales", "none", {"town": evil})
        self.assertNotIn("DROP", sql)
        self.assertEqual(params, [evil])

    def test_only_listed_dimensions_measures_and_totals_are_accepted(self):
        for args in ((["password_hash"], "sales", "rollup"), (["year", "year"], "sales", "rollup"), ([], "sales", "rollup"),
                     (["year", "town", "street"], "sales", "rollup"), (["year"], "DROP TABLE", "rollup"),
                     (["year"], "sales", "grouping sets"), (["year"], "sales", "rollup", {"email": "x"}),
                     (["year"], "sales", "rollup", {"year": "not a number"})):
            with self.assertRaises(ApiError, msg=str(args)):
                admin.cube_sql(*args)

    def test_every_drill_path_is_made_of_known_dimensions(self):
        for chain in admin.CUBE_HIERARCHIES.values():
            for dim in chain:
                self.assertIn(dim, admin.CUBE_DIMS)


class Identity(unittest.TestCase):
    def test_the_user_name_admin_is_the_admin_account_and_nobody_else_is_rewritten(self):
        self.assertEqual(admin.login_identifier("admin"), admin.ADMIN_EMAIL)
        self.assertEqual(admin.login_identifier("  ADMIN "), admin.ADMIN_EMAIL)
        self.assertEqual(admin.login_identifier("Someone@Example.com"), "someone@example.com")
        self.assertEqual(admin.login_identifier(None), "")

    def test_admin_password_comes_from_the_environment_when_set(self):
        with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "from-env-123"}):
            self.assertEqual(admin.admin_password(), "from-env-123")
        with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": ""}):
            self.assertEqual(admin.admin_password(), "admintest")

    def test_on_render_the_demo_password_is_never_used(self):
        with mock.patch.dict(os.environ, {"RENDER": "true", "ADMIN_PASSWORD": ""}):
            self.assertFalse(admin.admin_enabled())
        with mock.patch.dict(os.environ, {"RENDER": "true", "ADMIN_PASSWORD": "a-long-one"}):
            self.assertTrue(admin.admin_enabled())
        with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": ""}):
            os.environ.pop("RENDER", None)
            self.assertTrue(admin.admin_enabled())

    def test_jsonable_handles_what_the_databases_return(self):
        import datetime, decimal
        self.assertEqual(admin.jsonable(decimal.Decimal("5")), 5)
        self.assertEqual(admin.jsonable(decimal.Decimal("5.25")), 5.25)
        self.assertEqual(admin.jsonable(datetime.date(2026, 1, 2)), "2026-01-02")
        self.assertEqual(admin.jsonable(float("nan")), None)
        self.assertEqual(admin.jsonable({"a": [decimal.Decimal("1.5"), None]}), {"a": [1.5, None]})
        self.assertTrue(admin.jsonable("x" * 1000).endswith("…"))


class TerminalCommands(unittest.TestCase):
    def commands(self, db_url, mongo_uri):
        with mock.patch.object(admin.config, "database_url", return_value=db_url),              mock.patch.object(admin.config, "mongo_uri", return_value=mongo_uri),              mock.patch.object(admin.config, "mongo_db_name", return_value="resalewise"):
            return admin.terminal_commands()

    def test_local_databases_are_opened_through_docker(self):
        c = self.commands("postgresql://postgres:secret@localhost:5432/resalewise", "mongodb://localhost:27017")
        self.assertTrue(c["local"])
        self.assertTrue(c["postgres"].startswith("docker exec -it "))
        self.assertIn("psql -U postgres -d resalewise", c["postgres"])
        self.assertIn("mongosh resalewise", c["mongo"])

    def test_cloud_databases_get_the_plain_client_and_never_the_password(self):
        c = self.commands("postgresql://neon_user:hunter2@ep-x.neon.tech/neondb?sslmode=require",
                          "mongodb+srv://atlas_user:hunter3@cluster0.mongodb.net")
        self.assertFalse(c["local"])
        self.assertEqual(c["postgres"], 'psql "postgresql://neon_user@ep-x.neon.tech/neondb?sslmode=require"')
        self.assertEqual(c["mongo"], 'mongosh "mongodb+srv://atlas_user@cluster0.mongodb.net/resalewise"')
        self.assertNotIn("hunter", c["postgres"] + c["mongo"])


class ConcurrencyScript(unittest.TestCase):
    """The by-hand script of a run: statements in sending order, blocked ones marked."""

    def test_statements_come_in_sending_order_and_a_blocked_one_says_so(self):
        from resalewise import concurrency
        t = object.__new__(concurrency.Trial)
        t.steps = []
        import threading
        t._lock = threading.Lock()
        t.t0 = 0.0
        t.note("T1", "locks Alice", "ok", "UPDATE a", at=1.0, issued=1.0)
        t.note("T2", "goes through after waiting 900 ms", "ok", "UPDATE a2", at=3.0, issued=1.5, blocked=True)
        t.note("T2", "BLOCKED: waiting", "waiting", "UPDATE a2", at=1.6, issued=1.6)
        t.note("T1", "lock released", "ok", "COMMIT", at=2.0, issued=2.0)
        script = t.script()
        self.assertEqual([s["sql"] for s in script], ["UPDATE a", "UPDATE a2", "COMMIT"])
        self.assertTrue(script[1]["expect"].startswith("This terminal hangs"))
        self.assertNotIn("900", script[1]["expect"])
        self.assertEqual([s["sql"] for s in t.timeline()], ["UPDATE a", "UPDATE a2", "COMMIT", "UPDATE a2"])   # by the time each took effect
        self.assertNotIn("blocked", t.timeline()[0])


    def test_the_deadlock_script_does_not_name_a_victim(self):
        """Typed by hand the requests are seconds apart, which changes which session gets aborted."""
        from resalewise import concurrency
        quick = [{"session": x, "sql": q, "expect": "", "failed": False} for x, q in
                 [("T1", "BEGIN"), ("T1", "a"), ("T2", "BEGIN"), ("T2", "b"), ("T1", "c"), ("T2", "d"), ("T1", "ROLLBACK"), ("T2", "COMMIT")]]
        steps = concurrency._deadlock_by_hand(quick)
        self.assertEqual([s["sql"] for s in steps[:6]], ["BEGIN", "a", "BEGIN", "b", "c", "d"])
        self.assertIn("Which one depends on your timing", steps[5]["expect"])
        self.assertEqual([s["session"] for s in steps[6:]], ["", "", "T1", "T2"])      # rollback/commit name no terminal
        self.assertTrue(steps[8]["sql"].startswith("BEGIN;") and steps[8]["sql"].endswith("COMMIT"))


class Routes(unittest.TestCase):
    """Only admins may call /api/admin/*, checked against the database on every request."""

    def setUp(self):
        self.client = appmod.app.test_client()
        self.token = auth.make_token(7, "someone@example.com")
        self.headers = {"Authorization": "Bearer " + self.token}

    def as_user(self, is_admin):
        row = {"user_id": 7, "email": "someone@example.com", "name": "S", "created_at": None, "is_admin": is_admin}
        return mock.patch.object(dbm, "q", lambda sql, params=None: [row])

    def test_logged_out_is_401_and_a_forged_token_is_401(self):
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/overview", headers={"Authorization": "Bearer a.b"}).status_code, 401)

    def test_a_member_who_is_not_an_admin_is_403_on_every_admin_route(self):
        with self.as_user(False):
            for method, url in (("get", "/api/admin/overview"), ("post", "/api/admin/sql"), ("post", "/api/admin/mongo"),
                                ("post", "/api/admin/cube"), ("get", "/api/admin/cube/meta"), ("post", "/api/admin/indexes"),
                                ("post", "/api/admin/concurrency")):
                r = getattr(self.client, method)(url, headers=self.headers, **({"json": {}} if method == "post" else {}))
                self.assertEqual((url, r.status_code), (url, 403))
                self.assertEqual(r.get_json()["code"], "forbidden")

    def test_an_admin_gets_in_and_the_console_refuses_a_write_before_touching_the_database(self):
        with self.as_user(True):
            r = self.client.post("/api/admin/sql", headers=self.headers, json={"sql": "DELETE FROM users"})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.get_json()["code"], "sql_not_allowed")
            r = self.client.get("/api/admin/cube/meta", headers=self.headers)
            self.assertEqual(r.status_code, 200)
            self.assertIn("town", [d["key"] for d in r.get_json()["dims"]])

    def test_without_the_read_only_role_the_console_will_not_touch_the_users_table(self):
        """Fallback for logins that can't create roles: nothing else hides the hashes."""
        from resalewise.routes import admin as admin_routes
        for sql in ("SELECT * FROM users", "TABLE users", "SELECT u FROM users u", "SELECT to_jsonb(u) FROM public.users u",
                    "WITH x AS (SELECT * FROM USERS) SELECT * FROM x"):
            with self.assertRaises(ApiError, msg=sql):
                admin.check_basic_mode(sql)
        admin.check_basic_mode("SELECT town_name FROM v_transactions")           # other tables are fine
        admin.check_basic_mode("SELECT 'users' AS label".replace("'users'", "1"))
        calls = []

        class FakeCursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None): calls.append(sql)

        class FakeConn:
            def cursor(self, *a, **k): return FakeCursor()
        import contextlib

        @contextlib.contextmanager
        def fake_conn():
            yield FakeConn()
        with self.as_user(True), mock.patch.object(dbm, "pg_conn", fake_conn),                 mock.patch.object(admin_routes, "_enter_sandbox", lambda cur: "basic"):
            r = self.client.post("/api/admin/sql", headers=self.headers, json={"sql": "SELECT * FROM users"})
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "sql_not_allowed"))
        self.assertFalse([c for c in calls if "FROM users" in c], "the statement must not have been sent to the database")

    def test_a_pipeline_with_a_date_runs(self):
        """{"$date": ...} in the console has to turn into a real datetime before MongoDB sees it."""
        import datetime
        sent = []

        class FakeColl:
            def aggregate(self, pipeline, **kw):
                sent.append(pipeline)
                return [{"_id": "BEDOK", "sales": 3}]

        class FakeMongo:
            def list_collection_names(self): return ["transactions"]
            def __getitem__(self, name): return FakeColl()
        body = {"collection": "transactions",
                "pipeline": [{"$match": {"month": {"$gte": {"$date": "2025-01-01T00:00:00Z"}}}}, {"$group": {"_id": "$town"}}]}
        with self.as_user(True), mock.patch.object(dbm, "mongo", lambda: FakeMongo()):
            r = self.client.post("/api/admin/mongo", headers=self.headers, json=body)
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(sent[0][0]["$match"]["month"]["$gte"], datetime.datetime)

    def test_the_admin_user_name_signs_in_and_the_reply_says_so(self):
        from werkzeug.security import generate_password_hash
        rows = [{"user_id": 1, "email": admin.ADMIN_EMAIL, "name": "Administrator", "is_admin": True,
                 "password_hash": generate_password_hash("admintest")}]
        looked_up = []

        def fake_q(sql, params=None):
            looked_up.append(params)
            return rows
        with mock.patch.object(dbm, "q", fake_q):
            ok = self.client.post("/api/login", json={"email": "admin", "password": "admintest"})
            bad = self.client.post("/api/login", json={"email": "admin", "password": "wrong"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.get_json()["user"]["is_admin"])
        self.assertEqual(looked_up[0], [admin.ADMIN_EMAIL])
        self.assertEqual(bad.status_code, 401)

    def test_nobody_can_register_as_the_admin(self):
        r = self.client.post("/api/register", json={"email": admin.ADMIN_EMAIL, "password": "secret123", "name": "Imposter"})
        self.assertEqual(r.status_code, 409)


if __name__ == "__main__":
    unittest.main()

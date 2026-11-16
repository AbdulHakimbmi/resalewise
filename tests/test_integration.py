"""Integration tests against REAL PostgreSQL and MongoDB databases.

They create and delete users, bookmarks and reviews, so they only run when
you point them at a test database explicitly:

    RW_INTEGRATION=1 python -m pytest tests/test_integration.py -v

using DATABASE_URL / MONGO_URI from the environment (or .env). Run
`python scripts/load.py --reset` first; the engine tests also need
`python scripts/load_mongo_transactions.py`. Skipped otherwise.
"""
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RUN = os.environ.get("RW_INTEGRATION") == "1"
if RUN:
    from resalewise import config
    os.environ["RW_SKIP_STARTUP_MIGRATIONS"] = "1"
    os.environ.setdefault("TOKEN_SECRET", "integration-test-secret-0123456789")
    import psycopg2
    from pymongo.errors import ServerSelectionTimeoutError, WriteError
    from resalewise import create_app, db, web


@unittest.skipUnless(RUN, "set RW_INTEGRATION=1 (and a test DATABASE_URL / MONGO_URI) to run")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    def setUp(self):
        web.limiter._hits.clear()
        web._cache.clear()
        db._feature_cache.clear()
        self.c = self.app.test_client()

    # -- helpers ---------------------------------------------------------
    def register(self):
        email = "it-%s@example.com" % uuid.uuid4().hex[:10]
        r = self.c.post("/api/register", json={"email": email, "password": "secret-pw-1", "name": "IT User"})
        self.assertEqual(r.status_code, 201, r.get_json())
        body = r.get_json()
        return email, {"Authorization": "Bearer " + body["token"]}, body["user"]["user_id"]

    def any_txn(self, town="TAMPINES"):
        return db.q("SELECT t.txn_id FROM resale_transactions t JOIN blocks b USING (block_id) "
                    "JOIN towns tw USING (town_id) WHERE tw.town_name = %s ORDER BY t.txn_id DESC LIMIT 1",
                    [town])[0]["txn_id"]

    # -- PostgreSQL: accounts, many-to-many shortlist, triggers ------------
    def test_account_and_shortlist_lifecycle(self):
        email, H, uid = self.register()
        self.assertEqual(self.c.get("/api/me", headers=H).get_json()["email"], email)
        self.assertEqual(self.c.patch("/api/me", json={"name": "Renamed"}, headers=H).status_code, 200)

        txn = self.any_txn()
        self.assertEqual(self.c.post("/api/bookmarks", json={"txn_id": txn, "notes": "first"}, headers=H).status_code, 200)
        self.assertEqual(self.c.patch(f"/api/bookmarks/{txn}", json={"notes": "second"}, headers=H).status_code, 200)
        row = db.q("SELECT notes, updated_at FROM bookmarks WHERE user_id = %s AND txn_id = %s", [uid, txn])[0]
        self.assertEqual(row["notes"], "second")
        self.assertIsNotNone(row["updated_at"], "BEFORE UPDATE trigger must stamp updated_at")
        self.assertEqual(len(self.c.get("/api/bookmarks", headers=H).get_json()), 1)
        self.assertEqual(self.c.delete(f"/api/bookmarks/{txn}", headers=H).status_code, 200)

        actions = [a["action"] for a in self.c.get("/api/me/activity", headers=H).get_json()]
        self.assertEqual(actions, ["removed", "edited", "added"], "AFTER trigger must log every change")

        self.assertEqual(self.c.delete("/api/me", json={"password": "wrong"}, headers=H).status_code, 400)
        self.assertEqual(self.c.delete("/api/me", json={"password": "secret-pw-1"}, headers=H).status_code, 200)
        self.assertEqual(self.c.post("/api/login", json={"email": email, "password": "secret-pw-1"}).status_code, 401)
        self.assertEqual(db.q("SELECT COUNT(*) AS n FROM bookmark_audit WHERE user_id = %s", [uid])[0]["n"], 0)

    # -- the two databases must not disagree about a deleted account ------
    def test_account_delete_is_all_or_nothing_across_both_databases(self):
        email, H, uid = self.register()
        txn = self.any_txn()
        self.assertEqual(self.c.post("/api/bookmarks", json={"txn_id": txn}, headers=H).status_code, 200)
        r = self.c.post("/api/reviews", json={"town": "TAMPINES", "rating": 4, "comment": "Consistency test."}, headers=H)
        self.assertEqual(r.status_code, 201)
        review_id = r.get_json()["id"]

        # MongoDB unreachable: the request fails and PostgreSQL keeps the account and bookmark.
        with mock.patch.object(db, "mongo", side_effect=ServerSelectionTimeoutError("down")):
            self.assertEqual(self.c.delete("/api/me", json={"password": "secret-pw-1"}, headers=H).status_code, 503)
        self.assertEqual(db.q("SELECT COUNT(*) AS n FROM users WHERE user_id = %s", [uid])[0]["n"], 1)
        self.assertEqual(db.q("SELECT COUNT(*) AS n FROM bookmarks WHERE user_id = %s", [uid])[0]["n"], 1)

        # Both reachable: the account and bookmark are gone and the review no longer names the person.
        self.assertEqual(self.c.delete("/api/me", json={"password": "secret-pw-1"}, headers=H).status_code, 200)
        self.assertEqual(db.q("SELECT COUNT(*) AS n FROM users WHERE user_id = %s", [uid])[0]["n"], 0)
        from bson import ObjectId
        doc = db.mongo().reviews.find_one({"_id": ObjectId(review_id)})
        self.assertEqual((doc["author"], "user_id" in doc), ("Former member", False))
        db.mongo().reviews.delete_one({"_id": doc["_id"]})

    def test_duplicate_email_is_409_not_500(self):
        email, _, _ = self.register()
        r = self.c.post("/api/register", json={"email": email.upper(), "password": "secret-pw-1", "name": "Dup"})
        self.assertEqual(r.status_code, 409)

    def test_check_constraints_reject_impossible_rows(self):
        bad = [
            ("resale_price", "-1"), ("floor_area_sqm", "5"), ("storey_low", "9"),     # 9 > storey_high 3
            ("month", "DATE '2026-09-15'"),                                            # not the 1st
        ]
        base = db.q("SELECT block_id, flat_type_id, flat_model_id FROM resale_transactions LIMIT 1")[0]
        for col, val in bad:
            with self.subTest(col=col):
                vals = {"month": "DATE '2026-09-01'", "storey_low": "1", "storey_high": "3",
                        "floor_area_sqm": "90", "resale_price": "500000"}
                vals[col] = val
                sql = ("INSERT INTO resale_transactions (month, block_id, flat_type_id, flat_model_id, "
                       "storey_low, storey_high, floor_area_sqm, resale_price) VALUES "
                       f"({vals['month']}, %s, %s, %s, {vals['storey_low']}, {vals['storey_high']}, "
                       f"{vals['floor_area_sqm']}, {vals['resale_price']})")
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    db.execute(sql, [base["block_id"], base["flat_type_id"], base["flat_model_id"]])

    # -- PostgreSQL: views and window functions ----------------------------

    def test_drivers_peers(self):
        body = self.c.get(f"/api/drivers/{self.any_txn()}").get_json()
        self.assertGreater(body["peers"]["n"], 0)
        self.assertIn("not causes", body["caveat"])

    # -- MongoDB: validator, CRUD with ownership, text search, $facet ------

    def test_mongo_validator_rejects_bad_documents(self):
        from datetime import datetime, timezone
        with self.assertRaises(WriteError):
            db.mongo().reviews.insert_one({"town": "BEDOK", "rating": 9, "comment": "x",
                                           "created_at": datetime.now(timezone.utc)})

    # -- Both engines agree -------------------------------------------------
    def test_engines_return_identical_results(self):
        n_mongo = db.mongo().transactions.estimated_document_count()
        n_pg = db.q("SELECT COUNT(*) AS n FROM resale_transactions")[0]["n"]
        if n_mongo != n_pg:
            self.skipTest(f"MongoDB holds {n_mongo:,} of {n_pg:,} sales: run scripts/load_mongo_transactions.py (no --limit)")
        for qs in ("town=TAMPINES&flat_type=4%20ROOM", "budget=400000&min_lease=70",
                   "town=BEDOK&storey=10-12&min_area=90", "flat_model=model%20a&max_area=95"):
            with self.subTest(qs=qs):
                body = self.c.get("/api/engines/search?" + qs).get_json()
                self.assertTrue(body["same_total"], body)
                self.assertTrue(body["same_rows"], body)


@unittest.skipUnless(RUN, "set RW_INTEGRATION=1 (and a test DATABASE_URL) to run")
class PrecomputedViewTests(unittest.TestCase):
    """The insights endpoints answer from small materialized views
    (mv_window_stats, mv_quarter_stats, mv_town_quarter_stats) when they are
    current. They must give EXACTLY the numbers the live queries give.
    PostgreSQL only; run `python scripts/migrate.py` first."""

    @classmethod
    def setUpClass(cls):
        from resalewise.routes import insights
        cls.ins = insights
        web._cache.clear()
        db._feature_cache.clear()
        if not insights.precomputed_ok():
            raise unittest.SkipTest("views missing or stale: run python scripts/migrate.py --refresh")
        cls.types = [r["n"] for r in db.q(
            "SELECT DISTINCT UPPER(REPLACE(flat_type_name, '-', ' ')) AS n FROM flat_types ORDER BY 1")]
        cls.towns = [None, "BEDOK", "PUNGGOL", "BUKIT TIMAH"]   # all towns, a big, a young and a small town

    def test_window_endpoints_match_live_queries(self):
        ins = self.ins
        for months in ins.PRECOMPUTED_WINDOWS:
            with self.subTest(months=months, what="by flat type"):
                for town in self.towns:
                    self.assertEqual(ins.by_flat_type_rows(months, town),
                                     ins.by_flat_type_rows(months, town, live=True))
            for ft in self.types:
                with self.subTest(months=months, flat_type=ft):
                    self.assertEqual(ins.town_medians_rows(ft, months),
                                     ins.town_medians_rows(ft, months, live=True))
                    for town in self.towns:
                        self.assertEqual(ins.summary_parts(ft, months, town),
                                         ins.summary_parts(ft, months, town, live=True))
                        self.assertEqual(ins.price_band_counts(ft, months, town),
                                         ins.price_band_counts(ft, months, town, live=True))

    def test_trend_matches_live_query(self):
        for months in (12, 24, 60, 240):
            for ft in ("3 ROOM", "4 ROOM", "EXECUTIVE"):
                for town in self.towns:
                    with self.subTest(months=months, flat_type=ft, town=town):
                        self.assertEqual(self.ins.trend_rows(ft, months, town),
                                         self.ins.trend_rows(ft, months, town, live=True))


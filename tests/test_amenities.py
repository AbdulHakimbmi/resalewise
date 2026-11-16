"""Tests for the nearby-amenities feature: the pure helpers, the MongoDB feature
builder (against an in-memory fake that mimics $geoNear), the price model on
synthetic data, and the API endpoints. No network and no database.

    python -m unittest discover -s tests
"""
import json
import logging
import math
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ["RW_SKIP_STARTUP_MIGRATIONS"] = "1"
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:1")
os.environ.setdefault("TOKEN_SECRET", "unit-test-secret-not-for-production-use")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "analysis"))

from resalewise import amenities as am  # noqa: E402
from resalewise.geo import R_MONGO, destination, haversine_m  # noqa: E402

logging.disable(logging.CRITICAL)


def square(lon, lat, side_m):
    """A closed GeoJSON ring: a square about `side_m` metres wide with its corner at (lon, lat)."""
    e = destination(lon, lat, 90, side_m)
    n = destination(lon, lat, 0, side_m)
    return [[lon, lat], [e[0], lat], [e[0], n[1]], [lon, n[1]], [lon, lat]]


class Scoring(unittest.TestCase):
    def test_proximity_is_100_inside_zero_beyond_and_linear_between(self):
        self.assertEqual(am.proximity_score(0, 200, 1200), 100)
        self.assertEqual(am.proximity_score(200, 200, 1200), 100)
        self.assertEqual(am.proximity_score(1200, 200, 1200), 0)
        self.assertEqual(am.proximity_score(5000, 200, 1200), 0)
        self.assertAlmostEqual(am.proximity_score(700, 200, 1200), 50)
        self.assertEqual(am.proximity_score(None, 200, 1200), 0)

    def test_every_kind_has_a_sensible_scale(self):
        for k in am.SCORE_KINDS:
            self.assertLess(k.full_m, k.zero_m, k.kind)
        self.assertEqual(am.kind_score("primary_school", 1000), 100)    # inside MOE's 1 km phase
        self.assertEqual(am.kind_score("primary_school", 2000), 0)

    def test_composite_is_weighted_mean_of_chosen_kinds_only(self):
        d = {"park": 0, "hawker": 5000, "mrt": 0}
        score, per = am.composite_score(d, {"park": 1, "hawker": 1, "mrt": 0})
        self.assertEqual(score, 50.0)                                   # (100 + 0) / 2; MRT has weight 0
        self.assertEqual(per["mrt"], 100)
        score, _ = am.composite_score(d, {"park": 3, "hawker": 1})
        self.assertEqual(score, 75.0)
        self.assertIsNone(am.composite_score(d, {"park": 0})[0])

    def test_missing_distance_scores_zero_not_an_error(self):
        score, per = am.composite_score({}, {"park": 2})
        self.assertEqual(score, 0.0)
        self.assertEqual(per["park"], 0.0)

    def test_walk_minutes(self):
        self.assertEqual(am.walk_minutes(None), None)
        self.assertEqual(am.walk_minutes(1), 1)
        self.assertEqual(am.walk_minutes(800), 13)                      # 800 * 1.3 / 80


class GeometryAndGeoJson(unittest.TestCase):
    def test_polygon_boundary_points_are_at_most_step_apart(self):
        ring = square(103.85, 1.30, 1000)
        pts = am.geometry_points({"type": "Polygon", "coordinates": [ring]}, step_m=100, max_points=10_000)
        self.assertGreaterEqual(len(pts), 40)
        # Every point sits on the boundary; consecutive points are no more than ~100 m apart.
        for a, b in zip(pts, pts[1:]):
            self.assertLessEqual(haversine_m(a[0], a[1], b[0], b[1]), 101)

    def test_point_budget_widens_the_spacing(self):
        ring = square(103.85, 1.30, 5000)
        pts = am.geometry_points({"type": "Polygon", "coordinates": [ring]}, step_m=10, max_points=100)
        self.assertLessEqual(len(pts), 110)                              # the 4 corners add a few

    def test_point_multipoint_and_unknown(self):
        self.assertEqual(am.geometry_points({"type": "Point", "coordinates": [103.8, 1.3, 15]}), [(103.8, 1.3)])
        self.assertEqual(len(am.geometry_points({"type": "MultiPoint", "coordinates": [[103.8, 1.3], [103.9, 1.4]]})), 2)
        self.assertEqual(am.geometry_points({"type": "Nonsense"}), [])
        self.assertEqual(am.geometry_points(None), [])

    def test_name_from_plain_property_and_from_html_description(self):
        self.assertEqual(am.feature_name({"NAME": "Bishan-Ang Mo Kio Park"}), "Bishan-Ang Mo Kio Park")
        html = "<table><tr><th>NAME</th><td>Maxwell Food Centre</td></tr><tr><th>ADDRESS</th><td>1 Kadayanallur</td></tr></table>"
        self.assertEqual(am.feature_name({"Description": html}), "Maxwell Food Centre")
        self.assertEqual(am.feature_name({"Description": html}, name_field="ADDRESS"), "1 Kadayanallur")
        self.assertIsNone(am.feature_name({"NAME": "NIL"}))
        self.assertIsNone(am.feature_name({}))

    def test_the_kinds_are_what_the_project_ships(self):
        self.assertEqual(am.KIND_NAMES, ["park", "hawker", "gym", "childcare", "primary_school", "secondary_school"])
        self.assertNotIn("supermarket", am.KIND_BY_NAME)
        for k in am.KINDS:                                   # every scale must fall from full to zero
            self.assertLess(k.full_m, k.zero_m, k.kind)

    def test_kinds_the_project_no_longer_has_are_retired(self):
        self.assertEqual(am.retired_kinds(["park", "supermarket", "gym"]), ["supermarket"])
        self.assertEqual(am.retired_kinds(am.KIND_NAMES), [])

    def test_a_kml_placeholder_name_is_not_used_as_the_name(self):
        html = "<table><tr><th>NAME</th> <td>The Pit</td></tr><tr><th>ADDRESSSTREETNAME</th> <td>Tras Street</td></tr></table>"
        self.assertEqual(am.feature_name({"Name": "kml_1", "Description": html}), "The Pit")
        self.assertIsNone(am.feature_name({"Name": "kml_7"}))        # nothing better: the loader numbers it

    def test_parks_with_one_name_merge_but_gyms_do_not(self):
        parts = {"type": "FeatureCollection", "features": [
            {"properties": {"NAME": "East Coast Park"}, "geometry": {"type": "Polygon", "coordinates": [square(103.90, 1.30, 300)]}},
            {"properties": {"NAME": "East Coast Park"}, "geometry": {"type": "Polygon", "coordinates": [square(103.92, 1.30, 300)]}},
            {"properties": {"NAME": "Bedok Park"}, "geometry": {"type": "Polygon", "coordinates": [square(103.93, 1.32, 300)]}}]}
        groups, info = am.parse_geojson(parts, "park")
        self.assertEqual(sorted(n for n, _ in groups), ["Bedok Park", "East Coast Park"])
        shops = {"features": [
            {"properties": {"NAME": "Anytime Fitness"}, "geometry": {"type": "Point", "coordinates": [103.85, 1.30]}},
            {"properties": {"NAME": "Anytime Fitness"}, "geometry": {"type": "Point", "coordinates": [103.95, 1.35]}}]}
        groups, _ = am.parse_geojson(shops, "gym")
        self.assertEqual(len(groups), 2)                                 # two branches, two places
        docs = am.build_amenities("gym", groups, "test")
        self.assertEqual(len({d["_id"] for d in docs}), 2)               # unique ids

    def test_points_outside_singapore_and_empty_shapes_are_dropped(self):
        gj = {"features": [
            {"properties": {"NAME": "Far away"}, "geometry": {"type": "Point", "coordinates": [-74.0, 40.7]}},
            {"properties": {"NAME": "No shape"}, "geometry": None},
            {"properties": {"NAME": "Fine"}, "geometry": {"type": "Point", "coordinates": [103.85, 1.30]}}]}
        groups, info = am.parse_geojson(gj, "hawker")
        self.assertEqual([n for n, _ in groups], ["Fine"])
        self.assertEqual((info["outside_sg"], info["no_geometry"]), (1, 1))

    def test_unnamed_features_get_a_fallback_name_and_are_counted(self):
        gj = {"features": [{"properties": {"X": 1}, "geometry": {"type": "Point", "coordinates": [103.85, 1.30]}}]}
        groups, info = am.parse_geojson(gj, "hawker")
        self.assertEqual(info["unnamed"], 1)
        self.assertEqual(list(groups)[0][0], "Hawker centre 1")
        self.assertIn("X", info["fields_seen"])

    def test_build_amenities_point_vs_multipoint(self):
        docs = am.build_amenities("park", {("Tiny Park", "TINY PARK"): [(103.85, 1.30)],
                                           ("Big Park", "BIG PARK"): [(103.85, 1.30), (103.86, 1.31), (103.85, 1.30)]}, "t")
        by = {d["name"]: d for d in docs}
        self.assertEqual(by["Tiny Park"]["loc"]["type"], "Point")
        self.assertEqual(by["Big Park"]["loc"]["type"], "MultiPoint")
        self.assertEqual(by["Big Park"]["n_points"], 2)                  # duplicate point removed
        self.assertEqual(by["Big Park"]["_id"], "park-big-park")
        self.assertTrue(all(isinstance(c, float) for c in by["Tiny Park"]["centroid"]["coordinates"]))


class Schools(unittest.TestCase):
    def test_level_classification(self):
        self.assertEqual(am.classify_school("PRIMARY"), "primary_school")
        self.assertEqual(am.classify_school("Secondary"), "secondary_school")
        self.assertEqual(am.classify_school("MIXED LEVEL"), "secondary_school")
        self.assertIsNone(am.classify_school("JUNIOR COLLEGE"))
        self.assertIsNone(am.classify_school("CENTRALISED INSTITUTE"))
        self.assertIsNone(am.classify_school(None))

    def test_integrated_schools_with_a_primary_intake_count_as_both(self):
        self.assertEqual(am.school_kinds("MIXED LEVEL (P1-S4)"), ["primary_school", "secondary_school"])
        self.assertEqual(am.school_kinds("MIXED LEVEL (S1-JC2)"), ["secondary_school"])
        self.assertEqual(am.school_kinds("SECONDARY (S1-S5)"), ["secondary_school"])
        self.assertEqual(am.school_kinds("JUNIOR COLLEGE"), [])

    def test_postal_codes_regain_their_leading_zero(self):
        self.assertEqual(am.clean_postal("78884"), "078884")
        self.assertEqual(am.clean_postal(" 569933 "), "569933")
        self.assertIsNone(am.clean_postal(""))
        self.assertIsNone(am.clean_postal("1234567"))

    def test_column_names_are_found_whatever_the_style(self):
        cols = ["School_Name", "POSTAL CODE", "mainlevel_code"]
        self.assertEqual(am.find_column(cols, am.SCHOOL_COLUMNS["name"]), "School_Name")
        self.assertEqual(am.find_column(cols, am.SCHOOL_COLUMNS["postal"]), "POSTAL CODE")
        self.assertEqual(am.find_column(cols, am.SCHOOL_COLUMNS["level"]), "mainlevel_code")
        self.assertIsNone(am.find_column(["a"], am.SCHOOL_COLUMNS["name"]))

    def test_only_the_exact_postal_code_is_accepted(self):
        hits = [{"POSTAL": "569934", "LATITUDE": "1.37", "LONGITUDE": "103.84"},
                {"POSTAL": "569933", "LATITUDE": "1.3701", "LONGITUDE": "103.8401"},
                {"POSTAL": "569933", "LATITUDE": "40.7", "LONGITUDE": "-74.0"}]
        self.assertEqual(am.pick_postal_result(hits, "569933"), (1.3701, 103.8401))
        self.assertIsNone(am.pick_postal_result(hits, "000001"))
        self.assertIsNone(am.pick_postal_result([], "569933"))


class EngineComparison(unittest.TestCase):
    def test_tolerance_and_counts(self):
        a = [(1, "park", 100.0, "A", 1, 2, 3), (2, "park", 1000.0, "B", 0, 1, 1), (3, "park", 300.0, "C", 0, 0, 1)]
        b = [(1, "park", 100.2, "A", 1, 2, 3), (2, "park", 1004.0, "B", 0, 1, 2), (4, "park", 5.0, "D", 0, 0, 0)]
        r = am.compare_rows(a, b)
        self.assertEqual(r["pairs_compared"], 2)
        self.assertEqual((r["only_in_first"], r["only_in_second"]), (1, 1))
        self.assertEqual(r["nearest_distance_agree"], 2)                 # 0.2 m and 4 m (0.4 %): within 1 %
        self.assertEqual(r["counts_identical"], 1)
        self.assertEqual(r["max_abs_diff_m"], 4.0)

    def test_a_big_gap_is_caught(self):
        r = am.compare_rows([(1, "park", 100.0, "A", 0, 1, 1)], [(1, "park", 180.0, "A", 0, 1, 1)])
        self.assertEqual(r["nearest_distance_agree"], 0)

    def test_empty_inputs(self):
        r = am.compare_rows([], [])
        self.assertEqual(r["pairs_compared"], 0)
        self.assertIsNone(r["nearest_distance_agree_pct"])


# --------------------------------------------------------------------------- MongoDB feature builder
class FakeAmenityCollection:
    """Mimics the two $geoNear pipelines build_amenity_features.py runs, in Python."""

    def __init__(self, docs):
        self.docs = docs
        self.pipelines = []

    @staticmethod
    def _dist(doc, near):
        loc = doc["loc"]
        pts = [loc["coordinates"]] if loc["type"] == "Point" else loc["coordinates"]
        return min(haversine_m(near[0], near[1], p[0], p[1], r=R_MONGO) for p in pts)

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        stage = pipeline[0]["$geoNear"]
        assert stage["spherical"] and stage["key"] == "loc"
        near = stage["near"]["coordinates"]
        hits = [(self._dist(d, near), d) for d in self.docs
                if all(d.get(k) == v for k, v in stage.get("query", {}).items())]
        hits = sorted((h for h in hits if h[0] <= stage.get("maxDistance", float("inf"))), key=lambda h: h[0])
        keys = [list(s)[0] for s in pipeline[1:]]
        if keys == ["$sort", "$group"]:
            out = {}
            for d, doc in hits:
                g = out.setdefault(doc["kind"], {"_id": doc["kind"], "nearest_m": d, "nearest_name": doc["name"],
                                                  "n500": 0, "n1k": 0, "n2k": 0})
                g["n500"] += d <= 500
                g["n1k"] += d <= 1000
                g["n2k"] += 1
            return list(out.values())
        assert keys == ["$limit", "$project"], keys
        return [{"_id": doc["_id"], "name": doc["name"], "d": d} for d, doc in hits[:1]]


def point_doc(i, kind, lon, lat, name=None):
    return {"_id": f"{kind}-{i}", "name": name or f"{kind} {i}", "kind": kind,
            "loc": {"type": "Point", "coordinates": [lon, lat]}}


class MongoFeatureBuilder(unittest.TestCase):
    def setUp(self):
        import build_amenity_features as baf
        self.baf = baf
        self.home = (103.85, 1.30)

    def doc_at(self, kind, bearing, dist, i=1, name=None):
        lon, lat = destination(self.home[0], self.home[1], bearing, dist)
        return point_doc(i, kind, lon, lat, name)

    def run_block(self, docs, kinds):
        coll = FakeAmenityCollection(docs)
        block = {"_id": 77, "loc": {"type": "Point", "coordinates": list(self.home)}}
        return coll, self.baf.block_rows_mongo(coll, block, kinds)

    def test_nearest_and_counts_by_radius(self):
        docs = [self.doc_at("hawker", 0, 300, 1, "Near"), self.doc_at("hawker", 90, 800, 2),
                self.doc_at("hawker", 180, 1800, 3), self.doc_at("hawker", 270, 2500, 4)]
        _, rows = self.run_block(docs, ["hawker"])
        (block_id, kind, nearest, name, n500, n1k, n2k), = rows
        self.assertEqual((block_id, kind, name), (77, "hawker", "Near"))
        self.assertAlmostEqual(nearest, 300, delta=1)
        self.assertEqual((n500, n1k, n2k), (1, 2, 3))                    # the 2.5 km one is not counted

    def test_kind_with_nothing_within_2km_falls_back_to_the_true_nearest(self):
        docs = [self.doc_at("park", 45, 4200, 1, "Far Park"), self.doc_at("park", 45, 6000, 2),
                self.doc_at("hawker", 0, 100, 3)]
        coll, rows = self.run_block(docs, ["hawker", "park"])
        by = {r[1]: r for r in rows}
        self.assertAlmostEqual(by["park"][2], 4200, delta=2)
        self.assertEqual(by["park"][3], "Far Park")
        self.assertEqual(by["park"][4:], (0, 0, 0))
        self.assertEqual(len(coll.pipelines), 2)                         # one grouped query + one fallback

    def test_kind_with_no_amenities_at_all_gives_no_row(self):
        _, rows = self.run_block([self.doc_at("hawker", 0, 100)], ["hawker", "park"])
        self.assertEqual([r[1] for r in rows], ["hawker"])

    def test_counts_are_nested_as_the_table_constraint_requires(self):
        docs = [self.doc_at("hawker", b, d, i) for i, (b, d) in enumerate(
            [(0, 90), (30, 480), (60, 520), (90, 990), (120, 1010), (150, 1999), (180, 2001)])]
        _, rows = self.run_block(docs, ["hawker"])
        n500, n1k, n2k = rows[0][4:]
        self.assertTrue(n500 <= n1k <= n2k)
        self.assertEqual((n500, n1k, n2k), (2, 4, 6))

    def test_a_park_is_measured_to_its_nearest_boundary_point(self):
        e = destination(self.home[0], self.home[1], 90, 250)
        far = destination(self.home[0], self.home[1], 90, 900)
        park = {"_id": "park-x", "name": "Big Park", "kind": "park",
                "loc": {"type": "MultiPoint", "coordinates": [list(e), list(far)]}}
        _, rows = self.run_block([park], ["park"])
        self.assertAlmostEqual(rows[0][2], 250, delta=1)
        self.assertEqual(rows[0][6], 1)                                  # one park, not two points


# --------------------------------------------------------------------------- the price model
try:
    import sklearn  # noqa: F401
    import pandas as pd
    import numpy as np
    HAVE_SKLEARN = True
except ImportError:                                                      # pragma: no cover
    HAVE_SKLEARN = False


def synthetic_sales(seed=7, n_blocks=60, n_sales=7000, park_effect=-0.00015):
    """Sales where a block's price relative to the market falls with its distance to the
    nearest park, plus a town effect and noise. Distance to a hawker centre is pure noise."""
    rng = np.random.default_rng(seed)
    blocks = pd.DataFrame({
        "block_id": np.arange(n_blocks),
        "town": rng.choice(["ALPHA", "BETA", "GAMMA"], n_blocks),
        "park_m": rng.uniform(50, 2500, n_blocks),
        "hawker_m": rng.uniform(50, 2500, n_blocks),
        "nearest_mrt_m": rng.uniform(100, 2000, n_blocks)})
    blocks["park_n1k"] = (blocks.park_m <= 1000).astype(float)
    blocks["hawker_n1k"] = (blocks.hawker_m <= 1000).astype(float)
    s = blocks.iloc[rng.integers(0, n_blocks, n_sales)].reset_index(drop=True)
    months = pd.to_datetime("2021-01-01") + pd.to_timedelta(rng.integers(0, 48, n_sales) * 30.4, unit="D")
    s["month"] = months.to_period("M").to_timestamp()
    s["flat_type"] = rng.choice(["3 ROOM", "4 ROOM"], n_sales)
    s["flat_model"] = rng.choice(["IMPROVED", "NEW GENERATION"], n_sales)
    s["area"] = rng.normal(90, 12, n_sales)
    s["storey"] = rng.integers(1, 30, n_sales).astype(float)
    s["lease_years"] = rng.uniform(55, 95, n_sales)
    town_fx = s.town.map({"ALPHA": 0.10, "BETA": 0.0, "GAMMA": -0.08})
    rel = (park_effect * s.park_m + town_fx + 0.004 * (s.storey - 15) + 0.002 * (s.area - 90)
           + rng.normal(0, 0.02, n_sales))
    t = (s.month.dt.year - 2021) * 12 + s.month.dt.month
    s["psm"] = 5000 * (1 + 0.006 * t) * np.exp(rel)
    s["opened_year"] = np.nan
    return s


@unittest.skipUnless(HAVE_SKLEARN, "scikit-learn is not installed")
class PriceModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import amenity_model as m
        cls.m = m
        cls.df = m.mask_unopened_stations(m.add_target(synthetic_sales()))
        cls.kinds = ["hawker", "park"]
        cls.res = m.run_experiment(cls.df, cls.kinds, test_months=12, seed=1, repeats=2)

    def test_amenity_label_names_only_loaded_kinds(self):
        # The report must not claim supermarkets when none were loaded.
        label = next(x["label"] for x in self.res["models"] if x["id"] == "amenities")
        self.assertEqual(label, "+ hawker centres, parks")
        self.assertNotIn("gym", label)

    def test_a_long_list_of_kinds_is_counted_not_spelled_out(self):
        self.assertEqual(self.m.amenity_label(["park", "hawker", "gym"]), "+ parks, hawker centres, gyms")
        self.assertEqual(self.m.amenity_label(["park", "hawker", "gym", "childcare"]), "+ 4 kinds of nearby places")

    def test_target_removes_the_market_trend(self):
        # y is relative to the quarter's median, so it has no drift over time.
        d = self.df.assign(year=self.df.month.dt.year)
        means = d.groupby("year")["y"].mean()
        self.assertLess(means.max() - means.min(), 0.05)

    def test_test_months_come_after_the_training_months(self):
        tr, te = self.m.time_split(self.df, 12)
        self.assertLess(tr.month.max(), te.month.min())
        self.assertEqual(len(tr) + len(te), len(self.df))

    def test_amenities_help_when_they_matter_and_the_model_beats_no_model(self):
        by = {r["id"]: r for r in self.res["models"]}
        self.assertEqual(list(by), ["attributes", "mrt", "amenities", "full"])
        self.assertLess(by["amenities"]["mape_pct"], by["attributes"]["mape_pct"] * 0.8)
        self.assertGreater(by["amenities"]["lift_vs_attributes_pct"], 20)
        self.assertLess(by["amenities"]["mape_pct"], self.res["naive"]["mape_pct"] * 0.5)

    def test_importance_ranks_the_real_driver_first_and_noise_near_zero(self):
        imp = {r["group"]: r["increase_pct_points"] for r in self.res["importance"]}
        top = self.res["importance"][0]["group"]
        self.assertIn(top, ("park", "town"))
        self.assertGreater(imp["park"], 0.5)
        self.assertLess(abs(imp["hawker"]), 0.3)

    def test_premium_is_positive_near_a_park_and_negative_far_from_one(self):
        m = self.m
        num, cat = m.feature_lists(self.kinds, False, True)
        model, cats = m.fit_model(self.df, num, cat, 1)
        blocks = self.df.drop_duplicates("block_id")
        prem = m.location_premium(model, cats, self.df, num, cat, m.amenity_cols(self.kinds), blocks, min_sales=5)
        p = prem.merge(blocks[["block_id", "park_m"]], on="block_id")
        near, far = p[p.park_m < 300], p[p.park_m > 2000]
        self.assertGreater(len(near), 0)
        self.assertGreater(len(far), 0)
        self.assertGreater(near.premium_pct.mean(), 3)
        self.assertLess(far.premium_pct.mean(), -3)
        self.assertTrue((prem.n_sales >= 5).all())

    def test_stations_that_opened_after_the_sale_are_ignored(self):
        d = self.df.copy()
        d["opened_year"] = 2030.0
        masked = self.m.mask_unopened_stations(d)
        self.assertTrue(masked["nearest_mrt_m"].isna().all())

    def test_metrics_are_percentage_errors_on_the_price_scale(self):
        r = self.m.error_metrics([0.0, 0.0], [math.log(1.1), math.log(0.9)])
        self.assertAlmostEqual(r["mape_pct"], 10.0, places=2)


# --------------------------------------------------------------------------- API
class ApiBase(unittest.TestCase):
    RELATIONS = {"amenities", "block_amenity_features", "amenity_kinds", "block_features", "block_location_premium"}

    def setUp(self):
        import app as appmod
        from resalewise import db as dbm, web
        self.dbm = dbm
        web.limiter._hits.clear()
        web._cache.clear()
        dbm._feature_cache.clear()
        self.calls = []
        self.relations = set(self.RELATIONS)
        for target, fake in (("q", self.fake_q), ("has_relation", lambda n: n in self.relations)):
            p = mock.patch.object(dbm, target, fake)
            p.start()
            self.addCleanup(p.stop)
        self.client = appmod.app.test_client()

    def fake_q(self, sql, params=None):
        sql = " ".join(sql.split())
        self.calls.append((sql, list(params or [])))
        if "FROM block_amenity_features f JOIN amenity_kinds" in sql:
            return [{"kind": "park", "label": "Park", "nearest_m": 240.0, "nearest_name": "Bishan Park",
                     "within_500m": 1, "within_1km": 2, "within_2km": 3},
                    {"kind": "hawker", "label": "Hawker centre", "nearest_m": 650.0, "nearest_name": "Maxwell",
                     "within_500m": 0, "within_1km": 1, "within_2km": 4}]
        if "ST_ClosestPoint" in sql:
            return [{"kind": "gym", "name": "Pure Fitness", "d": 420.46, "lat": 1.3501, "lon": 103.8502},
                    {"kind": "childcare", "name": "Little Skool-House", "d": 95.04, "lat": 1.3499, "lon": 103.8498}]
        if "FROM block_features WHERE block_id = %s" in sql:
            return [{"nearest_station_name": "Bishan", "nearest_mrt_m": 410.0, "stations_within_1km": 2}]
        if "FROM block_location_premium WHERE block_id = %s" in sql:
            return [{"premium_pct": 3.4, "n_sales": 18, "model_run": "20261101T0900"}]
        if "PERCENTILE_CONT" in sql:
            return [{"block_id": 1, "block_no": "1", "street_name": "NEAR ST", "town_name": "BISHAN",
                     "median_price": 700000, "median_psm": 7000, "n_sales": 9, "latest_txn_id": 101},
                    {"block_id": 2, "block_no": "2", "street_name": "FAR RD", "town_name": "BISHAN",
                     "median_price": 600000, "median_psm": 6000, "n_sales": 5},
                    {"block_id": 3, "block_no": "3", "street_name": "NOWHERE AVE", "town_name": "BISHAN",
                     "median_price": 650000, "median_psm": 6500, "n_sales": 4}]
        if sql.startswith("SELECT DISTINCT kind FROM block_amenity_features"):
            return [{"kind": "park"}, {"kind": "hawker"}]
        if "FROM block_amenity_features WHERE block_id = ANY" in sql:
            return [{"block_id": 1, "kind": "park", "nearest_m": 100.0, "within_1km": 3},
                    {"block_id": 1, "kind": "hawker", "nearest_m": 900.0, "within_1km": 1},
                    {"block_id": 2, "kind": "park", "nearest_m": 1500.0, "within_1km": 0},
                    {"block_id": 2, "kind": "hawker", "nearest_m": 100.0, "within_1km": 2}]
        if "FROM block_features WHERE block_id = ANY" in sql:
            return [{"block_id": 1, "m": 250.0, "stations_within_1km": 2},
                    {"block_id": 2, "m": 1400.0, "stations_within_1km": 0}]
        if "FROM block_location_premium WHERE block_id = ANY" in sql:
            return [{"block_id": 1, "premium_pct": 4.2}]
        if "FROM amenities GROUP BY kind" in sql:
            return [{"kind": "hawker", "n": 110}, {"kind": "park", "n": 400}]
        if "COUNT(DISTINCT block_id)" in sql:
            return [{"blocks": 9500}]
        if "FROM block_location_premium" in sql:
            return [{"n": 3000}]
        return []


class BlockEndpoint(ApiBase):
    def test_items_scores_walking_time_and_premium(self):
        body = self.client.get("/api/amenities/block/5").get_json()
        self.assertTrue(body["available"])
        self.assertEqual([i["kind"] for i in body["items"]], ["mrt", "park", "hawker"])
        park = body["items"][1]
        self.assertEqual((park["nearest_m"], park["nearest_name"], park["within_1km"]), (240.0, "Bishan Park", 2))
        self.assertEqual(park["walk_min"], 4)
        self.assertGreater(park["score"], 90)
        self.assertEqual(body["premium"]["premium_pct"], 3.4)

    def test_the_nearby_places_come_with_a_point_to_draw_and_are_capped_per_kind(self):
        body = self.client.get("/api/amenities/block/5").get_json()
        gym = next(p for p in body["places"] if p["kind"] == "gym")
        self.assertEqual((gym["name"], gym["distance_m"], gym["walk_min"]), ("Pure Fitness", 420.5, 7))
        self.assertEqual((gym["lat"], gym["lon"]), (1.3501, 103.8502))
        sql, params = next(c for c in self.calls if "ST_ClosestPoint" in c[0])
        self.assertEqual(params, [2000, 5, 5])                       # radius, block, at most five of each kind
        self.assertIn("PARTITION BY a.kind", sql)

    def test_unknown_block_is_a_json_404(self):
        self.relations.discard("block_features")
        self.dbm.q = lambda sql, params=None: []
        r = self.client.get("/api/amenities/block/999999")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["code"], "not_found")

    def test_not_ready_says_unavailable_and_is_never_cached(self):
        self.relations.clear()
        r = self.client.get("/api/amenities/block/5")
        self.assertEqual(r.get_json(), {"available": False})
        self.assertEqual(r.headers["Cache-Control"], "no-store")


class StatusAndResults(ApiBase):
    def test_status_counts(self):
        body = self.client.get("/api/amenities/status").get_json()
        self.assertEqual(body["amenities"], {"hawker": 110, "park": 400})
        self.assertEqual(body["blocks_with_features"], 9500)
        self.assertEqual(body["blocks_with_premium"], 3000)

    def test_model_results_404_until_the_script_has_run_then_served(self):
        from resalewise.routes import ops
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ops, "RESULTS_DIR", tmp):
            r = self.client.get("/api/results/amenity-model")
            self.assertEqual((r.status_code, r.get_json()["code"]), (404, "not_found"))
            with open(os.path.join(tmp, "amenity_model.json"), "w") as fh:
                json.dump({"models": [{"id": "full"}]}, fh)
            body = self.client.get("/api/results/amenity-model").get_json()
            self.assertEqual(body["models"][0]["id"], "full")
            self.assertFalse(body["sample"])


class SqlFiles(unittest.TestCase):
    """The SQL cannot run without PostgreSQL, but its structure can be checked."""

    def setUp(self):
        with open(os.path.join(ROOT, "sql", "amenities.sql"), encoding="utf-8") as fh:
            self.sql = fh.read()

    def test_expected_tables_constraints_and_indexes(self):
        for needle in ("CREATE TABLE IF NOT EXISTS amenity_kinds", "CREATE TABLE IF NOT EXISTS amenities",
                       "CREATE TABLE IF NOT EXISTS block_amenity_features", "CREATE TABLE IF NOT EXISTS block_location_premium",
                       "PRIMARY KEY (block_id, kind)", "chk_counts_nested", "REFERENCES amenity_kinds(kind)",
                       "REFERENCES blocks(block_id) ON DELETE CASCADE", "USING gist (geog)"):
            self.assertIn(needle, self.sql, needle)

    def test_kinds_in_python_mongo_validator_and_nothing_else_disagree(self):
        from resalewise.mongo_setup import VALIDATORS, INDEXES
        enum = VALIDATORS["amenities"]["$jsonSchema"]["properties"]["kind"]["enum"]
        self.assertEqual(sorted(enum), sorted(am.KIND_NAMES))
        geo_indexes = [o for k, o in INDEXES["amenities"] if any(t == "2dsphere" for _, t in k)]
        self.assertEqual(len(geo_indexes), 1)            # a second geo index would make $geoNear ambiguous

    def test_sql_and_python_agree_on_what_a_kind_is(self):
        self.assertIn("kind ~ '^[a-z][a-z_]*$'", self.sql)
        for k in am.KIND_NAMES:
            self.assertRegex(k, r"^[a-z][a-z_]*$")


if __name__ == "__main__":
    unittest.main()

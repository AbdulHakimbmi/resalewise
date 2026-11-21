"""Unit tests for the geospatial matching and distance helpers (no network,
no database)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from resalewise.geo import (R_MONGO, destination, drop_excluded, haversine_m, in_sg, match_onemap,  # noqa: E402
                            mrt_band, norm_street, parse_station)


def hit(blk, road, lat=1.35, lon=103.94):
    return {"BLK_NO": blk, "ROAD_NAME": road, "LATITUDE": str(lat), "LONGITUDE": str(lon),
            "ADDRESS": f"{blk} {road} SINGAPORE 520123", "POSTAL": "520123"}


class StreetNormalisation(unittest.TestCase):
    def test_abbreviations_expand(self):
        self.assertEqual(norm_street("BT BATOK WEST AVE 6"), "BUKIT BATOK WEST AVENUE 6")
        self.assertEqual(norm_street("Tampines St 11"), "TAMPINES STREET 11")
        self.assertEqual(norm_street("C'WEALTH CRES"), "COMMONWEALTH CRESCENT")

    def test_saint_is_not_street(self):
        self.assertEqual(norm_street("ST. GEORGE'S LANE"), norm_street("ST GEORGES LANE"))
        self.assertEqual(norm_street("SAINT GEORGE'S LANE"), norm_street("ST. GEORGE'S LANE"))   # OneMap's spelling
        self.assertTrue(norm_street("ST. GEORGE'S LANE").startswith("ST "))


class OneMapMatching(unittest.TestCase):
    def test_exact_match_survives_abbreviation(self):
        q, r, _ = match_onemap("123", "TAMPINES STREET 11", [hit("123", "TAMPINES ST 11")])
        self.assertEqual(q, "exact")
        self.assertEqual(r["POSTAL"], "520123")

    def test_never_accepts_a_different_block_number(self):
        q, r, _ = match_onemap("123", "TAMPINES STREET 11", [hit("123A", "TAMPINES STREET 11"),
                                                             hit("12", "TAMPINES STREET 11")])
        self.assertEqual((q, r), ("unmatched", None))

    def test_first_exact_wins_over_earlier_fuzzy(self):
        q, r, _ = match_onemap("5", "JALAN BUKIT MERAH", [hit("5", "JALAN BUKIT MERAX", lat=1.28),
                                                          hit("5", "JALAN BUKIT MERAH", lat=1.29)])
        self.assertEqual(q, "exact")
        self.assertEqual(r["LATITUDE"], "1.29")

    def test_similar_road_is_fuzzy_dissimilar_is_unmatched(self):
        self.assertEqual(match_onemap("5", "JALAN BUKIT MERAH", [hit("5", "JALAN BUKIT MERAX")])[0], "fuzzy")
        self.assertEqual(match_onemap("5", "JALAN BUKIT MERAH", [hit("5", "YISHUN RING ROAD")])[0], "unmatched")

    def test_different_road_number_is_never_fuzzy(self):
        # Found by the hand check (V3): "321 CLEMENTI AVENUE 3" is not "321 CLEMENTI AVENUE 5".
        for want, got in (("CLEMENTI AVENUE 5", "CLEMENTI AVENUE 3"),
                          ("ANG MO KIO AVENUE 1", "ANG MO KIO AVENUE 2"),
                          ("SELETAR WEST FARMWAY 6", "SELETAR WEST FARMWAY 9")):
            with self.subTest(want=want):
                self.assertEqual(match_onemap("1", want, [hit("1", got)])[0], "unmatched")
        self.assertEqual(match_onemap("1", "CLEMENTI AVENUE 5", [hit("1", "CLEMENTI AVE 5")])[0], "exact")

    def test_points_outside_singapore_are_ignored(self):
        self.assertEqual(match_onemap("1", "X ROAD", [hit("1", "X ROAD", lat=40.7, lon=-74.0)])[0], "unmatched")

    def test_old_misspelt_longitude_key(self):
        r = {"BLK_NO": "1", "ROAD_NAME": "X ROAD", "LATITUDE": "1.3", "LONGTITUDE": "103.8"}
        self.assertEqual(match_onemap("1", "X ROAD", [r])[0], "exact")

    def test_no_results(self):
        self.assertEqual(match_onemap("1", "X ROAD", [])[0], "unmatched")


class Stations(unittest.TestCase):
    def test_parse_interchange(self):
        self.assertEqual(parse_station("DHOBY GHAUT MRT STATION (NS24 / NE6 / CC1)"),
                         ("DHOBY GHAUT", "mrt", ["NS24", "NE6", "CC1"]))

    def test_exit_rows_group_with_station(self):
        self.assertEqual(parse_station("TAMPINES MRT STATION EXIT B")[:2], ("TAMPINES", "mrt"))
        self.assertEqual(parse_station("SENJA LRT STATION (BP13)"), ("SENJA", "lrt", ["BP13"]))
        self.assertIsNone(parse_station("TAMPINES BUS INTERCHANGE"))


class Distances(unittest.TestCase):
    def test_destination_round_trip_on_mongo_sphere(self):
        for bearing in (0, 45, 90, 180, 270):
            lon, lat = destination(103.85, 1.30, bearing, 999.0, R_MONGO)
            self.assertAlmostEqual(haversine_m(103.85, 1.30, lon, lat, R_MONGO), 999.0, places=3)

    def test_bands_and_bbox(self):
        self.assertEqual([mrt_band(d) for d in (0, 500, 500.1, 1000, 1000.1)],
                         ["<=500m", "<=500m", "500m-1km", "500m-1km", ">1km"])
        self.assertTrue(in_sg(1.35, 103.9))
        self.assertFalse(in_sg(103.9, 1.35))       # swapped order is caught


class OpeningYears(unittest.TestCase):
    """The reference file that lets the price analysis ignore stations that were
    not yet open at the time of a sale."""

    @classmethod
    def setUpClass(cls):
        import csv
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "reference", "station_opening_years.csv")
        with open(path, encoding="utf-8") as fh:
            cls.rows = list(csv.DictReader(fh))

    def test_not_empty_and_unique(self):
        codes = [r["code"] for r in self.rows]
        self.assertGreater(len(codes), 150)
        self.assertEqual(len(codes), len(set(codes)))

    def test_codes_and_years_are_sane(self):
        import re
        for r in self.rows:
            self.assertRegex(r["code"], r"^(NS|EW|CG|NE|CC|CE|DT|TE|BP|SE|SW|PE|PW|S)\d{1,2}$")
            self.assertTrue(1987 <= int(r["opened_year"]) <= 2026, r)

    def test_known_openings(self):
        year = {r["code"]: int(r["opened_year"]) for r in self.rows}
        self.assertEqual(year["NS24"], 1987)      # Dhoby Ghaut, one of the first stations
        self.assertEqual(year["NE18"], 2024)      # Punggol Coast
        self.assertEqual(year["DT35"], 2017)      # Expo (Downtown Line)
        self.assertEqual(year["CG1"], 2001)       # Expo (East-West Line)
        self.assertEqual(year["TE14"], 2022)      # Orchard (Thomson-East Coast Line)


class ExcludedStations(unittest.TestCase):
    def test_non_stations_and_unbuilt_stations_are_dropped_everything_else_kept(self):
        pts = {("TAMPINES", "mrt"): [1], ("POPEYES WOODLANDS", "mrt"): [2], ("MOUNT PLEASANT", "mrt"): [3],
               ("PUNGGOL", "lrt"): [4]}
        kept, dropped = drop_excluded(pts, {("POPEYES WOODLANDS", "mrt"), ("MOUNT PLEASANT", "mrt")})
        self.assertEqual(sorted(kept), [("PUNGGOL", "lrt"), ("TAMPINES", "mrt")])
        self.assertEqual(dropped, [("MOUNT PLEASANT", "mrt"), ("POPEYES WOODLANDS", "mrt")])

    def test_kind_matters_so_an_lrt_station_with_the_same_name_survives(self):
        kept, dropped = drop_excluded({("PUNGGOL", "lrt"): [1], ("PUNGGOL", "mrt"): [2]}, {("PUNGGOL", "mrt")})
        self.assertEqual(list(kept), [("PUNGGOL", "lrt")])
        self.assertEqual(dropped, [("PUNGGOL", "mrt")])


if __name__ == "__main__":
    unittest.main()


class HandCheckSample(unittest.TestCase):
    """V3: a fresh sample (no block from an earlier round) with every town in it."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("validate_geo", os.path.join(ROOT, "scripts", "validate_geo.py"))
        cls.vg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.vg)

    def test_every_town_is_in_it_and_no_earlier_block(self):
        rows = [{"block_id": i, "town_name": f"T{i % 27}"} for i in range(500)]
        sample = self.vg.draw_v3_sample(rows, exclude={0, 1, 2})
        self.assertEqual(len(sample), 60)
        self.assertEqual(len({r["town_name"] for r in sample}), 27)
        self.assertFalse({0, 1, 2} & {r["block_id"] for r in sample})

    def test_error_bound_shrinks_with_a_bigger_sample(self):
        self.assertAlmostEqual(self.vg.upper_error_bound(0, 30), 0.095, places=3)   # 30 of 30 still allows ~9.5 %
        self.assertAlmostEqual(self.vg.upper_error_bound(0, 60), 0.049, places=3)
        self.assertEqual(self.vg.upper_error_bound(30, 30), 1.0)

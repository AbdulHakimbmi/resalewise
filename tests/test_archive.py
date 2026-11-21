"""The home page's town archive (assets/js/town-archive.js): the town list in the page and the photographs it
shows. No network, no browser.

    python -m unittest discover -s tests
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_PHOTO = set()                        # a town with no picture is drawn as a coloured tape; add its name here only if that is intended


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TownArchive(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")
        self.slides = re.findall(r'<li class="archive-slide" data-town="([^"]+)" data-median="(\d+)" data-count="(\d+)"', self.html)

    def test_the_page_lists_every_town_with_its_numbers(self):
        self.assertGreaterEqual(len(self.slides), 25)
        self.assertEqual(len({s[0] for s in self.slides}), len(self.slides), "a town is listed twice")
        for town, median, count in self.slides:
            self.assertGreater(int(median), 100000, town)
            self.assertGreater(int(count), 0, town)

    def test_every_town_has_a_photograph_or_is_a_known_exception(self):
        for town, _, _ in self.slides:
            slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "", re.sub(r"\s+", "-", town.lower().replace("/", "-"))))
            has = os.path.isfile(os.path.join(ROOT, "assets", "images", "towns", slug + ".jpg"))
            self.assertTrue(has or town in NO_PHOTO, f"{town}: no assets/images/towns/{slug}.jpg")

    def test_the_towns_with_a_supplied_illustration_say_so(self):
        import json
        credits = {e["file"]: e for e in json.loads(read("assets", "images", "towns", "credits.json"))}
        for slug in ("bukit-timah", "lim-chu-kang"):
            f = f"assets/images/towns/{slug}.jpg"
            self.assertTrue(os.path.isfile(os.path.join(ROOT, f)), f)
            self.assertIn("Illustrative", credits[f]["credit"], f)          # shown on the town's sheet and on the About page

    def test_the_old_coverflow_is_gone_from_every_page(self):
        for page in glob.glob(os.path.join(ROOT, "*.html")):
            self.assertNotIn("town-coverflow", read(os.path.basename(page)), os.path.basename(page))
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "assets", "js", "town-archive.js")))
        self.assertIn("town-archive.js", self.html)

    def test_opening_a_town_goes_to_a_page_that_exists(self):
        js = read("assets", "js", "town-archive.js")
        target = re.search(r'"(town\.html)\?town="', js).group(1)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, target)))


if __name__ == "__main__":
    unittest.main()

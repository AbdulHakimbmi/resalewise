"""The loading intro (intro/ is the source, assets/intro/ the built page) and the guard on the
homepage that sends a first visit there. No network, no browser.

    python -m unittest discover -s tests
"""
import os
import re
import sys
import unittest

os.environ["RW_SKIP_STARTUP_MIGRATIONS"] = "1"
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:1")
os.environ.setdefault("TOKEN_SECRET", "unit-test-secret-not-for-production-use")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class HomepageGuard(unittest.TestCase):
    """The redirect is the part that could lock visitors out, so its safety conditions are pinned."""

    def setUp(self):
        self.html = read("index.html")
        self.guard = self.html[self.html.index("rw_intro") - 900: self.html.index("</script>", self.html.index("rw_intro"))]

    def test_it_runs_before_anything_else_in_the_head(self):
        self.assertLess(self.html.index("rw_intro"), self.html.index("<link"))

    def test_it_cannot_loop_or_trap_anyone(self):
        for needle in ("nointro", "sessionStorage", "prefers-reduced-motion", "saveData", "window.top"):
            self.assertIn(needle, self.guard, needle)
        # the session flag is written before we leave, so Back and reload cannot bounce into the intro again
        self.assertLess(self.guard.index("setItem('rw_intro'"), self.guard.index("location.replace"))
        self.assertIn("catch (e)", self.guard)           # anything unexpected: show the page

    def test_it_sends_people_to_a_page_that_exists(self):
        target = re.search(r"location\.replace\('([^?']+)", self.guard).group(1)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, target)), target)


class BuiltIntro(unittest.TestCase):
    def test_the_built_page_uses_relative_links_only(self):
        html = read("assets", "intro", "index.html")
        for url in re.findall(r'(?:src|href)="([^"]+)"', html):
            self.assertFalse(url.startswith("/") or url.startswith("http"), url)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "assets", "intro", url)) or url.startswith("../"), url)

    def test_the_noscript_fallback_gets_you_into_the_site(self):
        self.assertIn("index.html?nointro=1", read("assets", "intro", "index.html"))


class IntroLoading(unittest.TestCase):
    def test_every_url_it_loads_is_a_real_api_route(self):
        import app as appmod
        adapter = appmod.app.url_map.bind("localhost")
        urls = re.findall(r"url: '([^']+)'", read("intro", "src", "loader.js"))
        self.assertGreaterEqual(len(urls), 5)
        for url in urls:
            path = url.split("?")[0]
            self.assertTrue(path.startswith("/api/"), url)
            adapter.match(path, method="GET")             # raises NotFound if no such route

    def test_next_page_is_restricted_to_a_plain_page_name(self):
        src = read("intro", "src", "loader.js")
        self.assertIn(r"/^[A-Za-z0-9_-]+\.html$/", src)   # no slashes, no schemes: it cannot send anyone elsewhere
        self.assertIn("nointro=1", src)


class IntroScenes(unittest.TestCase):
    def test_every_scene_photo_exists_and_carries_a_clean_credit(self):
        import json
        credits = {}
        for folder in ("towns", "home"):
            for e in json.loads(read("assets", "images", folder, "credits.json")):
                credits[e["file"]] = e["credit"]
        files = re.findall(r"photo\('([^']+)'\)", read("intro", "src", "scenes.js"))
        self.assertGreaterEqual(len(files), 5)
        for f in files:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "assets", "images", f)), f)
            credit = credits.get("assets/images/" + f, "")
            self.assertTrue(credit, f"no credit for {f}")
            self.assertNotIn("â€", credit, f"damaged characters in the credit of {f}")      # a double-encoded ellipsis once got in


if __name__ == "__main__":
    unittest.main()

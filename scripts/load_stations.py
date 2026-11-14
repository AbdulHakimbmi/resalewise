"""Step 2 of the geospatial pipeline: load MRT/LRT stations.

    python scripts/load_stations.py --geojson data/raw/LTAMRTStationExitGEOJSON.geojson
    python scripts/load_stations.py --onemap              # no download needed
    python scripts/load_stations.py --csv my_stations.csv # name,kind,codes,lat,lon

Sources, best first:
  --geojson  LTA "MRT Station Exit" GeoJSON from data.gov.sg (one point per
             exit). Exits are grouped into one MultiPoint per station, so a
             block's distance to a station is the distance to its nearest exit.
  --onemap   OneMap search for "MRT STATION" / "LRT STATION" (station
             buildings and exits OneMap knows about), grouped the same way.
  --csv      a hand-made list, one row per point.

Optional: data/reference/station_opening_years.csv (filled in, 221 codes) with columns
code,opened_year attaches opening years (used by validation check V5 and
to exclude not-yet-open stations in the price-driver analysis).

Writes MongoDB `places` (2dsphere index) and PostgreSQL `mrt_stations`.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from resalewise import config, db  # noqa: E402
from resalewise.geo import drop_excluded, in_sg, parse_station, slug  # noqa: E402
from resalewise.mongo_setup import ensure_collection  # noqa: E402

ONEMAP_BASE = os.environ.get("ONEMAP_BASE", "https://www.onemap.gov.sg")
OPENING = os.path.join(config.ROOT, "data", "reference", "station_opening_years.csv")
EXCLUSIONS = os.path.join(config.ROOT, "data", "reference", "station_exclusions.csv")


def _props_name(props):
    """LTA files have used different property layouts over the years: a
    STATION_NA field, or an HTML table inside 'Description'."""
    for k, v in props.items():
        if k.upper() in ("STATION_NA", "STATION_NAME", "STN_NAME", "NAME") and v:
            return str(v)
    desc = props.get("Description") or props.get("description") or ""
    cells = dict(re.findall(r"<th>\s*(.*?)\s*</th>\s*<td>\s*(.*?)\s*</td>", desc, re.S | re.I))
    for k in ("STATION_NA", "STATION_NAME"):
        if cells.get(k):
            return cells[k]
    return None


def from_geojson(path):
    with open(path, encoding="utf-8") as fh:
        gj = json.load(fh)
    pts = defaultdict(list)
    for f in gj.get("features", []):
        name = _props_name(f.get("properties") or {})
        geom = f.get("geometry") or {}
        if not name or geom.get("type") != "Point":
            continue
        lon, lat = geom["coordinates"][:2]
        parsed = parse_station(name if "STATION" in name.upper() else name + " MRT STATION")
        if parsed and in_sg(lat, lon):
            pts[(parsed[0], parsed[1])].append((lon, lat, tuple(parsed[2])))
    return pts, "lta-geojson"


def _get_json(req, retries=8):
    """GET with patient back-off: OneMap answers 429 (too many requests) when it is
    busy or when this address has just made many calls; waiting a little fixes it."""
    delay = 5.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                print(f"\n  OneMap said {e.code}; waiting {delay:.0f}s and retrying ({attempt + 1}/{retries - 1})", flush=True)
                time.sleep(delay); delay = min(delay * 2, 120)
                continue
            raise SystemExit(f"\nOneMap keeps answering HTTP {e.code}. Wait 10 minutes and run the same command again, "
                             "or use --geojson with the LTA file from data.gov.sg (see USER_MANUAL.md 5.2).")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(delay); delay = min(delay * 2, 120)
                continue
            raise SystemExit(f"\nCannot reach OneMap ({e}). Check your internet connection and run the command again.")


def from_onemap(pace=1.5):
    pts = defaultdict(list)
    for term in ("MRT STATION", "LRT STATION"):
        page, pages = 1, 1
        while page <= pages:
            q = urllib.parse.urlencode({"searchVal": term, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": page})
            req = urllib.request.Request(f"{ONEMAP_BASE}/api/common/elastic/search?{q}",
                                         headers={"User-Agent": "ResaleWiseSG/1.0 (student project)"})
            body = _get_json(req)
            pages = int(body.get("totalNumPages") or 1)
            for r in body.get("results", []):
                parsed = parse_station(r.get("SEARCHVAL") or r.get("BUILDING"))
                lat, lon = float(r["LATITUDE"]), float(r["LONGITUDE"])
                if parsed and in_sg(lat, lon):
                    pts[(parsed[0], parsed[1])].append((lon, lat, tuple(parsed[2])))
            print(f"\r  {term}: page {page}/{pages}", end="", flush=True)
            page += 1
            time.sleep(1.0 / pace)
        print()
    return pts, "onemap-search"


def from_csv(path):
    pts = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            lat, lon = float(row["lat"]), float(row["lon"])
            if in_sg(lat, lon):
                codes = tuple(c for c in (row.get("codes") or "").replace("/", " ").split() if c)
                pts[(row["name"].strip().upper(), row.get("kind", "mrt").strip().lower())].append((lon, lat, codes))
    return pts, "csv"


def opening_years():
    years = {}
    if os.path.isfile(OPENING):
        with open(OPENING, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("code") and row.get("opened_year"):
                    years[row["code"].strip().upper()] = int(row["opened_year"])
    return years


def exclusions():
    """(NAME, kind) pairs that the search returned but are not open stations (restaurants named
    after a station, or lines announced but not built). See data/reference/station_exclusions.csv."""
    out = set()
    if os.path.isfile(EXCLUSIONS):
        with open(EXCLUSIONS, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("name"):
                    out.add((row["name"].strip().upper(), (row.get("kind") or "mrt").strip().lower()))
    return out


def build(points_by_station, source):
    years = opening_years()
    stations = []
    for (name, kind), pts in sorted(points_by_station.items()):
        uniq = sorted({(round(lon, 6), round(lat, 6)) for lon, lat, _ in pts})
        codes = sorted({c for *_, cs in pts for c in cs})
        clon = sum(p[0] for p in uniq) / len(uniq)
        clat = sum(p[1] for p in uniq) / len(uniq)
        opened = min((years[c] for c in codes if c in years), default=None)
        loc = ({"type": "Point", "coordinates": list(uniq[0])} if len(uniq) == 1
               else {"type": "MultiPoint", "coordinates": [list(p) for p in uniq]})
        stations.append({"_id": slug(name, kind), "name": name.title(), "kind": kind, "codes": codes,
                         "loc": loc, "centroid": {"type": "Point", "coordinates": [clon, clat]},
                         "n_points": len(uniq), "opened_year": opened, "source": source})
    return stations


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--geojson")
    src.add_argument("--onemap", action="store_true")
    src.add_argument("--csv")
    args = ap.parse_args()

    if args.geojson:
        pts, source = from_geojson(args.geojson)
    elif args.onemap:
        pts, source = from_onemap()
    else:
        pts, source = from_csv(args.csv)
    pts, dropped = drop_excluded(pts, exclusions())
    if dropped:
        print(f"Left out {len(dropped)} search hits that are not open stations "
              "(data/reference/station_exclusions.csv): " + ", ".join(f"{n.title()} ({k})" for n, k in dropped))
    stations = build(pts, source)
    if not stations:
        sys.exit("No stations found in that source; nothing was changed.")

    coll = ensure_collection(db.mongo(), "places")
    coll.delete_many({})
    coll.insert_many(stations)

    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    with conn, conn.cursor() as cur:
        with open(os.path.join(config.ROOT, "sql", "geo.sql"), encoding="utf-8") as fh:
            cur.execute(fh.read())
        cur.execute("UPDATE block_features SET nearest_station_id = NULL")
        cur.execute("DELETE FROM mrt_stations")
        psycopg2.extras.execute_values(cur, """
          INSERT INTO mrt_stations (station_id, name, kind, codes, opened_year, n_points, geog, centroid, source)
          VALUES %s""", [
            (s["_id"], s["name"], s["kind"], s["codes"], s["opened_year"], s["n_points"],
             json.dumps(s["loc"]), json.dumps(s["centroid"]), s["source"]) for s in stations],
            template="(%s, %s, %s, %s, %s, %s, ST_GeomFromGeoJSON(%s)::geography, "
                     "ST_GeomFromGeoJSON(%s)::geography, %s)")
    conn.close()
    n_pts = sum(s["n_points"] for s in stations)
    print(f"Loaded {len(stations)} stations ({n_pts} points, source: {source}) into MongoDB "
          "`places` and PostgreSQL `mrt_stations`.")
    odd = [s["name"] for s in stations if s["kind"] == "mrt" and not s["codes"]]
    if odd:
        print("CHECK: MRT stations with no station code (could be a shop or a building, not a station): "
              + ", ".join(odd) + ". If one is not a station, add it to data/reference/station_exclusions.csv.")
    no_year = [s for s in stations if s["opened_year"] is None]
    print(f"Opening year known for {len(stations) - len(no_year)} of {len(stations)} stations.")
    if no_year:
        print("No opening year (treated as already open; add their codes to "
              "data/reference/station_opening_years.csv and rerun this script):")
        for s in no_year:
            print(f"  - {s['name']} ({s['kind']}) codes: {', '.join(s['codes']) or 'none'}")
    print("Next: python scripts/build_block_features.py")


if __name__ == "__main__":
    main()

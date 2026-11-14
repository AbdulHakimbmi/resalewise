"""Load nearby amenities (parks, hawker centres, gyms, childcare, schools).

    python scripts/load_amenities.py --kind park        --geojson data/raw/NParksParksandNatureReserves.geojson
    python scripts/load_amenities.py --kind hawker      --geojson data/raw/HawkerCentresGEOJSON.geojson
    python scripts/load_amenities.py --kind gym         --geojson data/raw/GymsSGGEOJSON.geojson
    python scripts/load_amenities.py --kind childcare   --geojson data/raw/ChildCareServices.geojson
    python scripts/load_amenities.py --schools          --csv "data/raw/General information of schools.csv"
    python scripts/load_amenities.py --kind hawker      --csv my_places.csv      # columns: name,lat,lon
    python scripts/load_amenities.py --kind park --geojson FILE --dry-run        # check the file only

Where to get the files (all free, data.gov.sg; search the dataset name):
  parks          "NParks Parks and Nature Reserves" (GeoJSON)
  hawker centres "Hawker Centres" (GeoJSON, NEA)
  gyms           "Gyms SG" (GeoJSON, SportSG)
  childcare      "Child Care Services" (GeoJSON, ECDA)
  schools        "General information of schools" (CSV, MOE). It has postal codes, not
                 coordinates, so each postal code is looked up on OneMap (about 350 calls,
                 a few minutes). Answers are saved in data/geo/amenity_postal_cache.jsonl,
                 so a second run is instant and an interrupted run carries on.

Each run REPLACES the amenities of the kinds it loads (and only those), in MongoDB
`amenities` and PostgreSQL `amenities`, so it is safe to run again. Afterwards:
    python scripts/build_amenity_features.py
"""
import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resalewise import amenities as am  # noqa: E402
from resalewise import config  # noqa: E402
from resalewise.geo import in_sg  # noqa: E402

POSTAL_CACHE = os.path.join(config.ROOT, "data", "geo", "amenity_postal_cache.jsonl")


# --------------------------------------------------------------------------- reading
def from_geojson(path, kind, name_field):
    with open(path, encoding="utf-8") as fh:
        gj = json.load(fh)
    groups, info = am.parse_geojson(gj, kind, name_field)
    print(f"  {info['features']} features read; {info['no_geometry']} without a usable shape, "
          f"{info['outside_sg']} outside Singapore, {info['unnamed']} without a name.")
    if info["unnamed"] and info["unnamed"] >= max(3, info["features"] // 2):
        print("  Most features had no name I recognise. Fields in the file: "
              + ", ".join(info["fields_seen"]) + "\n  Re-run with --name-field FIELD to pick the right one.")
    return {kind: groups}, "geojson:" + os.path.basename(path)


def from_places_csv(path, kind):
    groups = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        name_c, lat_c, lon_c = (am.find_column(cols, a) for a in (("name",), ("lat", "latitude"), ("lon", "lng", "longitude")))
        if not (name_c and lat_c and lon_c):
            sys.exit(f"{path}: need columns name, lat, lon (found: {', '.join(cols)}).")
        for i, row in enumerate(reader):
            try:
                lat, lon = float(row[lat_c]), float(row[lon_c])
            except (TypeError, ValueError):
                continue
            if in_sg(lat, lon):
                name = (row[name_c] or "").strip() or f"{am.KIND_BY_NAME[kind].label} {i + 1}"
                groups.setdefault((name, f"{name.upper()}|{lon:.5f},{lat:.5f}"), []).append((lon, lat))
    return {kind: groups}, "csv:" + os.path.basename(path)


def load_postal_cache():
    cache = {}
    if os.path.isfile(POSTAL_CACHE):
        with open(POSTAL_CACHE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    cache[r["postal"]] = (r["lat"], r["lon"]) if r.get("lat") is not None else None
    return cache


def from_schools_csv(path, pace=1.0):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        c = {k: am.find_column(cols, v) for k, v in am.SCHOOL_COLUMNS.items()}
        missing = [k for k, v in c.items() if v is None]
        if missing:
            sys.exit(f"{path}: could not find the {', '.join(missing)} column. Columns in the file: "
                     + ", ".join(cols))
        rows = list(reader)
    cache = load_postal_cache()
    todo = sorted({am.clean_postal(r[c["postal"]]) for r in rows} - {None} - set(cache))
    if todo:
        print(f"  Looking up {len(todo)} postal codes on OneMap (about {len(todo) * pace / 60:.0f} min; "
              "saved as it goes, so you can stop and run again)...")
        os.makedirs(os.path.dirname(POSTAL_CACHE), exist_ok=True)
        with open(POSTAL_CACHE, "a", encoding="utf-8") as out:
            for i, postal in enumerate(todo, 1):
                hit = am.onemap_postal(postal)
                cache[postal] = hit
                out.write(json.dumps({"postal": postal, "lat": hit[0] if hit else None,
                                      "lon": hit[1] if hit else None}) + "\n")
                out.flush()
                print(f"\r  {i}/{len(todo)}", end="", flush=True)
                time.sleep(pace)
        print()
    kinds, skipped_level, no_loc = {"primary_school": {}, "secondary_school": {}}, 0, []
    for r in rows:
        school_kind_list = am.school_kinds(r[c["level"]])
        if not school_kind_list:
            skipped_level += 1
            continue
        postal = am.clean_postal(r[c["postal"]])
        hit = cache.get(postal)
        name = (r[c["name"]] or "").strip()
        if not hit or not name:
            no_loc.append(name or postal or "?")
            continue
        lat, lon = hit
        for kind in school_kind_list:
            kinds[kind].setdefault((name, f"{name.upper()}|{postal}"), []).append((lon, lat))
    print(f"  {len(rows)} schools in the file: {skipped_level} are junior colleges / other levels (left out), "
          f"{len(no_loc)} could not be located" + (": " + ", ".join(no_loc[:8]) + (" ..." if len(no_loc) > 8 else "")
                                                   if no_loc else "") + ".")
    return kinds, "moe-schools+onemap"


# --------------------------------------------------------------------------- writing
def write_mongo(kinds_docs):
    from resalewise import db
    from resalewise.mongo_setup import ensure_collection
    coll = ensure_collection(db.mongo(), "amenities")
    coll.delete_many({"kind": {"$nin": am.KIND_NAMES}})          # kinds the project no longer has
    for kind, docs in kinds_docs.items():
        coll.delete_many({"kind": kind})
        if docs:
            coll.insert_many(docs)


def write_postgres(kinds_docs):
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    with conn, conn.cursor() as cur:
        with open(os.path.join(config.ROOT, "sql", "geo.sql"), encoding="utf-8") as fh:
            cur.execute(fh.read())
        with open(os.path.join(config.ROOT, "sql", "amenities.sql"), encoding="utf-8") as fh:
            cur.execute(fh.read())
        psycopg2.extras.execute_values(cur, """
          INSERT INTO amenity_kinds (kind, label, plural, full_m, zero_m, sort_order) VALUES %s
          ON CONFLICT (kind) DO UPDATE SET label = EXCLUDED.label, plural = EXCLUDED.plural,
            full_m = EXCLUDED.full_m, zero_m = EXCLUDED.zero_m, sort_order = EXCLUDED.sort_order""",
                                       [(k.kind, k.label, k.plural, k.full_m, k.zero_m, k.sort_order) for k in am.KINDS])
        cur.execute("SELECT kind FROM amenity_kinds")
        for old in am.retired_kinds(r[0] for r in cur.fetchall()):    # features, places, then the kind itself
            cur.execute("DELETE FROM block_amenity_features WHERE kind = %s", [old])
            cur.execute("DELETE FROM amenities WHERE kind = %s", [old])
            cur.execute("DELETE FROM amenity_kinds WHERE kind = %s", [old])
        for kind, docs in kinds_docs.items():
            cur.execute("DELETE FROM block_amenity_features WHERE kind = %s", [kind])
            cur.execute("DELETE FROM amenities WHERE kind = %s", [kind])
            if docs:
                psycopg2.extras.execute_values(cur, """
                  INSERT INTO amenities (amenity_id, kind, name, n_points, geog, centroid, source) VALUES %s""",
                                               [(d["_id"], kind, d["name"][:160], d["n_points"], json.dumps(d["loc"]),
                                                 json.dumps(d["centroid"]), d["source"][:48]) for d in docs],
                                               template="(%s, %s, %s, %s, ST_GeomFromGeoJSON(%s)::geography, "
                                                        "ST_GeomFromGeoJSON(%s)::geography, %s)")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=am.KIND_NAMES, help="what the file contains (not needed for --schools)")
    ap.add_argument("--schools", action="store_true", help="MOE schools CSV: primary and secondary schools")
    ap.add_argument("--geojson", help="a GeoJSON FeatureCollection")
    ap.add_argument("--csv", help="schools CSV with --schools, otherwise a CSV with columns name,lat,lon")
    ap.add_argument("--name-field", help="GeoJSON property that holds the name (if it is not guessed)")
    ap.add_argument("--dry-run", action="store_true", help="read and summarise the file; change nothing")
    args = ap.parse_args()

    if args.schools:
        if not args.csv:
            ap.error("--schools needs --csv")
        print("Schools:")
        by_kind, source = from_schools_csv(args.csv)
    else:
        if not args.kind or not (args.geojson or args.csv):
            ap.error("give --kind and --geojson (or --csv), or use --schools")
        print(f"{am.KIND_BY_NAME[args.kind].plural}:")
        by_kind, source = (from_geojson(args.geojson, args.kind, args.name_field) if args.geojson
                           else from_places_csv(args.csv, args.kind))

    docs = {kind: am.build_amenities(kind, groups, source) for kind, groups in by_kind.items()}
    for kind, d in docs.items():
        pts = sum(x["n_points"] for x in d)
        print(f"  {kind}: {len(d)} places ({pts} points)" + (f", e.g. {d[0]['name']}" if d else ""))
    if not any(docs.values()):
        sys.exit("Nothing usable was found in that file; nothing was changed.")
    if args.dry_run:
        print("Dry run: nothing written.")
        return
    write_mongo(docs)
    write_postgres(docs)
    print("Saved to MongoDB `amenities` and PostgreSQL `amenities`. "
          "Load the other kinds, then: python scripts/build_amenity_features.py")


if __name__ == "__main__":
    main()

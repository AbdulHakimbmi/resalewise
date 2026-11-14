"""What is associated with a higher price per square metre? Matched
comparisons with bootstrap confidence intervals.

    python analysis/price_drivers.py                 # 2017 onwards (default)
    python analysis/price_drivers.py --since 2020-01-01 --boot 2000

Method (the same for every driver)
----------------------------------
1. Remove time and town: each sale's price per sqm is divided by the median
   price per sqm of the SAME town, flat type and quarter (from the
   materialized view mv_town_quarter_stats). 1.00 = a typical price for that
   town, type and quarter. No external price index is needed.
2. Match: compare like with like inside strata (cells) that hold the other
   factors fixed, e.g. high vs low floors inside the SAME block and flat type.
   A cell counts only if BOTH groups have at least `min_group` sales.
3. Effect per cell = log(median of group A / median of group B); the overall
   effect is the weighted mean (weight = nA*nB/(nA+nB)), shown as a %.
4. 95 % confidence interval: bootstrap over cells (resample cells with
   replacement, B times, fixed seed) -> 2.5th / 97.5th percentiles.
5. A naive, unmatched comparison is reported next to it: the gap between the
   two shows how much the matching removes (confounding).

These are ASSOCIATIONS in past sales, not causal effects: buyers also pay
for things the data does not record (renovation, view, facing, noise), and
those can differ systematically between the groups.

Writes data/results/drivers.json and PostgreSQL table driver_effects
(read by GET /api/drivers/<txn_id>).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from resalewise import config  # noqa: E402

SQL = """
SELECT v.txn_id, v.quarter, v.sale_year, v.town_name, v.block_id, v.flat_type_name,
       v.floor_area_sqm::float AS area, v.remaining_lease_years::float AS lease_years,
       v.storey_mid::float AS storey_mid, v.price_per_sqm::float AS psm,
       m.median_psm::float AS period_median_psm
       {geo_cols}
FROM v_transactions v
JOIN mv_town_quarter_stats m
  ON m.town_name = v.town_name AND m.flat_type_name = v.flat_type_name AND m.quarter = v.quarter
{geo_join}
WHERE v.month >= %s AND m.txn_count >= 5
"""

GEO_COLS = ", f.mrt_band, f.nearest_mrt_m::float AS nearest_mrt_m, s.opened_year"
GEO_JOIN = ("LEFT JOIN block_features f ON f.block_id = v.block_id "
            "LEFT JOIN mrt_stations s ON s.station_id = f.nearest_station_id")

DRIVERS = [
    {"driver": "storey", "contrast": "high (13+) vs low (1-6)",
     "label": "Living on a high floor",
     "a": lambda d: d.storey_mid >= 13, "b": lambda d: d.storey_mid <= 6,
     "strata": ["block_id", "flat_type_name"], "min_group": 10,
     "why": "Same block and flat type, so location, block age and estate are identical."},
    {"driver": "lease", "contrast": "80+ years vs 60-70 years left",
     "label": "Having more lease left",
     "a": lambda d: d.lease_years >= 80, "b": lambda d: (d.lease_years >= 60) & (d.lease_years < 70),
     "strata": ["town_name", "flat_type_name", "area_band"], "min_group": 30,
     "why": "Same town, flat type and 10 sqm size band. Newer blocks also differ in design and "
            "location within the town, so this mixes lease with block age."},
    {"driver": "mrt", "contrast": "<=500 m vs >1 km from MRT/LRT",
     "label": "Being within 500 m of a station",
     "a": lambda d: d.mrt_band == "<=500m", "b": lambda d: d.mrt_band == ">1km",
     "strata": ["town_name", "flat_type_name", "area_band", "lease_band"], "min_group": 30, "needs_geo": True,
     "why": "Same town, flat type, size band and lease band. Stations sit next to town centres, "
            "so this also picks up being close to malls and hawker centres."},
    {"driver": "mrt", "contrast": "500 m-1 km vs >1 km from MRT/LRT",
     "label": "Being 500 m - 1 km from a station",
     "a": lambda d: d.mrt_band == "500m-1km", "b": lambda d: d.mrt_band == ">1km",
     "strata": ["town_name", "flat_type_name", "area_band", "lease_band"], "min_group": 30, "needs_geo": True,
     "why": "As above, for the middle distance band."},
]


def estimate(df, spec, boot, rng):
    d = df.assign(_a=spec["a"](df), _b=spec["b"](df))
    d = d[d._a | d._b]
    if d.empty:
        return None
    g = d.groupby(spec["strata"] + ["_a"], observed=True)["rel"].agg(["median", "size"]).reset_index()
    a = g[g._a].drop(columns="_a").rename(columns={"median": "ma", "size": "na"})
    b = g[~g._a].drop(columns="_a").rename(columns={"median": "mb", "size": "nb"})
    cells = a.merge(b, on=spec["strata"])
    cells = cells[(cells.na >= spec["min_group"]) & (cells.nb >= spec["min_group"])]
    if len(cells) < 5:
        return {"n_strata": int(len(cells)), "too_few_strata": True}
    lr = np.log(cells.ma.to_numpy() / cells.mb.to_numpy())
    w = (cells.na * cells.nb / (cells.na + cells.nb)).to_numpy()
    est = float(np.sum(w * lr) / np.sum(w))
    idx = rng.integers(0, len(cells), size=(boot, len(cells)))
    bs = np.sum(w[idx] * lr[idx], axis=1) / np.sum(w[idx], axis=1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    naive = float(np.log(d[d._a].rel.median() / d[d._b].rel.median()))
    pct = lambda x: round(float(100 * (np.exp(x) - 1)), 2)   # noqa: E731
    return {"effect_pct": pct(est), "ci_low": pct(lo), "ci_high": pct(hi), "naive_pct": pct(naive),
            "n_strata": int(len(cells)), "n_sales": int(cells.na.sum() + cells.nb.sum()),
            "share_strata_positive": round(float(np.mean(lr > 0)), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2017-01-01", help="first sale month included (YYYY-MM-DD)")
    ap.add_argument("--boot", type=int, default=2000, help="bootstrap resamples")
    ap.add_argument("--seed", type=int, default=2003)
    ap.add_argument("--skip-geo", action="store_true", help="ignore block_features even if present")
    args = ap.parse_args()

    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    has_geo = False
    if not args.skip_geo:
        with conn.cursor() as cur:
            # Check the tables exist before touching them (a query naming a
            # missing table fails at parse time, before any AND short-circuits).
            cur.execute("SELECT to_regclass('block_features') IS NOT NULL AND to_regclass('mrt_stations') IS NOT NULL")
            if cur.fetchone()[0]:
                cur.execute("SELECT EXISTS (SELECT 1 FROM block_features)")
                has_geo = bool(cur.fetchone()[0])
        conn.rollback()
    sql = SQL.format(geo_cols=GEO_COLS if has_geo else "", geo_join=GEO_JOIN if has_geo else "")
    with conn.cursor() as cur:
        cur.execute(sql, [args.since])
        cols = [c.name for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    print(f"{len(df):,} sales since {args.since} (geo features: {'yes' if has_geo else 'no'})")

    df["rel"] = df.psm / df.period_median_psm
    df["area_band"] = (df.area // 10).astype(int)
    df["lease_band"] = (df.lease_years // 10).astype(int)
    excluded_not_open = 0
    if has_geo:
        not_open = df.opened_year.notna() & (df.opened_year > df.sale_year)
        excluded_not_open = int(not_open.sum())
        df.loc[not_open, "mrt_band"] = None           # station not open yet at the time of sale

    rng = np.random.default_rng(args.seed)
    results = []
    for spec in DRIVERS:
        row = {k: spec[k] for k in ("driver", "contrast", "label", "min_group", "why")}
        row["strata"] = spec["strata"]
        if spec.get("needs_geo") and not has_geo:
            row["status"] = "needs geospatial pipeline (scripts/geocode_blocks.py ...)"
            results.append(row)
            print(f"  {spec['label']:38s} skipped (no geo features yet)")
            continue
        est = estimate(df, spec, args.boot, rng)
        row.update(est or {"status": "no data"})
        if est and not est.get("too_few_strata"):
            row["status"] = "ok"
            print(f"  {spec['label']:38s} {est['effect_pct']:+6.2f} %  [{est['ci_low']:+.2f}, {est['ci_high']:+.2f}]  "
                  f"naive {est['naive_pct']:+.2f} %  ({est['n_strata']} strata, {est['n_sales']:,} sales)")
        else:
            row["status"] = "too few matched strata"
        results.append(row)

    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "since": args.since, "sales_considered": int(len(df)), "bootstrap": args.boot, "seed": args.seed,
           "geo_features": has_geo, "excluded_station_not_open": excluded_not_open,
           "outcome": "price per sqm relative to the median of the same town, flat type and quarter",
           "drivers": results,
           "caveat": "Associations in historical sales, not causal effects."}
    os.makedirs(os.path.join(config.ROOT, "data", "results"), exist_ok=True)
    with open(os.path.join(config.ROOT, "data", "results", "drivers.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    with conn, conn.cursor() as cur:
        cur.execute("""
          CREATE TABLE IF NOT EXISTS driver_effects (
            driver      VARCHAR(16)  NOT NULL,
            contrast    VARCHAR(64)  NOT NULL,
            effect_pct  NUMERIC(7,2) NOT NULL,
            ci_low      NUMERIC(7,2) NOT NULL,
            ci_high     NUMERIC(7,2) NOT NULL,
            naive_pct   NUMERIC(7,2),
            n_strata    INT NOT NULL CHECK (n_strata > 0),
            n_sales     INT NOT NULL CHECK (n_sales > 0),
            method      TEXT NOT NULL,
            computed_at TIMESTAMP NOT NULL DEFAULT now(),
            PRIMARY KEY (driver, contrast),
            CHECK (ci_low <= ci_high)
          )""")
        cur.execute("DELETE FROM driver_effects")
        rows = [(r["driver"], r["contrast"], r["effect_pct"], r["ci_low"], r["ci_high"], r["naive_pct"],
                 r["n_strata"], r["n_sales"],
                 f"Matched within {' x '.join(r['strata'])}; >= {r['min_group']} sales per group; "
                 f"bootstrap over strata (B={args.boot}). {r['why']}")
                for r in results if r.get("status") == "ok"]
        if rows:
            psycopg2.extras.execute_values(cur, "INSERT INTO driver_effects (driver, contrast, effect_pct, ci_low, "
                                           "ci_high, naive_pct, n_strata, n_sales, method) VALUES %s", rows)
    conn.close()
    print("Wrote data/results/drivers.json and table driver_effects.")


if __name__ == "__main__":
    main()

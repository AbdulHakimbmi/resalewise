"""Does knowing what is near a block help predict what a flat sells for?

    python analysis/amenity_model.py
    python analysis/amenity_model.py --since 2019-01-01 --test-months 12 --skip-premium

Question and target
-------------------
Each sale's price per sqm is compared with the market level of its own quarter:
    y = log( price per sqm / median price per sqm of the same flat type in the same quarter, all towns )
This removes inflation (a 1995 sale and a 2025 sale are on the same footing) and
leaves what the model is meant to learn: how town, flat features and surroundings
move a price up or down relative to the market of that moment.

Models (gradient boosted trees, scikit-learn HistGradientBoostingRegressor)
---------------------------------------------------------------------------
  attributes        town, flat type, flat model, floor area, storey, remaining lease
  + MRT             ... and distance to the nearest MRT/LRT station
  + amenities       ... and distance to / number within 1 km of parks, hawker centres,
                        gyms, childcare centres and schools (no MRT)
  + MRT + amenities everything

Honest evaluation: the models are trained on older sales and scored on the LATEST
months (default 12), which they never saw, so the numbers say how well they would
have priced new sales. Fixed settings, no tuning on the test months. The error is
the mean absolute percentage error (MAPE) of the predicted price per sqm; the lift
of a model is how much smaller its MAPE is than the attributes-only model's.

Which surroundings matter most: grouped permutation importance on the test months
(shuffle one group of columns, e.g. everything about parks, and see how much worse
the error gets).

Location premium per block (optional, writes block_location_premium): with the full
model refit on all sales, each recent sale is priced twice, once with its block's real
surroundings and once with those of a typical block (the median of every feature).
The average difference over a block's recent sales is its premium.

What this does NOT show
-----------------------
These are ASSOCIATIONS in past sales, not causes: buyers also pay for things the data
does not record (renovation, view, facing, noise), and a park or school near a block
also tells the model about the neighbourhood. Surroundings are as of today's map, so a
park opened after a sale still counts for it. Straight-line distances, not walking routes.

Writes data/results/amenity_model.json (served by GET /api/results/amenity-model) and,
unless --skip-premium, the table block_location_premium.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BASE_NUM = ["area", "storey", "lease_years"]
BASE_CAT = ["town", "flat_type", "flat_model"]
MRT_COLS = ["nearest_mrt_m"]
MODEL_DEFS = [
    ("attributes", "Flat attributes + town", False, False),
    ("mrt", "+ distance to MRT/LRT", True, False),
    ("amenities", "+ nearby amenities", False, True),
    ("full", "+ MRT and amenities", True, True),
]
HGB_PARAMS = dict(max_iter=300, learning_rate=0.1, max_leaf_nodes=48, min_samples_leaf=40,
                  l2_regularization=1.0, early_stopping=False)


KIND_NAMES = {"park": "parks", "hawker": "hawker centres", "gym": "gyms", "childcare": "childcare centres",
              "primary_school": "primary schools", "secondary_school": "secondary schools"}


def amenity_label(kinds):
    """Name the amenity feature set after the kinds actually loaded, never after ones that are not."""
    names = [KIND_NAMES.get(k, k) for k in kinds]
    return "+ " + (", ".join(names) if len(names) <= 3 else f"{len(names)} kinds of nearby places")


def amenity_cols(kinds):
    return [c for k in kinds for c in (f"{k}_m", f"{k}_n1k")]


# --------------------------------------------------------------------------- data
def add_target(df):
    """y = log(price per sqm / median of the same flat type and quarter)."""
    d = df.copy()
    d["quarter"] = d["month"].dt.to_period("Q")
    med = d.groupby(["flat_type", "quarter"])["psm"].transform("median")
    d["y"] = np.log(d["psm"] / med)
    return d


def mask_unopened_stations(df):
    """A station that opened after the sale cannot have raised its price: treat as missing."""
    d = df.copy()
    if "opened_year" in d.columns:
        late = d["opened_year"].notna() & (d["opened_year"] > d["month"].dt.year)
        d.loc[late, "nearest_mrt_m"] = np.nan
    return d


def time_split(df, test_months):
    """Train on everything before the last `test_months` months, test on those."""
    last = df["month"].max()
    cutoff = last - pd.DateOffset(months=test_months)
    return df[df["month"] <= cutoff], df[df["month"] > cutoff]


# --------------------------------------------------------------------------- features
def fit_categories(train, cat_cols):
    return {c: sorted(train[c].dropna().unique().tolist()) for c in cat_cols}


def design_matrix(df, num_cols, cat_cols, cats):
    """Numeric columns as they are; categories as integer codes (unseen -> NaN, i.e. missing)."""
    parts, mask = [], []
    for c in num_cols:
        parts.append(df[c].to_numpy(dtype=float))
        mask.append(False)
    for c in cat_cols:
        lut = {v: i for i, v in enumerate(cats[c])}
        parts.append(df[c].map(lut).to_numpy(dtype=float))
        mask.append(True)
    return np.column_stack(parts), np.array(mask)


def feature_lists(kinds, use_mrt, use_amen):
    num = list(BASE_NUM) + (MRT_COLS if use_mrt else []) + (amenity_cols(kinds) if use_amen else [])
    return num, list(BASE_CAT)


def fit_model(train, num, cat, seed):
    from sklearn.ensemble import HistGradientBoostingRegressor
    cats = fit_categories(train, cat)
    X, mask = design_matrix(train, num, cat, cats)
    model = HistGradientBoostingRegressor(categorical_features=mask, random_state=seed, **HGB_PARAMS)
    model.fit(X, train["y"].to_numpy())
    return model, cats


def predict(model, cats, df, num, cat):
    X, _ = design_matrix(df, num, cat, cats)
    return model.predict(X)


# --------------------------------------------------------------------------- metrics
def error_metrics(y, pred):
    """Errors on the relative-price scale (exp(pred - y) - 1 is the percentage error)."""
    y, pred = np.asarray(y, float), np.asarray(pred, float)
    ape = np.abs(np.exp(pred - y) - 1.0)
    ss_res, ss_tot = float(np.sum((y - pred) ** 2)), float(np.sum((y - y.mean()) ** 2))
    return {"mape_pct": round(100 * float(ape.mean()), 3), "median_ape_pct": round(100 * float(np.median(ape)), 3),
            "rmse_log": round(float(np.sqrt(np.mean((y - pred) ** 2))), 5),
            "r2": round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None}


def naive_prediction(train, test):
    """Predict each test sale's relative price as the median of the same town and flat type
    in the training data's last 12 months (what you would do without a model)."""
    recent = train[train["month"] > train["month"].max() - pd.DateOffset(months=12)]
    med = recent.groupby(["town", "flat_type"])["y"].median()
    overall = float(recent["y"].median())
    keys = list(zip(test["town"], test["flat_type"]))
    return np.array([med.get(k, overall) for k in keys], dtype=float)


def grouped_importance(model, cats, test, num, cat, kinds, seed, repeats=3):
    """How much worse does the error get when one group of columns is shuffled?
    Reported in percentage points of MAPE."""
    X, _ = design_matrix(test, num, cat, cats)
    y = test["y"].to_numpy()
    cols = num + cat
    pos = {c: i for i, c in enumerate(cols)}
    groups = {"floor area": ["area"], "storey": ["storey"], "remaining lease": ["lease_years"],
              "town": ["town"], "flat type": ["flat_type"], "flat model": ["flat_model"],
              "MRT/LRT distance": MRT_COLS}
    for k in kinds:
        groups[k] = [f"{k}_m", f"{k}_n1k"]
    base = error_metrics(y, model.predict(X))["mape_pct"]
    rng = np.random.default_rng(seed)
    out = []
    for name, members in groups.items():
        idx = [pos[c] for c in members if c in pos]
        if not idx:
            continue
        inc = []
        for _ in range(repeats):
            Xp = X.copy()
            perm = rng.permutation(len(Xp))
            Xp[:, idx] = Xp[perm][:, idx]
            inc.append(error_metrics(y, model.predict(Xp))["mape_pct"] - base)
        out.append({"group": name, "increase_pct_points": round(float(np.mean(inc)), 3)})
    return sorted(out, key=lambda r: -r["increase_pct_points"])


def location_premium(model, cats, sales, num, cat, location_cols, block_table, min_sales=5):
    """Per block: mean percentage difference between the model's price with the block's real
    surroundings and with those of a typical block (the median of each location column over
    all blocks). `block_table` has one row per block with the location columns."""
    typical = {c: float(block_table[c].median()) for c in location_cols}
    cf = sales.copy()
    for c, v in typical.items():
        cf[c] = v
    diff = predict(model, cats, sales, num, cat) - predict(model, cats, cf, num, cat)
    tmp = pd.DataFrame({"block_id": sales["block_id"].to_numpy(), "pct": (np.exp(diff) - 1.0) * 100.0})
    g = tmp.groupby("block_id")["pct"].agg(["mean", "size"])
    g = g[g["size"] >= min_sales]
    return pd.DataFrame({"block_id": g.index, "premium_pct": g["mean"].round(2).to_numpy(),
                         "n_sales": g["size"].astype(int).to_numpy()}).reset_index(drop=True)


# --------------------------------------------------------------------------- the experiment
def run_experiment(df, kinds, test_months, seed, repeats=3):
    """`df` needs: month, town, flat_type, flat_model, area, storey, lease_years, psm, y,
    nearest_mrt_m and {kind}_m / {kind}_n1k. Returns the results dict (no database)."""
    train, test = time_split(df, test_months)
    if len(train) < 200 or len(test) < 50:
        raise SystemExit(f"Too few sales to evaluate (train {len(train)}, test {len(test)}).")
    results = {"train_sales": int(len(train)), "test_sales": int(len(test)),
               "train_to": train["month"].max().strftime("%Y-%m"),
               "test_from": test["month"].min().strftime("%Y-%m"), "test_to": test["month"].max().strftime("%Y-%m")}
    y_test = test["y"].to_numpy()
    results["naive"] = dict(label="Median of the same town and flat type (no model)",
                            **error_metrics(y_test, naive_prediction(train, test)))
    models, fitted = [], {}
    for mid, label, use_mrt, use_amen in MODEL_DEFS:
        if use_amen and not kinds:
            continue
        if mid == "amenities":
            label = amenity_label(kinds)
        num, cat = feature_lists(kinds, use_mrt, use_amen)
        model, cats = fit_model(train, num, cat, seed)
        m = error_metrics(y_test, predict(model, cats, test, num, cat))
        models.append(dict(id=mid, label=label, n_features=len(num) + len(cat), **m))
        fitted[mid] = (model, cats, num, cat)
        print(f"  {label:42s} MAPE {m['mape_pct']:6.3f} %   R2 {m['r2']}")
    base = models[0]["mape_pct"]
    for m in models:
        m["lift_vs_attributes_pct"] = round(100 * (base - m["mape_pct"]) / base, 2) if base else None
    results["models"] = models
    best_id = "full" if "full" in fitted else models[-1]["id"]
    model, cats, num, cat = fitted[best_id]
    results["importance_model"] = best_id
    results["importance"] = grouped_importance(model, cats, test, num, cat, kinds, seed, repeats)
    return results


# --------------------------------------------------------------------------- database side
SALES_SQL = """
SELECT v.txn_id, v.month, v.block_id, v.town_name AS town, v.flat_type_name AS flat_type,
       v.flat_model_name AS flat_model, v.floor_area_sqm::float AS area, v.storey_mid::float AS storey,
       v.remaining_lease_years::float AS lease_years, v.price_per_sqm::float AS psm
       {mrt_cols}
FROM v_transactions v
{mrt_join}
WHERE v.month >= %s AND v.price_per_sqm > 0 AND v.floor_area_sqm > 0
"""
MRT_COLS_SQL = ", f.nearest_mrt_m::float AS nearest_mrt_m, s.opened_year"
MRT_JOIN_SQL = ("LEFT JOIN block_features f ON f.block_id = v.block_id "
                "LEFT JOIN mrt_stations s ON s.station_id = f.nearest_station_id")


def load_data(conn, since):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('block_amenity_features') IS NOT NULL")
        if not cur.fetchone()[0]:
            sys.exit("No amenity features yet: run scripts/load_amenities.py and "
                     "scripts/build_amenity_features.py first.")
        cur.execute("SELECT to_regclass('block_features') IS NOT NULL AND to_regclass('mrt_stations') IS NOT NULL")
        has_mrt = bool(cur.fetchone()[0])
        cur.execute(SALES_SQL.format(mrt_cols=MRT_COLS_SQL if has_mrt else "", mrt_join=MRT_JOIN_SQL if has_mrt else ""),
                    [since])
        cols = [c.name for c in cur.description]
        sales = pd.DataFrame(cur.fetchall(), columns=cols)
        cur.execute("SELECT block_id, kind, nearest_m::float, within_1km FROM block_amenity_features")
        feats = pd.DataFrame(cur.fetchall(), columns=["block_id", "kind", "nearest_m", "within_1km"])
    if feats.empty:
        sys.exit("block_amenity_features is empty: run scripts/build_amenity_features.py first.")
    kinds = sorted(feats["kind"].unique())
    wide = feats.pivot(index="block_id", columns="kind", values=["nearest_m", "within_1km"])
    wide.columns = [f"{k}_m" if a == "nearest_m" else f"{k}_n1k" for a, k in wide.columns]
    wide = wide.reset_index()
    sales["month"] = pd.to_datetime(sales["month"])
    if not has_mrt:
        sales["nearest_mrt_m"] = np.nan
    sales = sales.merge(wide, on="block_id", how="left")
    return sales, kinds, has_mrt


def write_premium(conn, premium, run_id):
    import psycopg2.extras
    with conn, conn.cursor() as cur:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql", "amenities.sql"),
                  encoding="utf-8") as fh:
            cur.execute(fh.read())
        cur.execute("DELETE FROM block_location_premium")
        psycopg2.extras.execute_values(
            cur, "INSERT INTO block_location_premium (block_id, premium_pct, n_sales, model_run) VALUES %s",
            [(int(r.block_id), float(r.premium_pct), int(r.n_sales), run_id) for r in premium.itertuples()])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2017-01-01", help="first sale month used (YYYY-MM-DD)")
    ap.add_argument("--test-months", type=int, default=12, help="the latest months held out for testing")
    ap.add_argument("--premium-months", type=int, default=24, help="recent months used for block premiums")
    ap.add_argument("--seed", type=int, default=2003)
    ap.add_argument("--skip-premium", action="store_true", help="only evaluate; do not write block premiums")
    args = ap.parse_args()
    try:
        import sklearn  # noqa: F401
    except ImportError:
        sys.exit("scikit-learn is needed: python -m pip install -r requirements-dev.txt")

    import psycopg2
    from resalewise import config
    conn = psycopg2.connect(config.database_url(), connect_timeout=15)
    sales, kinds, has_mrt = load_data(conn, args.since)
    conn.rollback()
    sales = mask_unopened_stations(add_target(sales))
    print(f"{len(sales):,} sales since {args.since}; amenity kinds: {', '.join(kinds)}; MRT feature: "
          f"{'yes' if has_mrt else 'no'}")

    results = run_experiment(sales, kinds, args.test_months, args.seed)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "since": args.since,
           "test_months": args.test_months, "seed": args.seed, "kinds": kinds, "mrt_feature": has_mrt,
           "target": "log(price per sqm / median price per sqm of the same flat type in the same quarter)",
           "method": "HistGradientBoostingRegressor, fixed settings, trained on older sales and scored on the "
                     "latest months it never saw.", **results,
           "caveat": "Associations in past sales, not causes. Surroundings are as of today's map; "
                     "straight-line distances; renovation, view and facing are not in the data."}
    print("Most important groups (MAPE points lost when shuffled):")
    for r in results["importance"][:8]:
        print(f"  {r['group']:20s} +{r['increase_pct_points']:.3f}")

    if not args.skip_premium:
        loc_cols = (MRT_COLS if has_mrt else []) + amenity_cols(kinds)
        num, cat = feature_lists(kinds, has_mrt, True)
        model, cats = fit_model(sales, num, cat, args.seed)
        last = sales["month"].max() - pd.DateOffset(months=args.premium_months)
        recent = sales[sales["month"] > last]
        blocks = sales.drop_duplicates("block_id")
        prem = location_premium(model, cats, recent, num, cat, loc_cols, blocks)
        out["premium"] = {"months": args.premium_months, "blocks": int(len(prem)),
                          "p10_pct": round(float(prem["premium_pct"].quantile(0.1)), 2),
                          "median_pct": round(float(prem["premium_pct"].median()), 2),
                          "p90_pct": round(float(prem["premium_pct"].quantile(0.9)), 2),
                          "meaning": "Model-estimated price difference versus a block with typical surroundings, "
                                     "same flat, town and quarter."}
        write_premium(conn, prem, run_id)
        print(f"Block premiums: {len(prem):,} blocks, 10th-90th percentile {out['premium']['p10_pct']:+.1f} % "
              f"to {out['premium']['p90_pct']:+.1f} %")
    conn.close()
    os.makedirs(os.path.join(config.ROOT, "data", "results"), exist_ok=True)
    with open(os.path.join(config.ROOT, "data", "results", "amenity_model.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print("Wrote data/results/amenity_model.json" + ("" if args.skip_premium else " and table block_location_premium"))


if __name__ == "__main__":
    main()

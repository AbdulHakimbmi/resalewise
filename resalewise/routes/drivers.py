"""'What goes with a higher price?': matched comparisons for one sale.

Two parts:
1. Its peers: sales of the same flat type, the same 10 sqm size band, the same
   10-year remaining-lease band and the same year. Where does this flat sit
   among them?
2. Market-wide associations estimated by analysis/price_drivers.py with
   matched strata and bootstrap confidence intervals (table driver_effects),
   shown with the level that applies to this flat.

These are associations in historical sales, not causes: flats near MRT
stations or on high floors differ in other ways too (see the method notes).
"""
from flask import Blueprint, jsonify

from .. import db
from ..web import ApiError, cache_get, cache_put, iso

bp = Blueprint("drivers", __name__)

PEERS_SQL = """
WITH me AS (
  SELECT txn_id, flat_type_name, sale_year, floor_area_sqm, remaining_lease_years,
         price_per_sqm, resale_price, storey_mid, block_id
  FROM v_transactions WHERE txn_id = %s
), peers AS (
  SELECT v.price_per_sqm, v.resale_price
  FROM v_transactions v, me
  WHERE v.flat_type_name = me.flat_type_name
    AND v.month >= make_date(me.sale_year, 1, 1) AND v.month < make_date(me.sale_year + 1, 1, 1)
    AND FLOOR(v.floor_area_sqm / 10) = FLOOR(me.floor_area_sqm / 10)
    AND FLOOR(v.remaining_lease_years / 10) = FLOOR(me.remaining_lease_years / 10)
)
SELECT me.*,
       (SELECT COUNT(*) FROM peers)::int AS peer_n,
       (SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_per_sqm))::int FROM peers) AS peer_median_psm,
       (SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY resale_price))::int FROM peers) AS peer_median_price,
       (SELECT ROUND(AVG((p.price_per_sqm < me.price_per_sqm)::int) * 100)::int FROM peers p) AS pct_peers_cheaper
FROM me
"""


def _band_storey(mid):
    if mid is None:
        return None
    return "high (13+)" if mid >= 13 else ("low (1-6)" if mid <= 6 else "middle (7-12)")


def _band_lease(years):
    if years is None:
        return None
    return "80+ years" if years >= 80 else ("60-70 years" if 60 <= years < 70 else None)


@bp.route("/api/drivers/<int:txn_id>")
def drivers(txn_id):
    key = "drivers:%d" % txn_id
    found, body = cache_get(key, 300)
    if found:
        return jsonify(body)
    if not db.has_relation("v_transactions"):
        raise ApiError("Run `python scripts/migrate.py` to create the views first.", 503, "feature_unavailable")
    rows = db.q(PEERS_SQL, [txn_id])
    if not rows:
        raise ApiError("That transaction could not be found.", 404, "not_found")
    me = rows[0]
    for k in ("floor_area_sqm", "remaining_lease_years", "price_per_sqm", "resale_price", "storey_mid"):
        if me.get(k) is not None:
            me[k] = float(me[k])

    mrt = None
    if db.has_relation("block_features"):
        f = db.q("SELECT mrt_band, nearest_mrt_m::float AS m, nearest_station_name AS station "
                 "FROM block_features WHERE block_id = %s", [me["block_id"]])
        mrt = f[0] if f else None

    effects = []
    if db.has_relation("driver_effects"):
        effects = db.q("SELECT driver, contrast, effect_pct::float, ci_low::float, ci_high::float, "
                       "n_strata, n_sales, method, computed_at FROM driver_effects ORDER BY driver, contrast")
        for e in effects:
            e["computed_at"] = iso(e["computed_at"])

    applies = {
        "storey": _band_storey(me.get("storey_mid")),
        "lease": _band_lease(me.get("remaining_lease_years")),
        "mrt": (mrt or {}).get("mrt_band"),
    }
    body = {
        "txn_id": txn_id,
        "flat": {k: me.get(k) for k in ("flat_type_name", "sale_year", "floor_area_sqm",
                                         "remaining_lease_years", "price_per_sqm", "storey_mid")},
        "peers": {
            "definition": "Same flat type, same 10 sqm size band, same 10-year lease band, same year",
            "n": me["peer_n"], "median_psm": me["peer_median_psm"],
            "median_price": me["peer_median_price"], "pct_cheaper_than_this": me["pct_peers_cheaper"],
            "enough": (me["peer_n"] or 0) >= 30,
        },
        "mrt": mrt,
        "applies": applies,
        "effects": effects,
        "caveat": "Associations in past sales, not causes. Matching holds size, lease, type and "
                  "year (and town or block) fixed, but buyers also pay for things the data does "
                  "not record, such as renovation, view and facing.",
    }
    cache_put(key, body)
    return jsonify(body)

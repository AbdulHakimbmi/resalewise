"""MongoDB equivalents of the relational search queries.

`search_filter()` takes the SAME validated filter dict as the SQL builder in
routes/search.py (parse_filters) and applies the same rules, including which
values count as "no filter", so both engines answer exactly the same question.
In MongoDB the sale document already carries town, flat type and model
(denormalised), so there are no joins: that is the data-model difference the
benchmark measures alongside the engine difference.
"""


def search_filter(f):
    m = {}
    if f.get("town"):
        m["town"] = f["town"]
    if f.get("flat_type"):
        m["flat_type"] = f["flat_type"]
    if f.get("flat_model"):
        m["flat_model"] = f["flat_model"]
    if f.get("budget"):
        m["price"] = {"$lte": f["budget"]}
    if f.get("min_lease"):
        m["remaining_lease_months"] = {"$gte": f["min_lease"] * 12}
    area = {}
    if f.get("min_area"):
        area["$gte"] = f["min_area"]
    if f.get("max_area"):
        area["$lte"] = f["max_area"]
    if area:
        m["floor_area_sqm"] = area
    if f.get("storey_lo") is not None:
        m["storey_high"] = {"$gte": f["storey_lo"]}
    if f.get("storey_hi") is not None:
        m["storey_low"] = {"$lte": f["storey_hi"]}
    return m


NEWEST_FIRST = [("month", -1), ("_id", -1)]      # = ORDER BY t.month DESC, t.txn_id DESC

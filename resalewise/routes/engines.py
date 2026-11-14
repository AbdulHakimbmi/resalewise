"""Same question, both engines: run one search on PostgreSQL (normalised
tables + joins) and on MongoDB (denormalised `transactions` documents), check
the answers agree, and report how long each took. Used for the live demo on
the benchmark page; the rigorous numbers come from bench/run_bench.py."""
import time

from flask import Blueprint, jsonify, request

from .. import db
from ..mongo_queries import NEWEST_FIRST, search_filter
from ..web import ApiError, arg_int, rate_limit
from .search import build_search_filters, parse_filters

bp = Blueprint("engines", __name__)


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000, 2)


@bp.route("/api/engines/search")
@rate_limit(30)
def engines_search():
    limit = arg_int(request.args, "limit", 20, lo=1, hi=100)
    f = parse_filters(request.args)
    coll = db.mongo().transactions
    if coll.estimated_document_count() == 0:
        raise ApiError("Load the MongoDB copy first: python scripts/load_mongo_transactions.py",
                       503, "feature_unavailable")

    fw, params, _ = build_search_filters(request.args)
    t0 = time.perf_counter()
    pg_total = db.q("SELECT COUNT(*) AS n " + fw, params)[0]["n"]
    pg_rows = db.q("SELECT t.txn_id " + fw + " ORDER BY t.month DESC, t.txn_id DESC LIMIT %s",
                   params + [limit])
    pg_ms = _ms(t0)

    mfilter = search_filter(f)
    t0 = time.perf_counter()
    mg_total = coll.count_documents(mfilter)
    mg_rows = list(coll.find(mfilter, {"_id": 1}).sort(NEWEST_FIRST).limit(limit))
    mg_ms = _ms(t0)

    pg_ids = [int(r["txn_id"]) for r in pg_rows]
    mg_ids = [int(d["_id"]) for d in mg_rows]
    return jsonify({
        "filters": {k: v for k, v in f.items() if v not in (None, "")},
        "postgres": {"ms": pg_ms, "total": int(pg_total), "ids": pg_ids,
                     "shape": "5 normalised tables, 4 joins"},
        "mongodb":  {"ms": mg_ms, "total": int(mg_total), "ids": mg_ids,
                     "shape": "1 denormalised collection, no joins", "filter": mfilter},
        "same_total": int(pg_total) == int(mg_total),
        "same_rows": pg_ids == mg_ids,
        "note": "Single cold-ish request timings include network and pool overhead; "
                "see the benchmark for warmed, repeated measurements.",
    })

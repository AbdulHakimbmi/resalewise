"""Town reviews (MongoDB).

Reviews are free-form, user-generated documents with optional tags, so they
live in MongoDB as one document each; a $jsonSchema validator (see
resalewise/mongo_setup.py) still enforces the essential shape at the database.
Full CRUD: list / search, create, edit and delete your own reviews.
"""
import re
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request

from .. import db
from ..auth import current_user, require_auth
from ..web import ApiError, arg_int, arg_str, cached, json_body, rate_limit

bp = Blueprint("reviews", __name__)

MAX_COMMENT = 1000
MAX_AUTHOR  = 60
MAX_TAGS    = 8
_TAG_RE     = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{0,29}$")


def _known_towns():
    """Upper-case town names from Postgres, cached for an hour."""
    return cached("towns", 3600, lambda: {r["town_name"].upper() for r in db.q("SELECT town_name FROM towns")})


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)     # stored as naive UTC


def _ts(v):
    # Stored as naive UTC: add "Z" so browsers don't read it as local time.
    if hasattr(v, "isoformat"):
        return v.isoformat() + ("Z" if getattr(v, "tzinfo", None) is None else "")
    return v


def _public(doc, viewer_id=None):
    out = dict(doc)
    if "_id" in out:
        out["id"] = str(out.pop("_id"))
    owner = out.pop("user_id", None)
    out["mine"] = bool(viewer_id is not None and owner == viewer_id)
    for k in ("created_at", "updated_at"):
        if k in out:
            out[k] = _ts(out[k])
    return out


def _validate(data, partial=False):
    """Validate a review payload; return the cleaned fields."""
    out = {}
    if not partial or "rating" in data:
        try:
            rating = int(data.get("rating"))
        except (TypeError, ValueError):
            rating = 0
        if not 1 <= rating <= 5:
            raise ApiError("Please give a rating from 1 to 5 stars.", 400, "validation_error", field="rating")
        out["rating"] = rating
    if not partial or "comment" in data:
        comment = str(data.get("comment") or "").strip()
        if not comment:
            raise ApiError("Please write a short review.", 400, "validation_error", field="comment")
        if len(comment) > MAX_COMMENT:
            raise ApiError(f"Reviews can be at most {MAX_COMMENT} characters.", 400, "validation_error", field="comment")
        out["comment"] = comment
    if not partial or "tags" in data:
        tags = data.get("tags") or []
        if not isinstance(tags, list) or len(tags) > MAX_TAGS \
                or not all(isinstance(t, str) and _TAG_RE.match(t) for t in tags):
            raise ApiError("Tags are invalid.", 400, "validation_error", field="tags")
        out["tags"] = tags
    return out


def _oid(review_id):
    try:
        return ObjectId(review_id)
    except (InvalidId, TypeError):
        raise ApiError("That review could not be found.", 404, "not_found")


# How the review list can be ordered. Every order ends with _id so that two
# reviews with the same rating or timestamp always come out in the same order,
# which is what keeps page 2 from repeating or skipping a review on page 1.
SORTS = {
    "recent": [("created_at", -1), ("_id", -1)],
    "high":   [("rating", -1), ("created_at", -1), ("_id", -1)],
    "low":    [("rating", 1), ("created_at", -1), ("_id", -1)],
}


@bp.route("/api/reviews", methods=["GET"])
def list_reviews():
    """Reviews, optionally filtered by town. `q` switches to MongoDB full-text
    search (text index on comment + tags), ranked by relevance.

    Without `page` the answer is a plain array of the newest `limit` reviews.
    With `page` (1-based) MongoDB does the filtering, sorting and paging
    (`sort` = recent | high | low, `limit` per page) and the answer is
        {"rows": [...], "total": 412, "page": 2, "pages": 42, "limit": 10}
    so the browser never has to download every review to show ten of them."""
    town  = arg_str(request.args, "town")
    text  = arg_str(request.args, "q", max_len=80)
    limit = arg_int(request.args, "limit", 50, lo=1, hi=200)
    page  = arg_int(request.args, "page", None, lo=1, hi=100_000)
    sort  = arg_str(request.args, "sort", max_len=10) or "recent"
    if sort not in SORTS:
        raise ApiError("'sort' must be recent, high or low.", 400, "invalid_parameter")
    filt = {}
    if town:
        filt["town"] = town.upper()
    coll = db.mongo().reviews
    if text:
        filt["$text"] = {"$search": text}
    viewer = current_user() if request.headers.get("Authorization") else None
    viewer_id = viewer["user_id"] if viewer else None

    if page is None:
        if text:
            cursor = coll.find(filt, {"score": {"$meta": "textScore"}}) \
                         .sort([("score", {"$meta": "textScore"})]).limit(limit)
        else:
            cursor = coll.find(filt).sort("created_at", -1).limit(limit)
        return jsonify([_public(r, viewer_id) for r in cursor])

    total = coll.count_documents(filt)
    pages = max(1, -(-total // limit))
    page = min(page, pages)                          # a page past the end shows the last page
    if text:                                         # best match first; sort is ignored
        cursor = coll.find(filt, {"score": {"$meta": "textScore"}}) \
                     .sort([("score", {"$meta": "textScore"}), ("_id", -1)])
    else:
        cursor = coll.find(filt).sort(SORTS[sort])
    cursor = cursor.skip((page - 1) * limit).limit(limit)
    return jsonify({"rows": [_public(r, viewer_id) for r in cursor],
                    "total": total, "page": page, "pages": pages, "limit": limit})


@bp.route("/api/reviews/stats", methods=["GET"])
def review_stats():
    """Review statistics computed INSIDE MongoDB with the aggregation pipeline
    ($match -> $group -> $sort), so the browser reads finished numbers instead
    of downloading every review and averaging in JavaScript.

        {"overall": {"count": 412, "avg_rating": 4.1, "towns": 26},
         "by_town": [{"town": "TAMPINES", "count": 18, "avg_rating": 4.3}, ...]}
    """
    town = arg_str(request.args, "town")
    match = [{"$match": {"town": town.upper()}}] if town else []
    coll = db.mongo().reviews

    by_town_docs = list(coll.aggregate(match + [
        {"$group": {"_id": "$town",
                    "avg":   {"$avg": "$rating"},
                    "count": {"$sum": 1}}},
        {"$sort": {"count": -1, "_id": 1}},
    ]))
    overall_docs = list(coll.aggregate(match + [
        {"$group": {"_id": None,
                    "avg":   {"$avg": "$rating"},
                    "count": {"$sum": 1}}},
    ]))

    overall = overall_docs[0] if overall_docs else {"avg": None, "count": 0}
    return jsonify({
        "overall": {
            "count": overall["count"],
            "avg_rating": round(overall["avg"], 2) if overall["avg"] is not None else None,
            "towns": len(by_town_docs),
        },
        "by_town": [{"town": d["_id"], "count": d["count"],
                     "avg_rating": round(d["avg"], 2) if d["avg"] is not None else None}
                    for d in by_town_docs],
    })


INSIGHTS_PIPELINE = [
    {"$facet": {
        # Rating histogram (1-5 stars)
        "ratings": [{"$group": {"_id": "$rating", "n": {"$sum": 1}}}, {"$sort": {"_id": 1}}],
        # Most-used tags with the average rating of reviews that carry them
        "tags": [{"$unwind": "$tags"},
                 {"$group": {"_id": "$tags", "n": {"$sum": 1}, "avg": {"$avg": "$rating"}}},
                 {"$sort": {"n": -1, "_id": 1}}, {"$limit": 8}],
        # Reviews per month
        "monthly": [{"$group": {"_id": {"$dateTrunc": {"date": "$created_at", "unit": "month"}},
                                "n": {"$sum": 1}, "avg": {"$avg": "$rating"}}},
                    {"$sort": {"_id": 1}}],
        # Best-rated towns with enough reviews to mean something
        "best_towns": [{"$group": {"_id": "$town", "avg": {"$avg": "$rating"}, "n": {"$sum": 1}}},
                       {"$match": {"n": {"$gte": 3}}}, {"$sort": {"avg": -1, "n": -1}}, {"$limit": 5}],
    }},
]


@bp.route("/api/reviews/insights", methods=["GET"])
def review_insights():
    """One aggregation, four answers ($facet runs sub-pipelines over the same
    matched documents): rating histogram, top tags, monthly volume, best towns."""
    town = arg_str(request.args, "town")
    match = [{"$match": {"town": town.upper()}}] if town else []
    doc = next(iter(db.mongo().reviews.aggregate(match + INSIGHTS_PIPELINE)), None) or {}
    r2 = lambda v: round(v, 2) if v is not None else None   # noqa: E731
    return jsonify({
        "town": town.upper() if town else None,
        "ratings": [{"stars": d["_id"], "count": d["n"]} for d in doc.get("ratings", [])],
        "tags": [{"tag": d["_id"], "count": d["n"], "avg_rating": r2(d["avg"])} for d in doc.get("tags", [])],
        "monthly": [{"month": _ts(d["_id"]), "count": d["n"], "avg_rating": r2(d["avg"])}
                    for d in doc.get("monthly", []) if d.get("_id")],
        "best_towns": [{"town": d["_id"], "avg_rating": r2(d["avg"]), "count": d["n"]}
                       for d in doc.get("best_towns", [])],
    })


@bp.route("/api/reviews", methods=["POST"])
@rate_limit(10)
def add_review():
    data = json_body()
    town = str(data.get("town") or "").strip().upper()
    if not town:
        raise ApiError("Please choose a town.", 400, "validation_error", field="town")
    if town not in _known_towns():
        raise ApiError("That town isn't in our data.", 400, "validation_error", field="town")
    fields = _validate(data)

    user = current_user() if request.headers.get("Authorization") else None
    author = str(data.get("author") or "").strip() or ((user or {}).get("name") or "Anonymous")
    if len(author) > MAX_AUTHOR:
        raise ApiError(f"Names can be at most {MAX_AUTHOR} characters.", 400, "validation_error", field="author")

    doc = {"town": town, **fields, "author": author, "created_at": _now()}
    if user:
        doc["user_id"] = user["user_id"]       # lets the author edit / delete it later
    res = db.mongo().reviews.insert_one(doc)
    new_id = getattr(res, "inserted_id", None)
    return jsonify({"ok": True, "id": str(new_id) if new_id is not None else None}), 201


def _own_review(review_id, user):
    coll = db.mongo().reviews
    doc = coll.find_one({"_id": _oid(review_id)})
    if not doc:
        raise ApiError("That review could not be found.", 404, "not_found")
    if doc.get("user_id") != user["user_id"]:
        raise ApiError("You can only change your own reviews.", 403, "forbidden")
    return coll, doc


@bp.route("/api/reviews/<review_id>", methods=["PATCH"])
@rate_limit(20)
@require_auth
def edit_review(review_id, user):
    fields = _validate(json_body(), partial=True)
    if not fields:
        raise ApiError("Nothing to update.", 400, "validation_error")
    coll, doc = _own_review(review_id, user)
    coll.update_one({"_id": doc["_id"]}, {"$set": {**fields, "updated_at": _now()}})
    return jsonify({"ok": True, "id": review_id})


@bp.route("/api/reviews/<review_id>", methods=["DELETE"])
@rate_limit(20)
@require_auth
def delete_review(review_id, user):
    coll, doc = _own_review(review_id, user)
    coll.delete_one({"_id": doc["_id"]})
    return jsonify({"ok": True})

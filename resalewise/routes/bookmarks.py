"""The shortlist: bookmarks link users to transactions (a many-to-many
relationship stored in PostgreSQL). Full CRUD: list, add, edit notes, remove."""
from flask import Blueprint, jsonify

from .. import db
from ..auth import require_auth
from ..web import ApiError, json_body, rate_limit

bp = Blueprint("bookmarks", __name__)


def _clean_notes(raw):
    if raw is None:
        return None
    return str(raw).strip()[:500] or None


@bp.route("/api/bookmarks", methods=["GET"])
@require_auth
def list_bookmarks(user):
    rows = db.q("""
      SELECT b.txn_id, b.notes, b.created_at,
             t.month, tw.town_name, UPPER(REPLACE(ft.flat_type_name, '-', ' ')) AS flat_type_name, UPPER(REPLACE(fm.flat_model_name, '-', ' ')) AS flat_model_name,
             bl.block_no, bl.street_name,
             t.storey_low, t.storey_high, t.floor_area_sqm,
             t.remaining_lease_months, t.resale_price
      FROM bookmarks b
      JOIN resale_transactions t ON t.txn_id = b.txn_id
      JOIN blocks bl      ON bl.block_id = t.block_id
      JOIN towns tw       ON tw.town_id = bl.town_id
      JOIN flat_types ft  ON ft.flat_type_id = t.flat_type_id
      JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id
      WHERE b.user_id = %s
      ORDER BY b.created_at DESC
    """, [user["user_id"]])
    for r in rows:
        r["resale_price"]   = float(r["resale_price"])
        r["floor_area_sqm"] = float(r["floor_area_sqm"])
        r["month"]          = r["month"].isoformat()
        r["created_at"]     = r["created_at"].isoformat()
    return jsonify(rows)


@bp.route("/api/bookmarks", methods=["POST"])
@rate_limit(60)
@require_auth
def add_bookmark(user):
    data = json_body()
    try:
        txn_id = int(data.get("txn_id"))
    except (TypeError, ValueError):
        raise ApiError("'txn_id' must be a whole number.", 400, "validation_error", field="txn_id")
    if txn_id < 1:
        raise ApiError("'txn_id' must be a positive whole number.", 400, "validation_error", field="txn_id")
    notes = _clean_notes(data.get("notes"))

    # Without this check a bad id surfaces as a foreign-key error.
    if not db.q("SELECT 1 FROM resale_transactions WHERE txn_id = %s", [txn_id]):
        raise ApiError("That transaction could not be found.", 404, "not_found")

    db.execute("""
      INSERT INTO bookmarks (user_id, txn_id, notes)
      VALUES (%s, %s, %s)
      ON CONFLICT (user_id, txn_id) DO UPDATE SET notes = EXCLUDED.notes
    """, [user["user_id"], txn_id, notes])
    return jsonify({"ok": True, "txn_id": txn_id})


@bp.route("/api/bookmarks/<int:txn_id>", methods=["PATCH"])
@rate_limit(60)
@require_auth
def edit_bookmark(txn_id, user):
    """Edit the note on a saved flat (the trigger stamps updated_at)."""
    data = json_body()
    if "notes" not in data:
        raise ApiError("Send the new 'notes' (or null to clear them).", 400, "validation_error", field="notes")
    count, _ = db.execute("UPDATE bookmarks SET notes = %s WHERE user_id = %s AND txn_id = %s",
                          [_clean_notes(data.get("notes")), user["user_id"], txn_id])
    if not count:
        raise ApiError("That flat isn't in your shortlist.", 404, "not_found")
    return jsonify({"ok": True, "txn_id": txn_id})


@bp.route("/api/bookmarks/<int:txn_id>", methods=["DELETE"])
@rate_limit(60)
@require_auth
def remove_bookmark(txn_id, user):
    db.execute("DELETE FROM bookmarks WHERE user_id = %s AND txn_id = %s", [user["user_id"], txn_id])
    return jsonify({"ok": True})

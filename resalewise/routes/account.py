"""Accounts: register, log in, view / update / delete your account."""
import re

import psycopg2
from flask import Blueprint, jsonify
from werkzeug.security import check_password_hash, generate_password_hash

from .. import db
from ..auth import current_user, make_token, require_auth
from ..web import ApiError, iso, json_body, rate_limit

bp = Blueprint("account", __name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_password(pw, field="password"):
    if len(pw) < 6:
        raise ApiError("Password must be at least 6 characters.", 400, "validation_error", field=field)
    if len(pw) > 128:
        raise ApiError("Password must be 128 characters or fewer.", 400, "validation_error", field=field)


@bp.route("/api/register", methods=["POST"])
@rate_limit(10)
def register():
    data  = json_body()
    email = str(data.get("email") or "").strip().lower()
    pw    = str(data.get("password") or "")
    name  = str(data.get("name") or "").strip()[:80]

    if not name:
        raise ApiError("Please enter your name.", 400, "validation_error", field="name")
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ApiError("A valid email is required.", 400, "validation_error", field="email")
    _check_password(pw)

    if db.q("SELECT 1 FROM users WHERE email = %s", [email]):
        raise ApiError("An account with this email already exists.", 409, "conflict", field="email")

    try:
        _, row = db.execute(
            "INSERT INTO users (email, password_hash, name) VALUES (%s, %s, %s) RETURNING user_id",
            [email, generate_password_hash(pw), name])
    except psycopg2.errors.UniqueViolation:
        # Two sign-ups with the same email raced past the check above.
        raise ApiError("An account with this email already exists.", 409, "conflict", field="email")
    uid = row["user_id"]
    return jsonify({"token": make_token(uid, email),
                    "user": {"user_id": uid, "email": email, "name": name}}), 201


@bp.route("/api/login", methods=["POST"])
@rate_limit(10)
def login():
    data  = json_body()
    email = str(data.get("email") or "").strip().lower()[:254]
    pw    = str(data.get("password") or "")[:128]

    rows = db.q("SELECT user_id, email, name, password_hash FROM users WHERE email = %s", [email])
    if not rows or not check_password_hash(rows[0]["password_hash"], pw):
        raise ApiError("Incorrect email or password.", 401, "invalid_credentials")

    u = rows[0]
    return jsonify({"token": make_token(u["user_id"], u["email"]),
                    "user": {"user_id": u["user_id"], "email": u["email"], "name": u.get("name")}})


@bp.route("/api/me")
def me():
    user = current_user()
    if not user:
        raise ApiError("Please log in to continue.", 401, "unauthorized")
    return jsonify({"user_id": user["user_id"], "email": user["email"], "name": user.get("name"),
                    "created_at": iso(user.get("created_at"))})


@bp.route("/api/me", methods=["PATCH"])
@rate_limit(20)
@require_auth
def update_me(user):
    """Update the logged-in user's display name and/or password."""
    data = json_body()
    new_name = data.get("name")
    new_pw   = data.get("new_password")
    cur_pw   = data.get("current_password")

    updates, params = [], []
    if new_name is not None:
        new_name = str(new_name).strip()[:80]
        if not new_name:
            raise ApiError("Name cannot be empty.", 400, "validation_error", field="name")
        updates.append("name = %s"); params.append(new_name)

    if new_pw:
        new_pw = str(new_pw)
        _check_password(new_pw, field="new_password")
        row = db.q("SELECT password_hash FROM users WHERE user_id = %s", [user["user_id"]])
        if not row or not check_password_hash(row[0]["password_hash"], str(cur_pw or "")):
            raise ApiError("Your current password is incorrect.", 400, "invalid_credentials", field="current_password")
        updates.append("password_hash = %s"); params.append(generate_password_hash(new_pw))

    if not updates:
        raise ApiError("Nothing to update.", 400, "validation_error")

    params.append(user["user_id"])
    db.execute("UPDATE users SET " + ", ".join(updates) + " WHERE user_id = %s", params)

    fresh = db.q("SELECT user_id, email, name FROM users WHERE user_id = %s", [user["user_id"]])[0]
    return jsonify({"ok": True, "user": {"user_id": fresh["user_id"], "email": fresh["email"], "name": fresh.get("name")}})


@bp.route("/api/me", methods=["DELETE"])
@rate_limit(5)
@require_auth
def delete_me(user):
    """Delete the account. Needs the password again (a stolen token alone is
    not enough). ON DELETE CASCADE removes the bookmarks and profile."""
    data = json_body()
    row = db.q("SELECT password_hash FROM users WHERE user_id = %s", [user["user_id"]])
    if not row or not check_password_hash(row[0]["password_hash"], str(data.get("password") or "")[:128]):
        raise ApiError("Your password is incorrect.", 400, "invalid_credentials", field="password")

    with db.pg_conn() as conn, conn.cursor() as cur:
        # The cascade removes bookmarks + profile (the audit trigger logs those
        # removals), then the user's audit trail goes too.
        cur.execute("DELETE FROM users WHERE user_id = %s", [user["user_id"]])
        cur.execute("SELECT to_regclass('bookmark_audit') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("DELETE FROM bookmark_audit WHERE user_id = %s", [user["user_id"]])
    return jsonify({"ok": True})


@bp.route("/api/me/activity")
@require_auth
def my_activity(user):
    """Recent changes to your shortlist, recorded by a Postgres trigger
    (bookmark_audit). Empty list if the extras migration hasn't been applied."""
    if not db.has_relation("bookmark_audit"):
        return jsonify([])
    rows = db.q("""
      SELECT a.action, a.txn_id, a.notes, a.changed_at,
             tw.town_name, b.block_no, b.street_name
      FROM bookmark_audit a
      LEFT JOIN resale_transactions t ON t.txn_id = a.txn_id
      LEFT JOIN blocks b  ON b.block_id = t.block_id
      LEFT JOIN towns tw  ON tw.town_id = b.town_id
      WHERE a.user_id = %s
      ORDER BY a.changed_at DESC, a.audit_id DESC
      LIMIT 20
    """, [user["user_id"]])
    for r in rows:
        r["changed_at"] = iso(r["changed_at"])
    return jsonify(rows)

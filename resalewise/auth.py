"""Signed bearer tokens and the login helpers.

We use signed bearer tokens (not cookies) because the front end and the API
can be served from different origins during development, where cross-origin
session cookies are unreliable. The token lives in localStorage and is sent as
"Authorization: Bearer <token>".

    token = base64url(payload_json) + "." + base64url(HMAC-SHA256(secret, payload))

This is deliberately small and explicit (not a JWT library) so the report can
explain every step. Passwords are hashed with Werkzeug's scrypt-based
generate_password_hash and never stored in plain text.
"""
import base64
import functools
import hashlib
import hmac
import json
import time

from flask import request

from . import config, db
from .web import ApiError

TOKEN_TTL = 60 * 60 * 24 * 30   # 30 days


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(config.token_secret(), body.encode(), hashlib.sha256).digest())


def make_token(user_id: int, email: str) -> str:
    # The display name is not embedded (it can change); it is returned in the
    # login/register/me response bodies instead.
    payload = {"uid": user_id, "email": email, "exp": int(time.time()) + TOKEN_TTL}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + _sign(body)


def verify_token(token: str):
    """Return the payload dict if the token is valid and unexpired, else None."""
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def current_user():
    """Resolve the logged-in user from the Authorization header, or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = verify_token(auth[7:])
    if not payload:
        return None
    rows = db.q("SELECT user_id, email, name, created_at, is_admin FROM users WHERE user_id = %s", [payload["uid"]])
    return rows[0] if rows else None


def require_auth(fn):
    """Decorator: 401 unless a valid token is present. Injects `user` kwarg."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            raise ApiError("Please log in to continue.", 401, "unauthorized")
        return fn(*args, user=user, **kwargs)
    return wrapper


def require_admin(fn):
    """401 without a valid token, 403 if the user isn't an admin. The flag is read from the
    database each time, so taking it away works straight away."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            raise ApiError("Please log in to continue.", 401, "unauthorized")
        if not user.get("is_admin"):
            raise ApiError("This area is for administrators.", 403, "forbidden")
        return fn(*args, user=user, **kwargs)
    return wrapper

"""Session auth, CSRF and rate limiting for the login system.

- Login state lives in Flask's signed session cookie.
- Idle timeout: 1 hour normally, 30 days with "remember me".
- Identity: uid string "u<id>" (keys Telegram/autoplay data) + integer login_id
  (keys SQLite messages/rules/settings).
- Passwords/hashes never touch the session or the frontend.
"""
import functools
import hmac
import secrets
import time
from collections import defaultdict, deque

from flask import jsonify, redirect, request, session, url_for

IDLE_SECONDS = 3600            # 1 hour
REMEMBER_SECONDS = 30 * 86400  # 30 days

_hits = defaultdict(deque)


# --- session lifecycle ------------------------------------------------------
def _rg(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def login_session(user_row, remember: bool) -> str:
    session.clear()
    role = _rg(user_row, "role", "user") or "user"
    session["uid"] = f"u{user_row['id']}"
    session["login_id"] = int(user_row["id"])
    session["username"] = user_row["username"]
    session["role"] = role
    session["is_admin"] = role == "admin"
    session["epoch"] = _rg(user_row, "session_epoch", 0) or 0
    session["remember"] = bool(remember)
    session["last"] = time.time()
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = bool(remember)
    sid = secrets.token_urlsafe(12)
    session["sid"] = sid
    return sid


def logout_session():
    session.clear()


def _expired() -> bool:
    last = session.get("last", 0)
    limit = REMEMBER_SECONDS if session.get("remember") else IDLE_SECONDS
    return (time.time() - last) > limit


def is_authed() -> bool:
    if not session.get("uid"):
        return False
    if _expired():
        session.clear()
        return False
    return True


def touch():
    if session.get("uid"):
        session["last"] = time.time()


def current_uid():
    return session.get("uid")


def current_login_id():
    return session.get("login_id")


def is_admin() -> bool:
    return session.get("role") == "admin" or bool(session.get("is_admin"))


def session_epoch() -> int:
    return session.get("epoch", 0)


# --- CSRF -------------------------------------------------------------------
def get_csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def _valid_csrf() -> bool:
    expected = session.get("csrf")
    if not expected:
        return False
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        payload = request.get_json(silent=True) or {}
        sent = payload.get("csrf") or request.form.get("csrf")
    return bool(sent) and hmac.compare_digest(str(sent), str(expected))


# --- decorators -------------------------------------------------------------
def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_authed():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required."}), 401
            return redirect(url_for("login", next=request.path))
        touch()
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_authed():
            return jsonify({"error": "Authentication required."}), 401
        if not is_admin():
            return jsonify({"error": "Admin only."}), 403
        touch()
        return fn(*args, **kwargs)

    return wrapper


def csrf_protect(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _valid_csrf():
            return jsonify({"error": "Invalid or missing CSRF token."}), 403
        return fn(*args, **kwargs)

    return wrapper


def rate_limit(max_calls: int = 30, per_seconds: int = 10):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (request.remote_addr or "?", fn.__name__)
            now = time.time()
            bucket = _hits[key]
            while bucket and bucket[0] <= now - per_seconds:
                bucket.popleft()
            if len(bucket) >= max_calls:
                return jsonify({"error": "Too many attempts. Please slow down."}), 429
            bucket.append(now)
            return fn(*args, **kwargs)

        return wrapper

    return decorator

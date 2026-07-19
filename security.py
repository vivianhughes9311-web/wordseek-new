"""Identity, optional access gate, CSRF and rate limiting.

Identity: every visitor gets a signed `uid` in their session cookie — this is
their account handle (each uid maps to one Telegram login).

Access gate: OPTIONAL. If DASHBOARD_PASSWORD is set, the whole dashboard sits
behind that shared password (useful for a private instance). If it is NOT set,
the dashboard is open and the only login is "Login with Telegram" — so no scary
password warning when it is unset.
"""
import functools
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from flask import jsonify, redirect, request, session, url_for

ACCESS_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
_hits = defaultdict(deque)


# --- identity ---------------------------------------------------------------
def ensure_uid() -> str:
    uid = session.get("uid")
    if not uid:
        uid = secrets.token_urlsafe(18)
        session["uid"] = uid
        session.permanent = True
    return uid


def current_uid() -> str:
    return session.get("uid") or ensure_uid()


# --- optional access gate ---------------------------------------------------
def gate_enabled() -> bool:
    return bool(ACCESS_PASSWORD)


def gate_ok() -> bool:
    return (not gate_enabled()) or (session.get("gate_ok") is True)


def check_password(candidate: str) -> bool:
    if not ACCESS_PASSWORD:
        return False
    return hmac.compare_digest(str(candidate), ACCESS_PASSWORD)


def open_gate():
    session["gate_ok"] = True
    session.permanent = True
    session["csrf"] = secrets.token_urlsafe(32)


def close_gate():
    session.pop("gate_ok", None)


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
def access_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ensure_uid()
        if not gate_ok():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Access locked."}), 401
            return redirect(url_for("login", next=request.path))
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
                return jsonify({"error": "Rate limit exceeded. Please slow down."}), 429
            bucket.append(now)
            return fn(*args, **kwargs)

        return wrapper

    return decorator

"""Lightweight auth, CSRF and rate-limiting for the dashboard control plane.

- Password comes from the DASHBOARD_PASSWORD env var (never hardcoded).
- Login state is kept in Flask's signed session cookie.
- CSRF: a per-session token must accompany every state-changing POST.
- Rate limiting: simple in-memory sliding window per (ip, endpoint).
"""
import functools
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from flask import jsonify, redirect, request, session, url_for

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

_hits = defaultdict(deque)


def password_configured() -> bool:
    return bool(DASHBOARD_PASSWORD)


def check_password(candidate: str) -> bool:
    if not DASHBOARD_PASSWORD:
        return False
    return hmac.compare_digest(str(candidate), DASHBOARD_PASSWORD)


def login_user():
    session["auth"] = True
    session.permanent = True
    # rotate CSRF token on login
    session["csrf"] = secrets.token_urlsafe(32)


def logout_user():
    session.pop("auth", None)
    session.pop("csrf", None)


def is_authed() -> bool:
    return session.get("auth") is True


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


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_authed():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required."}), 401
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

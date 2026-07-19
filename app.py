"""WordSeek — solver web app + web-controlled Telegram monitor.

The original solver endpoints (/, /solve, /health) are unchanged in behaviour.
A password-protected dashboard (/dashboard) plus /api/* control routes drive a
background Telegram client (see telegram_worker.py) that monitors a group,
auto-solves WordSeek boards and can send the best guess back.
"""
import logging
import os
import secrets
from datetime import timedelta

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from security import (
    check_password,
    csrf_protect,
    get_csrf_token,
    is_authed,
    login_required,
    login_user,
    logout_user,
    password_configured,
    rate_limit,
)
from solver import solve_board

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.environ.get("COOKIE_SECURE", "0") == "1"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=64 * 1024,
)

# The Telegram manager starts its own background thread on first import.
# Guard it so the web app still boots even if Telethon/credentials are absent.
try:
    from telegram_worker import get_manager

    manager = get_manager()
except Exception:  # pragma: no cover
    app.logger.exception("Telegram manager unavailable")
    manager = None


# ----------------------------------------------------------------------------
# Existing solver endpoints (unchanged behaviour)
# ----------------------------------------------------------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    tg = {"available": False, "configured": False, "connected": False}
    if manager is not None:
        try:
            st = manager.status()
            tg = {"available": st["available"], "configured": st["configured"], "connected": st["connected"]}
        except Exception:
            pass
    return jsonify({"status": "ok", "telegram": tg})


@app.post("/solve")
def solve():
    data = request.get_json(silent=True) or {}
    board = str(data.get("board", "")).strip()

    try:
        mode = int(data.get("mode", 5))
    except (TypeError, ValueError):
        return jsonify({"error": "Mode must be 4 or 5."}), 400

    if mode not in (4, 5):
        return jsonify({"error": "Mode must be 4 or 5."}), 400
    if not board:
        return jsonify({"error": "Paste a WordSeek board first."}), 400
    if len(board) > 25_000:
        return jsonify({"error": "Board input is too large."}), 413

    try:
        result = solve_board(board, mode)
    except Exception:
        app.logger.exception("Solver failed")
        return jsonify({"error": "Solver failed. Check the board format."}), 500

    return jsonify({
        "text": result["message"],
        "answer": result["answer"],
        "guesses": result["guesses"],
        "count": result["count"],
        "mode": mode,
    })


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
@rate_limit(max_calls=20, per_seconds=60)
def login():
    if is_authed():
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        if not password_configured():
            error = "Dashboard password is not configured on the server."
        elif check_password(request.form.get("password", "")):
            login_user()
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))
        else:
            error = "Incorrect password."

    return render_template(
        "login.html",
        error=error,
        password_set=password_configured(),
    )


@app.route("/logout", methods=["GET", "POST"])
def logout():
    logout_user()
    return redirect(url_for("home"))


# ----------------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------------
@app.get("/dashboard")
@login_required
def dashboard():
    tg_available = manager is not None and manager.status().get("available")
    tg_mode = manager.status().get("mode") if manager is not None else None
    return render_template(
        "dashboard.html",
        csrf_token=get_csrf_token(),
        tg_available=tg_available,
        tg_mode=tg_mode,
    )


# ----------------------------------------------------------------------------
# Control API — all protected: login + CSRF + rate limit
# ----------------------------------------------------------------------------
def _manager_ready():
    if manager is None:
        return jsonify({"error": "Telegram support is unavailable on this server."}), 503
    return None


@app.get("/api/status")
@login_required
@rate_limit(max_calls=120, per_seconds=10)
def api_status():
    if manager is None:
        return jsonify({"available": False, "configured": False, "connected": False})
    return jsonify(manager.status())


@app.post("/api/connect")
@login_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=30)
def api_connect():
    guard = _manager_ready()
    if guard:
        return guard
    return jsonify(manager.connect())


@app.post("/api/disconnect")
@login_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=30)
def api_disconnect():
    guard = _manager_ready()
    if guard:
        return guard
    return jsonify(manager.disconnect())


@app.post("/api/pause")
@login_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=30)
def api_pause():
    guard = _manager_ready()
    if guard:
        return guard
    return jsonify(manager.pause())


@app.post("/api/resume")
@login_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=30)
def api_resume():
    guard = _manager_ready()
    if guard:
        return guard
    return jsonify(manager.resume())


@app.get("/api/groups")
@login_required
@rate_limit(max_calls=15, per_seconds=30)
def api_groups():
    if manager is None:
        return jsonify({"ok": False, "error": "unavailable", "groups": []})
    return jsonify(manager.list_groups())


@app.post("/api/select_group")
@login_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_select_group():
    guard = _manager_ready()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(manager.select_group(data.get("id"), data.get("name")))


@app.post("/api/auto_send")
@login_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_auto_send():
    guard = _manager_ready()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(manager.set_auto_send(bool(data.get("enabled"))))


@app.post("/api/delay")
@login_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_delay():
    guard = _manager_ready()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(manager.set_delay(data.get("delay_ms")))


@app.post("/api/send")
@login_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=20)
def api_send():
    guard = _manager_ready()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(manager.send_guess(data.get("word")))


# ----------------------------------------------------------------------------
@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "51332"))
    # Reloader disabled so we never start two Telegram clients.
    app.run(host="0.0.0.0", port=port, use_reloader=False)

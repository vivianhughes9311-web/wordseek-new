"""WordSeek — solver + multi-account Telegram autoplay.

Public solver: /, /solve, /health (unchanged behaviour).
Dashboard: /dashboard — each visitor logs in with their OWN Telegram account
(phone + code) and can monitor a group, auto-solve and autoplay entire rounds.
An optional shared access password (DASHBOARD_PASSWORD) can gate the whole
dashboard; when unset, the dashboard is open and Telegram login is the only auth.
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
    url_for,
)

from security import (
    access_required,
    check_password,
    close_gate,
    csrf_protect,
    current_uid,
    ensure_uid,
    gate_enabled,
    get_csrf_token,
    open_gate,
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
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=64 * 1024,
)

try:
    from telegram_worker import get_service

    service = get_service()
except Exception:  # pragma: no cover
    app.logger.exception("Telegram service unavailable")
    service = None


# ----------------------------------------------------------------------------
# Solver (unchanged)
# ----------------------------------------------------------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    tg = {"available": False, "configured": False}
    if service is not None:
        tg = {"available": getattr(service, "api_id", 0) is not None and True, "configured": service.configured()}
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
        "text": result["message"], "answer": result["answer"],
        "guesses": result["guesses"], "count": result["count"], "mode": mode,
    })


# ----------------------------------------------------------------------------
# Optional access gate
# ----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
@rate_limit(max_calls=20, per_seconds=60)
def login():
    ensure_uid()
    if not gate_enabled():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        if check_password(request.form.get("password", "")):
            open_gate()
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    close_gate()
    return redirect(url_for("home"))


# ----------------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------------
@app.get("/dashboard")
@access_required
def dashboard():
    ensure_uid()
    tg_configured = service is not None and service.configured()
    return render_template("dashboard.html", csrf_token=get_csrf_token(), tg_configured=tg_configured)


# ----------------------------------------------------------------------------
# Control API
# ----------------------------------------------------------------------------
def _need_service():
    if service is None:
        return jsonify({"error": "Telegram support is unavailable on this server."}), 503
    return None


@app.get("/api/status")
@access_required
@rate_limit(max_calls=150, per_seconds=10)
def api_status():
    if service is None:
        return jsonify({"available": False, "configured": False, "connected": False})
    return jsonify(service.status(current_uid()))


# --- Telegram login ---
@app.post("/api/tg/send_code")
@access_required
@csrf_protect
@rate_limit(max_calls=8, per_seconds=60)
def api_send_code():
    guard = _need_service()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(service.login_start(current_uid(), data.get("phone")))


@app.post("/api/tg/sign_in")
@access_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=60)
def api_sign_in():
    guard = _need_service()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(service.login_code(current_uid(), data.get("code")))


@app.post("/api/tg/password")
@access_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=60)
def api_password():
    guard = _need_service()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(service.login_password(current_uid(), data.get("password")))


@app.post("/api/tg/logout")
@access_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=30)
def api_tg_logout():
    guard = _need_service()
    if guard:
        return guard
    return jsonify(service.logout(current_uid()))


# --- group + manual ---
@app.get("/api/groups")
@access_required
@rate_limit(max_calls=15, per_seconds=30)
def api_groups():
    if service is None:
        return jsonify({"ok": False, "error": "unavailable", "groups": []})
    return jsonify(service.list_groups(current_uid()))


@app.post("/api/select_group")
@access_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_select_group():
    guard = _need_service()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(service.select_group(current_uid(), data.get("id"), data.get("name")))


@app.post("/api/send")
@access_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=20)
def api_send():
    guard = _need_service()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(service.send_guess(current_uid(), data.get("word")))


@app.post("/api/auto_send")
@access_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_auto_send():
    guard = _need_service()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(service.set_auto_send(current_uid(), bool(data.get("enabled"))))


# --- autoplay ---
@app.post("/api/autoplay/enabled")
@access_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_ap_enabled():
    guard = _need_service()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    return jsonify(service.autoplay_set_enabled(current_uid(), bool(data.get("enabled"))))


@app.post("/api/autoplay/<action>")
@access_required
@csrf_protect
@rate_limit(max_calls=40, per_seconds=30)
def api_ap_action(action):
    guard = _need_service()
    if guard:
        return guard
    uid = current_uid()
    mapping = {
        "start": service.autoplay_start,
        "stop": service.autoplay_stop,
        "pause": service.autoplay_pause,
        "resume": service.autoplay_resume,
        "emergency": service.autoplay_emergency,
    }
    fn = mapping.get(action)
    if not fn:
        return jsonify({"error": "Unknown action."}), 404
    return jsonify(fn(uid))


@app.post("/api/autoplay_config")
@access_required
@csrf_protect
@rate_limit(max_calls=40, per_seconds=30)
def api_ap_config():
    guard = _need_service()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    data.pop("csrf", None)
    return jsonify(service.autoplay_config(current_uid(), data))


# ----------------------------------------------------------------------------
@app.after_request
def secure_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "51332"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

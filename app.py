"""WordSeek — solver + multi-user Telegram autoplay dashboard.

Public solver: /, /solve, /health.
Everything else lives behind a username/password login (SQLite + hashed
passwords). Each account has its own Telegram login, autoplay, custom messages
and automation rules. The first account created becomes the admin.
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

import db
import security
from security import (
    admin_required,
    csrf_protect,
    current_login_id,
    current_uid,
    get_csrf_token,
    login_required,
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
    MAX_CONTENT_LENGTH=256 * 1024,
)

try:
    from telegram_worker import get_service

    service = get_service()
except Exception:  # pragma: no cover
    app.logger.exception("Telegram service unavailable")
    service = None


@app.before_request
def _touch_seen():
    if request.path.startswith(("/api/", "/dashboard")):
        try:
            if security.is_authed():
                lid = current_login_id()
                if lid:
                    db.touch_seen(lid)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Solver (public, unchanged)
# ----------------------------------------------------------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    tg = {"available": False, "configured": False}
    if service is not None:
        tg = {"available": True, "configured": service.configured()}
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
    return jsonify({"text": result["message"], "answer": result["answer"],
                    "guesses": result["guesses"], "count": result["count"], "mode": mode})


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
@rate_limit(max_calls=12, per_seconds=60)
def login():
    setup = db.count_users() == 0
    if security.is_authed() and not setup:
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        if setup:
            res = db.create_user(username, password, is_admin=True)
            if not res.get("ok"):
                error = res.get("error")
            else:
                row = db.get_user(res["id"])
                sid = security.login_session(row, remember)
                db.touch_login(row["id"], sid)
                return redirect(url_for("dashboard"))
        else:
            row = db.verify_login(username, password)
            if row == "disabled":
                error = "This account is disabled."
            elif not row:
                error = "Incorrect username or password."
            else:
                sid = security.login_session(row, remember)
                db.touch_login(row["id"], sid)
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))

    return render_template("login.html", error=error, setup=setup)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    security.logout_session()
    return redirect(url_for("login"))


# ----------------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------------
@app.get("/dashboard")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        csrf_token=get_csrf_token(),
        username=session.get("username"),
        is_admin=session.get("is_admin"),
        tg_configured=service is not None and service.configured(),
    )


# ----------------------------------------------------------------------------
# Telegram + autoplay API
# ----------------------------------------------------------------------------
def _need_service():
    if service is None:
        return jsonify({"error": "Telegram support is unavailable."}), 503
    return None


@app.get("/api/status")
@login_required
@rate_limit(max_calls=200, per_seconds=10)
def api_status():
    if service is None:
        return jsonify({"available": False, "configured": False, "connected": False})
    return jsonify(service.status(current_uid()))


@app.post("/api/tg/send_code")
@login_required
@csrf_protect
@rate_limit(max_calls=8, per_seconds=60)
def api_send_code():
    if (g := _need_service()):
        return g
    return jsonify(service.login_start(current_uid(), (request.get_json(silent=True) or {}).get("phone")))


@app.post("/api/tg/sign_in")
@login_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=60)
def api_sign_in():
    if (g := _need_service()):
        return g
    return jsonify(service.login_code(current_uid(), (request.get_json(silent=True) or {}).get("code")))


@app.post("/api/tg/password")
@login_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=60)
def api_password():
    if (g := _need_service()):
        return g
    return jsonify(service.login_password(current_uid(), (request.get_json(silent=True) or {}).get("password")))


@app.post("/api/tg/logout")
@login_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=30)
def api_tg_logout():
    if (g := _need_service()):
        return g
    return jsonify(service.logout(current_uid()))


@app.get("/api/groups")
@login_required
@rate_limit(max_calls=15, per_seconds=30)
def api_groups():
    if service is None:
        return jsonify({"ok": False, "error": "unavailable", "groups": []})
    return jsonify(service.list_groups(current_uid()))


@app.post("/api/select_group")
@login_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_select_group():
    if (g := _need_service()):
        return g
    d = request.get_json(silent=True) or {}
    return jsonify(service.select_group(current_uid(), d.get("id"), d.get("name")))


@app.post("/api/send")
@login_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=20)
def api_send():
    if (g := _need_service()):
        return g
    return jsonify(service.send_guess(current_uid(), (request.get_json(silent=True) or {}).get("word")))


@app.post("/api/auto_send")
@login_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_auto_send():
    if (g := _need_service()):
        return g
    return jsonify(service.set_auto_send(current_uid(), bool((request.get_json(silent=True) or {}).get("enabled"))))


@app.post("/api/autoplay/enabled")
@login_required
@csrf_protect
@rate_limit(max_calls=30, per_seconds=30)
def api_ap_enabled():
    if (g := _need_service()):
        return g
    return jsonify(service.autoplay_set_enabled(current_uid(), bool((request.get_json(silent=True) or {}).get("enabled"))))


@app.post("/api/autoplay/<action>")
@login_required
@csrf_protect
@rate_limit(max_calls=40, per_seconds=30)
def api_ap_action(action):
    if (g := _need_service()):
        return g
    uid = current_uid()
    mapping = {
        "start": service.autoplay_start, "stop": service.autoplay_stop,
        "pause": service.autoplay_pause, "resume": service.autoplay_resume,
        "emergency": service.autoplay_emergency,
    }
    fn = mapping.get(action)
    if not fn:
        return jsonify({"error": "Unknown action."}), 404
    return jsonify(fn(uid))


@app.post("/api/autoplay_config")
@login_required
@csrf_protect
@rate_limit(max_calls=40, per_seconds=30)
def api_ap_config():
    if (g := _need_service()):
        return g
    d = request.get_json(silent=True) or {}
    d.pop("csrf", None)
    return jsonify(service.autoplay_config(current_uid(), d))


# ----------------------------------------------------------------------------
# Custom messages
# ----------------------------------------------------------------------------
@app.get("/api/messages")
@login_required
@rate_limit(max_calls=60, per_seconds=10)
def api_messages():
    uid = current_login_id()
    return jsonify({"settings": db.get_msg_settings(uid), "messages": db.list_messages(uid)})


@app.post("/api/messages")
@login_required
@csrf_protect
@rate_limit(max_calls=60, per_seconds=30)
def api_messages_add():
    return jsonify(db.add_message(current_login_id(), (request.get_json(silent=True) or {}).get("text")))


@app.post("/api/messages/update")
@login_required
@csrf_protect
@rate_limit(max_calls=60, per_seconds=30)
def api_messages_update():
    d = request.get_json(silent=True) or {}
    return jsonify(db.update_message(current_login_id(), int(d.get("id", 0)), d.get("text")))


@app.post("/api/messages/delete")
@login_required
@csrf_protect
@rate_limit(max_calls=60, per_seconds=30)
def api_messages_delete():
    d = request.get_json(silent=True) or {}
    return jsonify(db.delete_message(current_login_id(), int(d.get("id", 0))))


@app.post("/api/messages/reorder")
@login_required
@csrf_protect
@rate_limit(max_calls=60, per_seconds=30)
def api_messages_reorder():
    d = request.get_json(silent=True) or {}
    return jsonify(db.reorder_messages(current_login_id(), d.get("ids", [])))


@app.post("/api/messages/import")
@login_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=30)
def api_messages_import():
    d = request.get_json(silent=True) or {}
    return jsonify(db.replace_messages(current_login_id(), d.get("texts", [])))


@app.post("/api/messages/settings")
@login_required
@csrf_protect
@rate_limit(max_calls=40, per_seconds=30)
def api_messages_settings():
    d = request.get_json(silent=True) or {}
    d.pop("csrf", None)
    return jsonify({"ok": True, "settings": db.save_msg_settings(current_login_id(), d)})


# ----------------------------------------------------------------------------
# Automation rules
# ----------------------------------------------------------------------------
@app.get("/api/rules")
@login_required
@rate_limit(max_calls=60, per_seconds=10)
def api_rules():
    return jsonify({"rules": db.list_rules(current_login_id())})


@app.post("/api/rules")
@login_required
@csrf_protect
@rate_limit(max_calls=40, per_seconds=30)
def api_rules_add():
    d = request.get_json(silent=True) or {}
    return jsonify(db.add_rule(current_login_id(), d.get("name"), d.get("trigger"),
                               d.get("actions", []), bool(d.get("enabled", True))))


@app.post("/api/rules/update")
@login_required
@csrf_protect
@rate_limit(max_calls=60, per_seconds=30)
def api_rules_update():
    d = request.get_json(silent=True) or {}
    return jsonify(db.update_rule(current_login_id(), int(d.get("id", 0)),
                                  name=d.get("name"), trigger=d.get("trigger"),
                                  actions=d.get("actions"), enabled=d.get("enabled")))


@app.post("/api/rules/delete")
@login_required
@csrf_protect
@rate_limit(max_calls=40, per_seconds=30)
def api_rules_delete():
    d = request.get_json(silent=True) or {}
    return jsonify(db.delete_rule(current_login_id(), int(d.get("id", 0))))


@app.post("/api/rules/reorder")
@login_required
@csrf_protect
@rate_limit(max_calls=40, per_seconds=30)
def api_rules_reorder():
    d = request.get_json(silent=True) or {}
    return jsonify(db.reorder_rules(current_login_id(), d.get("ids", [])))


# ----------------------------------------------------------------------------
# Account (self-service)
# ----------------------------------------------------------------------------
@app.post("/api/account/username")
@login_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=60)
def api_account_username():
    d = request.get_json(silent=True) or {}
    res = db.set_username(current_login_id(), d.get("username"))
    if res.get("ok"):
        session["username"] = str(d.get("username")).strip()
    return jsonify(res)


@app.post("/api/account/password")
@login_required
@csrf_protect
@rate_limit(max_calls=10, per_seconds=60)
def api_account_password():
    d = request.get_json(silent=True) or {}
    row = db.get_user(current_login_id())
    from werkzeug.security import check_password_hash
    if not row or not check_password_hash(row["password_hash"], d.get("current", "")):
        return jsonify({"ok": False, "error": "Current password is incorrect."})
    return jsonify(db.set_password(current_login_id(), d.get("new", "")))


# ----------------------------------------------------------------------------
# Users admin
# ----------------------------------------------------------------------------
@app.get("/api/users")
@admin_required
@rate_limit(max_calls=60, per_seconds=10)
def api_users():
    return jsonify({"users": db.list_users(), "me": current_login_id()})


@app.post("/api/users")
@admin_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=30)
def api_users_add():
    d = request.get_json(silent=True) or {}
    return jsonify(db.create_user(d.get("username"), d.get("password"), bool(d.get("is_admin"))))


@app.post("/api/users/delete")
@admin_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=30)
def api_users_delete():
    d = request.get_json(silent=True) or {}
    uid = int(d.get("id", 0))
    if uid == current_login_id():
        return jsonify({"ok": False, "error": "You cannot delete your own account."})
    admins = [u for u in db.list_users() if u["is_admin"] and not u["disabled"]]
    target = db.get_user(uid)
    if target and target["is_admin"] and len(admins) <= 1:
        return jsonify({"ok": False, "error": "Cannot delete the last admin."})
    return jsonify(db.delete_user(uid))


@app.post("/api/users/disable")
@admin_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=30)
def api_users_disable():
    d = request.get_json(silent=True) or {}
    uid = int(d.get("id", 0))
    disabled = bool(d.get("disabled"))
    if uid == current_login_id():
        return jsonify({"ok": False, "error": "You cannot disable your own account."})
    return jsonify(db.set_disabled(uid, disabled))


@app.post("/api/users/password")
@admin_required
@csrf_protect
@rate_limit(max_calls=20, per_seconds=30)
def api_users_password():
    d = request.get_json(silent=True) or {}
    return jsonify(db.set_password(int(d.get("id", 0)), d.get("password", "")))


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

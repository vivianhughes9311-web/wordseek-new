"""SQLite storage for accounts, custom messages and automation rules.

- Passwords are hashed with Werkzeug (scrypt); plaintext is never stored and the
  hash is never returned to the frontend (see safe_user()).
- One shared connection guarded by a lock (fine at this scale).
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "app.db"

ACTIVE_WINDOW = 120  # seconds; "active session" if seen within this window

_lock = threading.RLock()
_conn = None

DEFAULT_MSG_SETTINGS = {
    "enabled": False,
    "mode": "random",          # "random" | "sequential"
    "delay_ms": 800,
    "max_messages": 1,
    "on_new_game": True,
    "after_every_game": False,
    "only_wins": False,
    "only_losses": False,
}


def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            disabled INTEGER NOT NULL DEFAULT 0,
            created_at REAL, last_login REAL, last_seen REAL, session_id TEXT
        );
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rules(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL DEFAULT 'Rule',
            enabled INTEGER NOT NULL DEFAULT 1,
            trigger TEXT NOT NULL DEFAULT 'new_game',
            actions TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS settings(
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.commit()
    return conn


def conn():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = _connect()
    return _conn


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
def count_users() -> int:
    with _lock:
        return conn().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def safe_user(row) -> dict:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "disabled": bool(row["disabled"]),
        "created_at": row["created_at"],
        "last_login": row["last_login"],
        "last_seen": row["last_seen"],
        "active": bool(row["last_seen"] and (time.time() - row["last_seen"] < ACTIVE_WINDOW)),
    }


def create_user(username: str, password: str, is_admin: bool = False):
    username = (username or "").strip()
    if len(username) < 3 or len(username) > 32:
        return {"ok": False, "error": "Username must be 3–32 characters."}
    if len(password or "") < 6:
        return {"ok": False, "error": "Password must be at least 6 characters."}
    with _lock:
        try:
            cur = conn().execute(
                "INSERT INTO users(username, password_hash, is_admin, disabled, created_at) VALUES(?,?,?,0,?)",
                (username, generate_password_hash(password), 1 if is_admin else 0, time.time()),
            )
            conn().commit()
            return {"ok": True, "id": cur.lastrowid}
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "That username is taken."}


def get_by_username(username: str):
    with _lock:
        return conn().execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()


def get_user(user_id: int):
    with _lock:
        return conn().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def verify_login(username: str, password: str):
    row = get_by_username(username)
    if not row or not check_password_hash(row["password_hash"], password or ""):
        return None
    if row["disabled"]:
        return "disabled"
    return row


def list_users() -> list:
    with _lock:
        rows = conn().execute("SELECT * FROM users ORDER BY id").fetchall()
        return [safe_user(r) for r in rows]


def set_password(user_id: int, password: str):
    if len(password or "") < 6:
        return {"ok": False, "error": "Password must be at least 6 characters."}
    with _lock:
        conn().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), user_id))
        conn().commit()
    return {"ok": True}


def set_username(user_id: int, username: str):
    username = (username or "").strip()
    if len(username) < 3 or len(username) > 32:
        return {"ok": False, "error": "Username must be 3–32 characters."}
    with _lock:
        try:
            conn().execute("UPDATE users SET username=? WHERE id=?", (username, user_id))
            conn().commit()
            return {"ok": True}
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "That username is taken."}


def set_disabled(user_id: int, disabled: bool):
    with _lock:
        conn().execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))
        conn().commit()
    return {"ok": True}


def delete_user(user_id: int):
    with _lock:
        conn().execute("DELETE FROM users WHERE id=?", (user_id,))
        conn().execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        conn().execute("DELETE FROM rules WHERE user_id=?", (user_id,))
        conn().execute("DELETE FROM settings WHERE user_id=?", (user_id,))
        conn().commit()
    return {"ok": True}


def touch_login(user_id: int, session_id: str):
    with _lock:
        conn().execute("UPDATE users SET last_login=?, last_seen=?, session_id=? WHERE id=?",
                       (time.time(), time.time(), session_id, user_id))
        conn().commit()


def touch_seen(user_id: int):
    with _lock:
        conn().execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), user_id))
        conn().commit()


# --------------------------------------------------------------------------- #
# custom messages
# --------------------------------------------------------------------------- #
def list_messages(user_id: int) -> list:
    with _lock:
        rows = conn().execute("SELECT id, text FROM messages WHERE user_id=? ORDER BY position, id", (user_id,)).fetchall()
        return [{"id": r["id"], "text": r["text"]} for r in rows]


def message_texts(user_id: int) -> list:
    return [m["text"] for m in list_messages(user_id)]


def add_message(user_id: int, text: str):
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Message is empty."}
    if len(text) > 300:
        return {"ok": False, "error": "Message too long."}
    with _lock:
        pos = conn().execute("SELECT COALESCE(MAX(position), -1)+1 p FROM messages WHERE user_id=?", (user_id,)).fetchone()["p"]
        cur = conn().execute("INSERT INTO messages(user_id, position, text) VALUES(?,?,?)", (user_id, pos, text))
        conn().commit()
        return {"ok": True, "id": cur.lastrowid}


def update_message(user_id: int, mid: int, text: str):
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Message is empty."}
    with _lock:
        conn().execute("UPDATE messages SET text=? WHERE id=? AND user_id=?", (text[:300], mid, user_id))
        conn().commit()
    return {"ok": True}


def delete_message(user_id: int, mid: int):
    with _lock:
        conn().execute("DELETE FROM messages WHERE id=? AND user_id=?", (mid, user_id))
        conn().commit()
    return {"ok": True}


def reorder_messages(user_id: int, ids: list):
    with _lock:
        for pos, mid in enumerate(ids):
            conn().execute("UPDATE messages SET position=? WHERE id=? AND user_id=?", (pos, int(mid), user_id))
        conn().commit()
    return {"ok": True}


def replace_messages(user_id: int, texts: list):
    texts = [str(t).strip()[:300] for t in texts if str(t).strip()][:200]
    with _lock:
        conn().execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        for pos, t in enumerate(texts):
            conn().execute("INSERT INTO messages(user_id, position, text) VALUES(?,?,?)", (user_id, pos, t))
        conn().commit()
    return {"ok": True, "count": len(texts)}


# --------------------------------------------------------------------------- #
# message settings (per user)
# --------------------------------------------------------------------------- #
def get_msg_settings(user_id: int) -> dict:
    with _lock:
        row = conn().execute("SELECT data FROM settings WHERE user_id=?", (user_id,)).fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row["data"])
        except Exception:
            data = {}
    merged = dict(DEFAULT_MSG_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_MSG_SETTINGS})
    return merged


def save_msg_settings(user_id: int, patch: dict) -> dict:
    current = get_msg_settings(user_id)
    for k in DEFAULT_MSG_SETTINGS:
        if k in patch:
            current[k] = patch[k]
    current["mode"] = "sequential" if current.get("mode") == "sequential" else "random"
    current["delay_ms"] = max(0, min(20000, int(current.get("delay_ms", 800) or 0)))
    current["max_messages"] = max(1, min(10, int(current.get("max_messages", 1) or 1)))
    for b in ("enabled", "on_new_game", "after_every_game", "only_wins", "only_losses"):
        current[b] = bool(current.get(b))
    with _lock:
        conn().execute("INSERT INTO settings(user_id, data) VALUES(?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
                       (user_id, json.dumps(current)))
        conn().commit()
    return current


# --------------------------------------------------------------------------- #
# automation rules
# --------------------------------------------------------------------------- #
VALID_TRIGGERS = ("new_game", "won", "lost")
VALID_ACTIONS = ("wait", "send_text", "send_message", "start_autoplay", "stop_autoplay", "new_game_command")


def _clean_actions(actions) -> list:
    out = []
    for a in (actions or [])[:25]:
        t = a.get("type")
        if t not in VALID_ACTIONS:
            continue
        if t == "wait":
            out.append({"type": "wait", "ms": max(0, min(20000, int(a.get("ms", 1000) or 0)))})
        elif t == "send_text":
            txt = str(a.get("text", "")).strip()[:300]
            if txt:
                out.append({"type": "send_text", "text": txt})
        elif t == "send_message":
            out.append({"type": "send_message", "mode": "sequential" if a.get("mode") == "sequential" else "random"})
        else:
            out.append({"type": t})
    return out


def list_rules(user_id: int) -> list:
    with _lock:
        rows = conn().execute("SELECT * FROM rules WHERE user_id=? ORDER BY position, id", (user_id,)).fetchall()
    result = []
    for r in rows:
        try:
            actions = json.loads(r["actions"])
        except Exception:
            actions = []
        result.append({"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]),
                       "trigger": r["trigger"], "actions": actions})
    return result


def add_rule(user_id: int, name: str, trigger: str, actions: list, enabled: bool = True):
    trigger = trigger if trigger in VALID_TRIGGERS else "new_game"
    with _lock:
        pos = conn().execute("SELECT COALESCE(MAX(position), -1)+1 p FROM rules WHERE user_id=?", (user_id,)).fetchone()["p"]
        cur = conn().execute(
            "INSERT INTO rules(user_id, position, name, enabled, trigger, actions) VALUES(?,?,?,?,?,?)",
            (user_id, pos, (name or "Rule")[:60], 1 if enabled else 0, trigger, json.dumps(_clean_actions(actions))),
        )
        conn().commit()
        return {"ok": True, "id": cur.lastrowid}


def update_rule(user_id: int, rid: int, name=None, trigger=None, actions=None, enabled=None):
    with _lock:
        row = conn().execute("SELECT * FROM rules WHERE id=? AND user_id=?", (rid, user_id)).fetchone()
        if not row:
            return {"ok": False, "error": "Rule not found."}
        new_name = (name if name is not None else row["name"])[:60]
        new_trigger = trigger if trigger in VALID_TRIGGERS else row["trigger"]
        new_actions = json.dumps(_clean_actions(actions)) if actions is not None else row["actions"]
        new_enabled = (1 if enabled else 0) if enabled is not None else row["enabled"]
        conn().execute("UPDATE rules SET name=?, trigger=?, actions=?, enabled=? WHERE id=? AND user_id=?",
                       (new_name, new_trigger, new_actions, new_enabled, rid, user_id))
        conn().commit()
    return {"ok": True}


def delete_rule(user_id: int, rid: int):
    with _lock:
        conn().execute("DELETE FROM rules WHERE id=? AND user_id=?", (rid, user_id))
        conn().commit()
    return {"ok": True}


def reorder_rules(user_id: int, ids: list):
    with _lock:
        for pos, rid in enumerate(ids):
            conn().execute("UPDATE rules SET position=? WHERE id=? AND user_id=?", (pos, int(rid), user_id))
        conn().commit()
    return {"ok": True}

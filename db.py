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
            text TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 1
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
        CREATE TABLE IF NOT EXISTS prefs(
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS games(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at REAL,
            group_name TEXT, mode INTEGER, result TEXT,
            guesses INTEGER, seconds REAL, answer TEXT,
            board TEXT, guess_list TEXT
        );
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts REAL, category TEXT, text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_games_user ON games(user_id, id);
        CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id, id);
        """
    )
    # migrations for existing databases (ignore if the column already exists)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN weight INTEGER NOT NULL DEFAULT 1")
    except Exception:
        pass
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
        for tbl in ("users", "messages", "rules", "settings", "prefs", "games", "logs"):
            col = "id" if tbl == "users" else "user_id"
            conn().execute(f"DELETE FROM {tbl} WHERE {col}=?", (user_id,))
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
        rows = conn().execute("SELECT id, text, weight FROM messages WHERE user_id=? ORDER BY position, id", (user_id,)).fetchall()
        return [{"id": r["id"], "text": r["text"], "weight": r["weight"] or 1} for r in rows]


def message_texts(user_id: int) -> list:
    return [m["text"] for m in list_messages(user_id)]


def message_weighted(user_id: int) -> list:
    return [(m["text"], max(1, int(m["weight"] or 1))) for m in list_messages(user_id)]


def duplicate_message(user_id: int, mid: int):
    with _lock:
        row = conn().execute("SELECT text, weight FROM messages WHERE id=? AND user_id=?", (mid, user_id)).fetchone()
        if not row:
            return {"ok": False, "error": "Message not found."}
        pos = conn().execute("SELECT COALESCE(MAX(position), -1)+1 p FROM messages WHERE user_id=?", (user_id,)).fetchone()["p"]
        conn().execute("INSERT INTO messages(user_id, position, text, weight) VALUES(?,?,?,?)",
                       (user_id, pos, row["text"], row["weight"] or 1))
        conn().commit()
    return {"ok": True}


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


def update_message(user_id: int, mid: int, text: str = None, weight: int = None):
    with _lock:
        if text is not None:
            text = (text or "").strip()
            if not text:
                return {"ok": False, "error": "Message is empty."}
            conn().execute("UPDATE messages SET text=? WHERE id=? AND user_id=?", (text[:300], mid, user_id))
        if weight is not None:
            w = max(1, min(20, int(weight)))
            conn().execute("UPDATE messages SET weight=? WHERE id=? AND user_id=?", (w, mid, user_id))
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
    current["mode"] = current.get("mode") if current.get("mode") in ("random", "sequential", "weighted") else "random"
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


# --------------------------------------------------------------------------- #
# dashboard preferences (theme, animation, sound, etc.)
# --------------------------------------------------------------------------- #
DEFAULT_PREFS = {
    "theme": "cute",
    "anim_speed": "normal",     # off | slow | normal | fast
    "sound": True,
    "default_group": None,
}


def get_prefs(user_id: int) -> dict:
    with _lock:
        row = conn().execute("SELECT data FROM prefs WHERE user_id=?", (user_id,)).fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row["data"])
        except Exception:
            data = {}
    merged = dict(DEFAULT_PREFS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_PREFS})
    return merged


def save_prefs(user_id: int, patch: dict) -> dict:
    cur = get_prefs(user_id)
    for k in DEFAULT_PREFS:
        if k in patch:
            cur[k] = patch[k]
    cur["theme"] = str(cur.get("theme") or "cute")[:20]
    if cur.get("anim_speed") not in ("off", "slow", "normal", "fast"):
        cur["anim_speed"] = "normal"
    cur["sound"] = bool(cur.get("sound"))
    with _lock:
        conn().execute("INSERT INTO prefs(user_id, data) VALUES(?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
                       (user_id, json.dumps(cur)))
        conn().commit()
    return cur


# --------------------------------------------------------------------------- #
# games (history / replay + statistics source)
# --------------------------------------------------------------------------- #
def record_game(user_id, group_name, mode, result, guesses, seconds, answer, board, guess_list):
    with _lock:
        conn().execute(
            "INSERT INTO games(user_id, created_at, group_name, mode, result, guesses, seconds, answer, board, guess_list) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, time.time(), (group_name or "")[:80], int(mode or 0),
             ("won" if result == "won" else "lost"), int(guesses or 0),
             float(seconds or 0), (answer or "")[:24],
             (board or "")[:2000], json.dumps(guess_list or [])[:2000]),
        )
        # keep at most 500 games per user
        conn().execute(
            "DELETE FROM games WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM games WHERE user_id=? ORDER BY id DESC LIMIT 500)",
            (user_id, user_id))
        conn().commit()


def list_games(user_id: int, limit: int = 50, offset: int = 0) -> list:
    with _lock:
        rows = conn().execute("SELECT id, created_at, group_name, mode, result, guesses, seconds, answer "
                              "FROM games WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                              (user_id, limit, offset)).fetchall()
        return [dict(r) for r in rows]


def get_game(user_id: int, gid: int):
    with _lock:
        row = conn().execute("SELECT * FROM games WHERE id=? AND user_id=?", (gid, user_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["guess_list"] = json.loads(d.get("guess_list") or "[]")
    except Exception:
        d["guess_list"] = []
    return d


def compute_stats(user_id: int) -> dict:
    with _lock:
        rows = conn().execute("SELECT created_at, result, guesses, seconds FROM games WHERE user_id=? ORDER BY id",
                              (user_id,)).fetchall()
    games = len(rows)
    wins = sum(1 for r in rows if r["result"] == "won")
    losses = games - wins
    win_guesses = [r["guesses"] for r in rows if r["result"] == "won" and r["guesses"]]
    win_secs = [r["seconds"] for r in rows if r["result"] == "won" and r["seconds"]]

    # streaks (based on chronological win/loss)
    cur_streak = longest = run = 0
    for r in rows:
        if r["result"] == "won":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    # current streak = trailing wins
    for r in reversed(rows):
        if r["result"] == "won":
            cur_streak += 1
        else:
            break

    # daily series (last 14 days)
    import time as _t
    day = 86400
    now = _t.time()
    start = now - 13 * day
    buckets = {}
    for r in rows:
        if not r["created_at"] or r["created_at"] < start - day:
            continue
        d = int((r["created_at"]) // day)
        b = buckets.setdefault(d, {"games": 0, "wins": 0})
        b["games"] += 1
        if r["result"] == "won":
            b["wins"] += 1
    series = []
    base_day = int(start // day)
    for i in range(14):
        d = base_day + i
        b = buckets.get(d, {"games": 0, "wins": 0})
        series.append({"day": d * day, "games": b["games"], "wins": b["wins"]})

    def avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else 0

    return {
        "games": games, "wins": wins, "losses": losses,
        "win_rate": round(100 * wins / games) if games else 0,
        "current_streak": cur_streak, "longest_streak": longest,
        "avg_guesses": avg(win_guesses),
        "avg_solve_time": avg(win_secs),
        "fastest_solve": round(min(win_secs), 2) if win_secs else 0,
        "series": series,
    }


# --------------------------------------------------------------------------- #
# logs (searchable)
# --------------------------------------------------------------------------- #
LOG_CATEGORIES = ("login", "telegram", "guess", "autoplay", "automation", "settings", "warning", "error", "system")


def add_log(user_id, category, text):
    if not user_id:
        return
    cat = category if category in LOG_CATEGORIES else "system"
    with _lock:
        conn().execute("INSERT INTO logs(user_id, ts, category, text) VALUES(?,?,?,?)",
                       (user_id, time.time(), cat, str(text)[:280]))
        conn().execute(
            "DELETE FROM logs WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 1000)",
            (user_id, user_id))
        conn().commit()


def list_logs(user_id: int, category: str = None, query: str = None, limit: int = 200) -> list:
    sql = "SELECT ts, category, text FROM logs WHERE user_id=?"
    args = [user_id]
    if category and category in LOG_CATEGORIES:
        sql += " AND category=?"
        args.append(category)
    if query:
        sql += " AND text LIKE ?"
        args.append(f"%{query[:60]}%")
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(500, limit)))
    with _lock:
        rows = conn().execute(sql, args).fetchall()
        return [dict(r) for r in rows]

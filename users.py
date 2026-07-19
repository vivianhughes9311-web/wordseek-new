"""Per-user persistence (multi-account).

Each browser identity (a signed `uid` cookie) maps to one Telegram account and
its settings. Files live under DATA_DIR/users/<uid>.json.

Stored per user (the encrypted session is the only sensitive field, and it is
encrypted at rest via crypto_store):
  - account_name, phone_masked   (display only)
  - session_enc                  (AES-encrypted Telethon StringSession)
  - group_id, group_name
  - auto_send                    (simple one-shot best-guess sender)
  - autoplay {...}               (full autoplay configuration)
  - activity [...]               (recent events)
"""
import json
import os
import re
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
USERS_DIR = DATA_DIR / "users"
MAX_ACTIVITY = 60

DEFAULT_AUTOPLAY = {
    "enabled": False,          # Autoplay ON/OFF (master feature flag)
    "max_guesses": 30,         # personal per-game send cap (WordSeek allows /30)
    "delay_ms": 1500,          # delay before each guess
    "cooldown_ms": 4000,       # strict min gap between guesses
    "min_confidence": 0,       # 0..100; skip auto-send below this
    "auto_start_on_new_game": True,
    "auto_stop_after_game": False,
    "send_new_game_command": False,
    "new_game_command": "/new",
    "max_repeat": 3,           # stop if the same board repeats this many times
}

DEFAULT_USER = {
    "account_name": None,
    "phone_masked": None,
    "session_enc": None,
    "group_id": None,
    "group_name": None,
    "auto_send": False,
    "autoplay": dict(DEFAULT_AUTOPLAY),
    "activity": [],
}

_UID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_lock = threading.RLock()


def valid_uid(uid: str) -> bool:
    return bool(uid) and bool(_UID_RE.match(uid))


def _path(uid: str) -> Path:
    return USERS_DIR / f"{uid}.json"


def _ensure():
    try:
        USERS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _merge(data: dict) -> dict:
    merged = {**DEFAULT_USER, **{k: v for k, v in (data or {}).items() if k in DEFAULT_USER}}
    ap = dict(DEFAULT_AUTOPLAY)
    ap.update({k: v for k, v in (data.get("autoplay") or {}).items() if k in DEFAULT_AUTOPLAY})
    merged["autoplay"] = ap
    if not isinstance(merged.get("activity"), list):
        merged["activity"] = []
    merged["activity"] = merged["activity"][-MAX_ACTIVITY:]
    return merged


def load(uid: str) -> dict:
    with _lock:
        if not valid_uid(uid):
            return dict(DEFAULT_USER, autoplay=dict(DEFAULT_AUTOPLAY))
        p = _path(uid)
        if p.exists():
            try:
                return _merge(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        return _merge({})


def save(uid: str, data: dict) -> dict:
    with _lock:
        if not valid_uid(uid):
            return data
        _ensure()
        clean = _merge(data)
        try:
            tmp = _path(uid).with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_path(uid))
        except Exception:
            pass
        return clean


def all_uids() -> list:
    with _lock:
        if not USERS_DIR.exists():
            return []
        return [p.stem for p in USERS_DIR.glob("*.json") if valid_uid(p.stem)]

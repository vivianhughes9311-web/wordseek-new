"""Small, Railway-volume-friendly JSON storage.

Stores ONLY safe, non-secret data:
  - selected group id/name
  - auto-send toggle, delay, cooldown
  - recent activity log

Secrets (API id/hash, bot token, phone, session strings) are NEVER written
here — they live only in environment variables.
"""
import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
ACTIVITY_FILE = DATA_DIR / "activity.json"

# Only these keys are ever persisted (allow-list guards against leaking data).
DEFAULT_SETTINGS = {
    "group_id": None,
    "group_name": None,
    "auto_send": False,
    "delay_ms": 1500,
    "cooldown_ms": 8000,
    "one_guess_per_board": True,
}

MAX_ACTIVITY = 60
_lock = threading.RLock()


def _ensure_dir():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _atomic_write(path: Path, data) -> None:
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_settings() -> dict:
    with _lock:
        data = {}
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        merged = dict(DEFAULT_SETTINGS)
        for key in DEFAULT_SETTINGS:
            if key in data:
                merged[key] = data[key]
        return merged


def save_settings(settings: dict) -> dict:
    with _lock:
        clean = {key: settings.get(key, DEFAULT_SETTINGS[key]) for key in DEFAULT_SETTINGS}
        try:
            _atomic_write(SETTINGS_FILE, clean)
        except Exception:
            pass
        return clean


def load_activity() -> list:
    with _lock:
        if ACTIVITY_FILE.exists():
            try:
                data = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[-MAX_ACTIVITY:]
            except Exception:
                pass
        return []


def save_activity(items: list) -> None:
    with _lock:
        try:
            _atomic_write(ACTIVITY_FILE, list(items)[-MAX_ACTIVITY:])
        except Exception:
            pass

"""Background Telegram monitor + auto-guesser.

Runs a Telethon client inside ONE dedicated background thread that owns its own
asyncio event loop. Flask talks to it only through thread-safe helpers, so:
  * Telegram work never blocks the Flask request threads, and
  * a Telegram failure never crashes the web server.

Secrets are read exclusively from environment variables and are never returned
to the frontend or written to disk/logs.
"""
import asyncio
import logging
import os
import threading
import time
from collections import deque

import board as board_utils
import storage
from solver import solve_board

log = logging.getLogger("wordseek.telegram")

# Telethon is optional at import time: if it is missing the web app still runs,
# and the dashboard simply reports Telegram as "unavailable".
try:
    from telethon import TelegramClient, events
    from telethon.errors import FloodWaitError
    from telethon.sessions import StringSession

    TELETHON_AVAILABLE = True
    TELETHON_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when dependency missing
    TELETHON_AVAILABLE = False
    TELETHON_IMPORT_ERROR = str(exc)

# Keep third-party noise (and any incidental identifiers) out of our logs.
logging.getLogger("telethon").setLevel(logging.WARNING)


def _now() -> float:
    return time.time()


class TelegramManager:
    def __init__(self):
        # --- credentials (env only) ---
        try:
            self.api_id = int(os.environ.get("TELEGRAM_API_ID", "") or 0)
        except ValueError:
            self.api_id = 0
        self.api_hash = os.environ.get("TELEGRAM_API_HASH", "") or ""
        self._string_session = os.environ.get("TELEGRAM_STRING_SESSION", "") or ""
        self._bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "") or ""

        mode = (os.environ.get("TELEGRAM_MODE", "") or "").lower()
        if mode not in ("user", "bot"):
            mode = "bot" if (self._bot_token and not self._string_session) else "user"
        self.mode = mode

        self._autostart = (os.environ.get("TELEGRAM_AUTOSTART", "1") == "1")

        # --- persisted settings ---
        self.settings = storage.load_settings()

        # --- runtime state ---
        self.connected = False
        self.authorized = False
        self.paused = False
        self.connecting = False
        self.reconnecting = False
        self.cooldown_until = 0.0
        self.last_message_ts = None
        self.last_board = None
        self.last_result = None
        self.last_sent = None
        self.last_error = None
        self._me_name = None

        self.activity = deque(storage.load_activity(), maxlen=storage.MAX_ACTIVITY)
        self.errors = deque(maxlen=25)

        # de-dupe of processed and already-sent boards
        self._seen = deque(maxlen=300)
        self._seen_set = set()
        self._sent_boards = set()

        self._state_lock = threading.RLock()
        self.client = None
        self._watchdog_task = None
        self._aio_lock = None  # created inside the loop

        # --- background event loop ---
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="tg-loop", daemon=True)
        self.thread.start()

        if self.configured() and self._autostart:
            # fire-and-forget initial connect
            try:
                asyncio.run_coroutine_threadsafe(self._connect(), self.loop)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # loop plumbing
    # ------------------------------------------------------------------ #
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self._aio_lock = asyncio.Lock()
        self.loop.run_forever()

    def _submit(self, coro, timeout=45):
        """Run a coroutine on the loop from a Flask thread and wait for it."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def configured(self) -> bool:
        return bool(
            TELETHON_AVAILABLE
            and self.api_id
            and self.api_hash
            and (self._string_session or self._bot_token)
        )

    def _log_activity(self, kind: str, text: str):
        entry = {"ts": _now(), "type": kind, "text": text[:200]}
        with self._state_lock:
            self.activity.append(entry)
            items = list(self.activity)
        storage.save_activity(items)
        log.info("[%s] %s", kind, text)

    def _set_error(self, text: str):
        with self._state_lock:
            self.last_error = text[:200]
            self.errors.append({"ts": _now(), "text": text[:200]})
        log.warning("telegram error: %s", text)

    def _set_cooldown(self, seconds: float):
        self.cooldown_until = _now() + max(0.0, float(seconds))

    def _mark_seen(self, key: str):
        if key in self._seen_set:
            return
        self._seen.append(key)
        self._seen_set.add(key)
        # keep the set bounded alongside the deque
        while len(self._seen_set) > len(self._seen):
            oldest = self._seen.popleft() if self._seen else None
            if oldest is not None:
                self._seen_set.discard(oldest)

    def _is_ready(self) -> bool:
        return bool(self.client and self.connected and self.authorized)

    # ------------------------------------------------------------------ #
    # connection lifecycle
    # ------------------------------------------------------------------ #
    async def _connect(self):
        async with self._aio_lock:
            if self._is_ready():
                return {"ok": True}
            self.connecting = True
            try:
                if self.mode == "bot":
                    self.client = TelegramClient(StringSession(), self.api_id, self.api_hash)
                    await self.client.start(bot_token=self._bot_token)
                    self.authorized = True
                else:
                    session = StringSession(self._string_session) if self._string_session else StringSession()
                    self.client = TelegramClient(session, self.api_id, self.api_hash)
                    await self.client.connect()
                    self.authorized = await self.client.is_user_authorized()
                    if not self.authorized:
                        self._set_error("Session not authorized — provide a valid TELEGRAM_STRING_SESSION.")
                        self.connected = self.client.is_connected()
                        return {"ok": False, "error": "not authorized"}

                self.connected = self.client.is_connected()
                try:
                    me = await self.client.get_me()
                    self._me_name = getattr(me, "first_name", None) or getattr(me, "username", None) or "account"
                except Exception:
                    self._me_name = None

                self.client.add_event_handler(self._on_message, events.NewMessage())
                self._ensure_watchdog()
                self._log_activity("system", "Connected to Telegram")
                return {"ok": True}
            except FloodWaitError as exc:
                self._set_cooldown(exc.seconds)
                self._set_error(f"FloodWait during connect — waiting {exc.seconds}s")
                return {"ok": False, "error": f"flood wait {exc.seconds}s"}
            except Exception as exc:
                self._set_error(f"Connect failed: {type(exc).__name__}")
                return {"ok": False, "error": "connect failed"}
            finally:
                self.connecting = False

    async def _disconnect(self):
        try:
            if self.client:
                await self.client.disconnect()
        except Exception:
            pass
        finally:
            self.connected = False
            self.authorized = False
            self._log_activity("system", "Disconnected from Telegram")
        return {"ok": True}

    def _ensure_watchdog(self):
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self.loop.create_task(self._watchdog())

    async def _watchdog(self):
        """Auto-reconnect if the connection drops (e.g. after a Railway restart)."""
        while True:
            await asyncio.sleep(15)
            try:
                if self.client and self.authorized and not self.client.is_connected():
                    self.reconnecting = True
                    self._log_activity("system", "Connection lost — reconnecting…")
                    await self.client.connect()
                    self.connected = self.client.is_connected()
                    if self.connected:
                        self._log_activity("system", "Reconnected")
                    self.reconnecting = False
                else:
                    self.connected = bool(self.client and self.client.is_connected())
            except FloodWaitError as exc:
                self._set_cooldown(exc.seconds)
                self.reconnecting = False
            except Exception as exc:
                self._set_error(f"Reconnect error: {type(exc).__name__}")
                self.reconnecting = False

    # ------------------------------------------------------------------ #
    # message handling
    # ------------------------------------------------------------------ #
    def _same_chat(self, chat_id, target) -> bool:
        try:
            return int(chat_id) == int(target)
        except (TypeError, ValueError):
            return False

    async def _on_message(self, event):
        try:
            target = self.settings.get("group_id")
            if target is None or self.paused:
                return
            if not self._same_chat(event.chat_id, target):
                return

            self.last_message_ts = _now()
            text = event.raw_text or ""
            detected = board_utils.detect_board(text)
            if not detected:
                return

            key = detected["key"]
            if key in self._seen_set:
                return
            self._mark_seen(key)

            start = time.perf_counter()
            result = solve_board(text, detected["mode"])
            solve_ms = round((time.perf_counter() - start) * 1000, 1)

            with self._state_lock:
                self.last_board = {
                    "text": board_utils.clean_board_text(text, detected["mode"]),
                    "mode": detected["mode"],
                }
                self.last_result = {
                    "answer": result["answer"],
                    "guesses": result["guesses"][:5],
                    "count": result["count"],
                    "solve_ms": solve_ms,
                }
            self._log_activity(
                "board",
                f"{detected['mode']}-letter board → "
                + (f"{result['answer'].upper()} ({result['count']} left)" if result["answer"] else "no match"),
            )

            if self.settings.get("auto_send") and result["answer"]:
                await self._maybe_auto_send(key, result)
        except Exception as exc:
            self._set_error(f"Message handler error: {type(exc).__name__}")

    async def _maybe_auto_send(self, key: str, result: dict):
        if self.settings.get("one_guess_per_board", True) and key in self._sent_boards:
            return
        if _now() < self.cooldown_until:
            self._log_activity("system", "Auto-send skipped (cooldown active)")
            return
        delay = max(0, int(self.settings.get("delay_ms", 1500))) / 1000.0
        if delay:
            await asyncio.sleep(delay)
        # re-check cooldown/dup after the delay
        if _now() < self.cooldown_until:
            return
        if self.settings.get("one_guess_per_board", True) and key in self._sent_boards:
            return
        await self._do_send(result["answer"], auto=True, key=key)

    async def _do_send(self, word: str, auto: bool, key: str = None):
        if not self._is_ready():
            return {"ok": False, "error": "Not connected"}
        target = self.settings.get("group_id")
        if target is None:
            return {"ok": False, "error": "No target group selected"}
        try:
            await self.client.send_message(int(target), word.upper())
            self.last_sent = {"word": word.upper(), "ts": _now(), "auto": auto}
            self._set_cooldown(int(self.settings.get("cooldown_ms", 8000)) / 1000.0)
            if key:
                self._sent_boards.add(key)
            self._log_activity("send", f"{'Auto' if auto else 'Manual'} sent: {word.upper()}")
            return {"ok": True, "word": word.upper()}
        except FloodWaitError as exc:
            self._set_cooldown(exc.seconds)
            self._set_error(f"FloodWait on send — waiting {exc.seconds}s")
            return {"ok": False, "error": f"flood wait {exc.seconds}s"}
        except Exception as exc:
            self._set_error(f"Send failed: {type(exc).__name__}")
            return {"ok": False, "error": "send failed"}

    async def _list_groups(self):
        if not self._is_ready():
            return {"ok": False, "error": "Not connected", "groups": []}
        groups = []
        try:
            async for dialog in self.client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    groups.append({"id": dialog.id, "name": (dialog.name or "Unnamed")[:80]})
                if len(groups) >= 200:
                    break
        except FloodWaitError as exc:
            self._set_cooldown(exc.seconds)
            return {"ok": False, "error": f"flood wait {exc.seconds}s", "groups": []}
        except Exception as exc:
            self._set_error(f"Group list failed: {type(exc).__name__}")
            return {"ok": False, "error": "could not load groups", "groups": []}
        return {"ok": True, "groups": groups}

    # ------------------------------------------------------------------ #
    # public (thread-safe) API used by Flask
    # ------------------------------------------------------------------ #
    def connect(self):
        if not TELETHON_AVAILABLE:
            return {"ok": False, "error": "Telethon is not installed."}
        if not self.configured():
            return {"ok": False, "error": "Telegram is not configured (check environment variables)."}
        try:
            return self._submit(self._connect(), timeout=90)
        except Exception as exc:
            self._set_error(f"Connect error: {type(exc).__name__}")
            return {"ok": False, "error": "connect timed out"}

    def disconnect(self):
        if not TELETHON_AVAILABLE:
            return {"ok": False, "error": "Telethon is not installed."}
        try:
            return self._submit(self._disconnect(), timeout=30)
        except Exception:
            return {"ok": False, "error": "disconnect error"}

    def pause(self):
        self.paused = True
        self._log_activity("system", "Monitoring paused")
        return {"ok": True}

    def resume(self):
        self.paused = False
        self._log_activity("system", "Monitoring resumed")
        return {"ok": True}

    def list_groups(self):
        if not self._is_ready():
            return {"ok": False, "error": "Connect to Telegram first.", "groups": []}
        try:
            return self._submit(self._list_groups(), timeout=60)
        except Exception:
            return {"ok": False, "error": "group list timed out", "groups": []}

    def select_group(self, group_id, group_name=None):
        try:
            gid = int(group_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid group id."}
        name = (str(group_name).strip()[:80] if group_name else str(gid))
        self.settings["group_id"] = gid
        self.settings["group_name"] = name
        self.settings = storage.save_settings(self.settings)
        self._sent_boards.clear()
        self._log_activity("system", f"Target group set: {name}")
        return {"ok": True, "group_id": gid, "group_name": name}

    def set_auto_send(self, enabled: bool):
        self.settings["auto_send"] = bool(enabled)
        self.settings = storage.save_settings(self.settings)
        self._log_activity("system", f"Auto-send {'enabled' if enabled else 'disabled'}")
        return {"ok": True, "auto_send": bool(enabled)}

    def set_delay(self, delay_ms):
        try:
            value = int(delay_ms)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Delay must be a number."}
        value = max(0, min(60000, value))
        self.settings["delay_ms"] = value
        self.settings = storage.save_settings(self.settings)
        return {"ok": True, "delay_ms": value}

    def send_guess(self, word: str):
        if not self._is_ready():
            return {"ok": False, "error": "Not connected to Telegram."}
        if self.settings.get("group_id") is None:
            return {"ok": False, "error": "No target group selected."}
        candidate = str(word or "").strip().lower()
        if not candidate.isalpha() or len(candidate) not in (4, 5):
            return {"ok": False, "error": "Invalid guess."}
        # Only allow words from the most recent solve, to prevent arbitrary sends.
        allowed = set()
        if self.last_result:
            allowed = {g.lower() for g in self.last_result.get("guesses", [])}
            if self.last_result.get("answer"):
                allowed.add(self.last_result["answer"].lower())
        if allowed and candidate not in allowed:
            return {"ok": False, "error": "Guess is not one of the current results."}
        try:
            return self._submit(self._do_send(candidate, auto=False), timeout=30)
        except Exception:
            return {"ok": False, "error": "send timed out"}

    # ------------------------------------------------------------------ #
    # sanitized status for the dashboard (no secrets ever)
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        with self._state_lock:
            cooldown_remaining = max(0, int((self.cooldown_until - _now()) * 1000))
            return {
                "available": TELETHON_AVAILABLE,
                "configured": self.configured(),
                "mode": self.mode if self.configured() else None,
                "connected": self.connected,
                "authorized": self.authorized,
                "connecting": self.connecting,
                "reconnecting": self.reconnecting,
                "paused": self.paused,
                "account": self._me_name,
                "group": (
                    {"id": self.settings.get("group_id"), "name": self.settings.get("group_name")}
                    if self.settings.get("group_id") is not None
                    else None
                ),
                "auto_send": bool(self.settings.get("auto_send")),
                "delay_ms": int(self.settings.get("delay_ms", 1500)),
                "cooldown_ms": int(self.settings.get("cooldown_ms", 8000)),
                "cooldown_remaining_ms": cooldown_remaining,
                "last_message_ts": self.last_message_ts,
                "last_board": self.last_board,
                "last_result": self.last_result,
                "last_sent": self.last_sent,
                "last_error": self.last_error,
                "activity": list(self.activity)[-30:][::-1],
                "errors": list(self.errors)[-10:][::-1],
            }


# --- singleton ------------------------------------------------------------- #
_manager = None
_manager_lock = threading.Lock()


def get_manager() -> TelegramManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TelegramManager()
    return _manager

"""Multi-account Telegram service with phone/OTP login + autoplay.

One background thread owns a single asyncio loop. Every logged-in user has their
own Telethon client (their own account) running on that loop, with an isolated
AutoplayEngine. Flask talks to this service through thread-safe helpers.

Login is a "Login with Telegram" flow: the app uses one app-level API_ID/API_HASH
(set by the operator), and each user signs in with their phone number + the code
Telegram sends them (+ 2FA password if enabled). The resulting session string is
encrypted at rest (crypto_store) and never sent to the browser or logged.
"""
import asyncio
import logging
import os
import threading
import time
from collections import deque

import board as board_utils
import crypto_store
import users as user_store
from autoplay import AutoplayEngine
from solver import solve_board

log = logging.getLogger("wordseek.telegram")

try:
    from telethon import TelegramClient, events
    from telethon.errors import (
        FloodWaitError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberInvalidError,
        SessionPasswordNeededError,
    )
    from telethon.sessions import StringSession

    TELETHON_AVAILABLE = True
except Exception as exc:  # pragma: no cover
    TELETHON_AVAILABLE = False

logging.getLogger("telethon").setLevel(logging.WARNING)


def _now():
    return time.time()


def _mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 4:
        return "•••"
    return "+" + "•" * (len(digits) - 4) + digits[-4:]


class UserRuntime:
    def __init__(self, uid, data, service):
        self.uid = uid
        self.data = data
        self.service = service
        self.client = None
        self.connected = False
        self.authorized = False
        self.account_name = data.get("account_name")
        self.phone_masked = data.get("phone_masked")
        self.last_board = None
        self.last_result = None
        self.last_message_ts = None
        self.errors = deque(maxlen=15)
        self.activity = deque(data.get("activity", []), maxlen=user_store.MAX_ACTIVITY)
        self._connecting = False
        self._simple_sent = set()
        self._simple_cooldown = 0.0
        self.engine = AutoplayEngine(
            config=self.data["autoplay"],
            send=lambda text: self.service._send_text(self, text),
            on_activity=self._on_activity,
            on_history=self._on_history,
            loop=self.service.loop,
        )

    # --- logging / persistence ---
    def _on_activity(self, kind, text):
        entry = {"ts": _now(), "type": kind, "text": str(text)[:180]}
        self.activity.append(entry)
        self.data["activity"] = list(self.activity)
        user_store.save(self.uid, self.data)

    def _on_history(self, entry):
        entry = {"ts": _now(), "type": entry.get("result", "game"),
                 "text": f"{entry.get('result','?').upper()}: {entry.get('word','')} "
                         f"in {entry.get('guesses','?')} guesses"}
        self.activity.append(entry)
        self.data["activity"] = list(self.activity)
        user_store.save(self.uid, self.data)

    def _set_error(self, text):
        self.errors.append({"ts": _now(), "text": str(text)[:180]})
        log.warning("user error: %s", text)

    async def _simple_auto_send(self, board_key, answer):
        if board_key in self._simple_sent:
            return
        if _now() < self._simple_cooldown:
            return
        delay = max(0, int(self.data["autoplay"].get("delay_ms", 1500))) / 1000.0
        if delay:
            await asyncio.sleep(delay)
        res = await self.service._send_text(self, answer.upper())
        if res.get("ok"):
            self._simple_sent.add(board_key)
            self._simple_cooldown = _now() + int(self.data["autoplay"].get("cooldown_ms", 4000)) / 1000.0
            self._on_activity("send", f"Auto-sent best guess: {answer.upper()}")
        elif res.get("flood_wait"):
            self._simple_cooldown = _now() + int(res["flood_wait"])


class TelegramService:
    def __init__(self):
        try:
            self.api_id = int(os.environ.get("TELEGRAM_API_ID", "") or 0)
        except ValueError:
            self.api_id = 0
        self.api_hash = os.environ.get("TELEGRAM_API_HASH", "") or ""

        self.runtimes = {}
        self.pending = {}  # uid -> {client, phone, hash}
        self._rt_lock = threading.RLock()

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="tg-loop", daemon=True)
        self.thread.start()

    # ------------------------------------------------------------------ #
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def configured(self):
        return bool(TELETHON_AVAILABLE and self.api_id and self.api_hash)

    # ------------------------------------------------------------------ #
    def get_runtime(self, uid):
        with self._rt_lock:
            rt = self.runtimes.get(uid)
            if rt is None:
                rt = UserRuntime(uid, user_store.load(uid), self)
                self.runtimes[uid] = rt
            return rt

    def _register_handler(self, rt):
        async def handler(event):
            try:
                gid = rt.data.get("group_id")
                if gid is None:
                    return
                try:
                    if int(event.chat_id) != int(gid):
                        return
                except (TypeError, ValueError):
                    return
                text = event.raw_text or ""
                rt.last_message_ts = _now()
                detected = board_utils.detect_board(text)
                if detected:
                    try:
                        start = time.perf_counter()
                        result = solve_board(text, detected["mode"])
                        rt.last_board = {"text": board_utils.clean_board_text(text, detected["mode"]),
                                         "mode": detected["mode"]}
                        rt.last_result = {"answer": result["answer"], "guesses": result["guesses"][:5],
                                          "count": result["count"],
                                          "solve_ms": round((time.perf_counter() - start) * 1000, 1)}
                    except Exception:
                        pass
                if rt.data["autoplay"].get("enabled"):
                    await rt.engine.handle(text, event.id)
                elif rt.data.get("auto_send") and detected and rt.last_result and rt.last_result["answer"]:
                    await rt._simple_auto_send(detected["key"], rt.last_result["answer"])
            except Exception as exc:
                rt._set_error(f"Handler error: {type(exc).__name__}")

        rt.client.add_event_handler(handler, events.NewMessage())

    # ------------------------------------------------------------------ #
    # login flow (phone -> code -> optional 2FA)
    # ------------------------------------------------------------------ #
    def login_start(self, uid, phone):
        if not self.configured():
            return {"ok": False, "error": "Telegram login is not configured on this server."}
        phone = str(phone or "").strip()
        if len(phone) < 6:
            return {"ok": False, "error": "Enter a valid phone number in international format."}
        try:
            return self._submit(self._login_start(uid, phone), timeout=60)
        except Exception:
            return {"ok": False, "error": "Could not start login. Try again."}

    async def _login_start(self, uid, phone):
        # discard any previous pending client
        old = self.pending.pop(uid, None)
        if old:
            try:
                await old["client"].disconnect()
            except Exception:
                pass
        client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            return {"ok": False, "error": "That phone number is invalid."}
        except FloodWaitError as exc:
            return {"ok": False, "error": f"Too many attempts. Wait {exc.seconds}s and retry."}
        except Exception:
            return {"ok": False, "error": "Could not send the code. Check the number and try again."}
        self.pending[uid] = {"client": client, "phone": phone, "hash": sent.phone_code_hash}
        return {"ok": True, "next": "code"}

    def login_code(self, uid, code):
        p = self.pending.get(uid)
        if not p:
            return {"ok": False, "error": "No login in progress. Start again."}
        try:
            return self._submit(self._login_code(uid, str(code or "").strip()), timeout=60)
        except Exception:
            return {"ok": False, "error": "Sign-in failed. Try again."}

    async def _login_code(self, uid, code):
        p = self.pending.get(uid)
        client, phone = p["client"], p["phone"]
        try:
            await client.sign_in(phone, code, phone_code_hash=p["hash"])
        except SessionPasswordNeededError:
            return {"ok": True, "next": "password"}
        except PhoneCodeInvalidError:
            return {"ok": False, "error": "That code is incorrect."}
        except PhoneCodeExpiredError:
            return {"ok": False, "error": "That code expired. Start again."}
        except FloodWaitError as exc:
            return {"ok": False, "error": f"Too many attempts. Wait {exc.seconds}s."}
        except Exception:
            return {"ok": False, "error": "Sign-in failed. Check the code."}
        return await self._finalize(uid, client, phone)

    def login_password(self, uid, password):
        p = self.pending.get(uid)
        if not p:
            return {"ok": False, "error": "No login in progress. Start again."}
        try:
            return self._submit(self._login_password(uid, str(password or "")), timeout=60)
        except Exception:
            return {"ok": False, "error": "Sign-in failed. Try again."}

    async def _login_password(self, uid, password):
        p = self.pending.get(uid)
        client, phone = p["client"], p["phone"]
        try:
            await client.sign_in(password=password)
        except Exception:
            return {"ok": False, "error": "Incorrect 2FA password."}
        return await self._finalize(uid, client, phone)

    async def _finalize(self, uid, client, phone):
        try:
            session_str = StringSession.save(client.session)
            me = await client.get_me()
            name = getattr(me, "first_name", None) or getattr(me, "username", None) or "account"
        except Exception:
            return {"ok": False, "error": "Could not finalize login."}

        rt = self.get_runtime(uid)
        rt.data["session_enc"] = crypto_store.encrypt(session_str)
        rt.data["account_name"] = name
        rt.data["phone_masked"] = _mask_phone(phone)
        user_store.save(uid, rt.data)

        rt.client = client
        rt.connected = True
        rt.authorized = True
        rt.account_name = name
        rt.phone_masked = rt.data["phone_masked"]
        self._register_handler(rt)
        rt._on_activity("system", "Telegram account connected")
        self.pending.pop(uid, None)
        return {"ok": True, "next": "done", "account": name}

    # ------------------------------------------------------------------ #
    # reconnect from stored session (used after restarts)
    # ------------------------------------------------------------------ #
    def ensure_connected(self, uid):
        rt = self.get_runtime(uid)
        if not self.configured() or rt.connected or rt._connecting:
            return
        if not rt.data.get("session_enc"):
            return
        rt._connecting = True
        asyncio.run_coroutine_threadsafe(self._connect_stored(rt), self.loop)

    async def _connect_stored(self, rt):
        try:
            session_str = crypto_store.decrypt(rt.data.get("session_enc"))
            if not session_str:
                rt._set_error("Stored session could not be read (SECRET_KEY changed?). Please re-login.")
                rt.data["session_enc"] = None
                user_store.save(rt.uid, rt.data)
                return
            client = TelegramClient(StringSession(session_str), self.api_id, self.api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                rt._set_error("Session expired. Please re-login.")
                rt.data["session_enc"] = None
                user_store.save(rt.uid, rt.data)
                return
            rt.client = client
            rt.connected = True
            rt.authorized = True
            me = await client.get_me()
            rt.account_name = getattr(me, "first_name", None) or rt.account_name
            self._register_handler(rt)
            rt._on_activity("system", "Reconnected to Telegram")
        except FloodWaitError as exc:
            rt._set_error(f"FloodWait {exc.seconds}s during reconnect")
        except Exception as exc:
            rt._set_error(f"Reconnect failed: {type(exc).__name__}")
        finally:
            rt._connecting = False

    # ------------------------------------------------------------------ #
    def logout(self, uid):
        try:
            return self._submit(self._logout(uid), timeout=30)
        except Exception:
            return {"ok": False, "error": "logout error"}

    async def _logout(self, uid):
        rt = self.runtimes.get(uid)
        if rt:
            rt.engine.emergency_stop()
            try:
                if rt.client:
                    await rt.client.log_out()
            except Exception:
                try:
                    if rt.client:
                        await rt.client.disconnect()
                except Exception:
                    pass
            rt.client = None
            rt.connected = False
            rt.authorized = False
            rt.data["session_enc"] = None
            rt.data["account_name"] = None
            rt.data["phone_masked"] = None
            user_store.save(uid, rt.data)
            rt._on_activity("system", "Logged out of Telegram")
        return {"ok": True}

    # ------------------------------------------------------------------ #
    async def _send_text(self, rt, text):
        gid = rt.data.get("group_id")
        if gid is None:
            return {"ok": False, "error": "No target group selected"}
        if not (rt.client and rt.connected):
            return {"ok": False, "error": "Not connected"}
        try:
            await rt.client.send_message(int(gid), str(text))
            return {"ok": True}
        except FloodWaitError as exc:
            return {"ok": False, "flood_wait": int(exc.seconds), "error": f"flood wait {exc.seconds}s"}
        except Exception as exc:
            rt._set_error(f"Send failed: {type(exc).__name__}")
            return {"ok": False, "error": "send failed"}

    # ------------------------------------------------------------------ #
    def list_groups(self, uid):
        rt = self.get_runtime(uid)
        if not (rt.client and rt.connected):
            return {"ok": False, "error": "Connect your Telegram account first.", "groups": []}
        try:
            return self._submit(self._list_groups(rt), timeout=60)
        except Exception:
            return {"ok": False, "error": "group list timed out", "groups": []}

    async def _list_groups(self, rt):
        groups = []
        try:
            async for dialog in rt.client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    groups.append({"id": dialog.id, "name": (dialog.name or "Unnamed")[:80]})
                if len(groups) >= 200:
                    break
        except FloodWaitError as exc:
            return {"ok": False, "error": f"flood wait {exc.seconds}s", "groups": []}
        except Exception:
            return {"ok": False, "error": "could not load groups", "groups": []}
        return {"ok": True, "groups": groups}

    def select_group(self, uid, group_id, group_name=None):
        rt = self.get_runtime(uid)
        try:
            gid = int(group_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid group id."}
        rt.data["group_id"] = gid
        rt.data["group_name"] = (str(group_name).strip()[:80] if group_name else str(gid))
        user_store.save(uid, rt.data)
        rt._simple_sent.clear()
        rt.engine.stop()
        rt._on_activity("system", f"Target group set: {rt.data['group_name']}")
        return {"ok": True, "group_id": gid, "group_name": rt.data["group_name"]}

    def send_guess(self, uid, word):
        rt = self.get_runtime(uid)
        if not (rt.client and rt.connected):
            return {"ok": False, "error": "Not connected."}
        if rt.data.get("group_id") is None:
            return {"ok": False, "error": "No target group selected."}
        candidate = str(word or "").strip().lower()
        if not candidate.isalpha() or len(candidate) not in (4, 5):
            return {"ok": False, "error": "Invalid guess."}
        allowed = set()
        if rt.last_result:
            allowed = {g.lower() for g in rt.last_result.get("guesses", [])}
            if rt.last_result.get("answer"):
                allowed.add(rt.last_result["answer"].lower())
        if allowed and candidate not in allowed:
            return {"ok": False, "error": "Guess is not one of the current results."}
        try:
            res = self._submit(self._send_text(rt, candidate.upper()), timeout=30)
            if res.get("ok"):
                rt._on_activity("send", f"Manual sent: {candidate.upper()}")
            return res
        except Exception:
            return {"ok": False, "error": "send timed out"}

    def set_auto_send(self, uid, enabled):
        rt = self.get_runtime(uid)
        rt.data["auto_send"] = bool(enabled)
        user_store.save(uid, rt.data)
        return {"ok": True, "auto_send": bool(enabled)}

    # --- autoplay controls ---
    def _ap(self, uid, fn, *args):
        rt = self.get_runtime(uid)
        # engine methods mutate state; run them on the loop for thread-safety
        def call():
            fn(rt.engine, *args)
        try:
            self._submit(_wrap(call), timeout=15)
        except Exception:
            call()
        user_store.save(uid, rt.data)
        return {"ok": True, "autoplay": rt.engine.status()}

    def autoplay_set_enabled(self, uid, value):
        return self._ap(uid, lambda e, v: e.set_enabled(v), bool(value))

    def autoplay_start(self, uid):
        return self._ap(uid, lambda e: e.start())

    def autoplay_stop(self, uid):
        return self._ap(uid, lambda e: e.stop())

    def autoplay_pause(self, uid):
        return self._ap(uid, lambda e: e.pause())

    def autoplay_resume(self, uid):
        return self._ap(uid, lambda e: e.resume())

    def autoplay_emergency(self, uid):
        return self._ap(uid, lambda e: e.emergency_stop())

    def autoplay_config(self, uid, patch):
        rt = self.get_runtime(uid)
        clean = {}
        for key in ("max_guesses", "delay_ms", "cooldown_ms", "min_confidence",
                    "auto_start_on_new_game", "auto_stop_after_game",
                    "send_new_game_command", "new_game_command", "max_repeat"):
            if key in patch:
                clean[key] = patch[key]
        clean = _sanitize_config(clean)
        rt.engine.update_config(clean)
        rt.data["autoplay"].update(clean)
        user_store.save(uid, rt.data)
        return {"ok": True, "autoplay": rt.engine.status()}

    # ------------------------------------------------------------------ #
    def status(self, uid):
        rt = self.get_runtime(uid)
        # opportunistically reconnect a stored session after a restart
        if self.configured() and not rt.connected and rt.data.get("session_enc"):
            self.ensure_connected(uid)
        return {
            "available": TELETHON_AVAILABLE,
            "configured": self.configured(),
            "connected": rt.connected,
            "authorized": rt.authorized,
            "account": rt.account_name,
            "phone": rt.phone_masked,
            "login_pending": uid in self.pending,
            "group": ({"id": rt.data.get("group_id"), "name": rt.data.get("group_name")}
                      if rt.data.get("group_id") is not None else None),
            "auto_send": bool(rt.data.get("auto_send")),
            "last_message_ts": rt.last_message_ts,
            "last_board": rt.last_board,
            "last_result": rt.last_result,
            "errors": list(rt.errors)[-8:][::-1],
            "activity": list(rt.activity)[-25:][::-1],
            "autoplay": rt.engine.status(),
        }


async def _wrap(fn):
    fn()


def _sanitize_config(clean):
    if "max_guesses" in clean:
        clean["max_guesses"] = max(1, min(20, int(clean["max_guesses"])))
    if "delay_ms" in clean:
        clean["delay_ms"] = max(0, min(60000, int(clean["delay_ms"])))
    if "cooldown_ms" in clean:
        clean["cooldown_ms"] = max(0, min(120000, int(clean["cooldown_ms"])))
    if "min_confidence" in clean:
        clean["min_confidence"] = max(0, min(100, int(clean["min_confidence"])))
    if "max_repeat" in clean:
        clean["max_repeat"] = max(2, min(20, int(clean["max_repeat"])))
    if "new_game_command" in clean:
        clean["new_game_command"] = str(clean["new_game_command"]).strip()[:64] or "/new"
    for b in ("auto_start_on_new_game", "auto_stop_after_game", "send_new_game_command"):
        if b in clean:
            clean[b] = bool(clean[b])
    return clean


# --- singleton ------------------------------------------------------------- #
_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = TelegramService()
    return _service

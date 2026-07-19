"""Event-driven autoplay engine for WordSeek.

One engine instance per user. It is driven purely by incoming Telegram board
messages (no blind loop): every confirmed board update triggers a fresh solve
against the FULL board (so green/yellow/red feedback is always re-applied by the
existing solver), and at most one guess is sent per distinct board state.

The engine never touches Telegram directly — the owning service injects:
  send(text)      -> awaitable dict {ok, flood_wait, error}
  on_activity(kind, text)
  on_history(entry)
  now()           -> float seconds
  loop            -> the asyncio loop the engine runs on
"""
import asyncio
import time

from board import detect_board, detect_mode
from solver import GREEN, solve_board

# --- states -----------------------------------------------------------------
IDLE = "IDLE"
WAITING_FOR_BOARD = "WAITING_FOR_BOARD"
BOARD_DETECTED = "BOARD_DETECTED"
SOLVING = "SOLVING"
WAITING_TO_SEND = "WAITING_TO_SEND"
GUESS_SENT = "GUESS_SENT"
WAITING_FOR_UPDATE = "WAITING_FOR_UPDATE"
WON = "WON"
LOST = "LOST"
PAUSED = "PAUSED"
ERROR = "ERROR"

WIN_WORDS = ("you won", "you win", "solved", "correct", "congrat", "🎉", "🏆", "winner")
LOSS_WORDS = ("game over", "you lost", "you lose", "better luck", "the word was",
              "out of guesses", "no more guesses", "failed", "ran out")
NEW_GAME_WORDS = ("new game", "new word", "round started", "game started",
                  "let's play", "lets play", "guess the word", "starting a new")

_ACTIVE = (WAITING_FOR_BOARD, BOARD_DETECTED, SOLVING, WAITING_TO_SEND, GUESS_SENT, WAITING_FOR_UPDATE)
_TERMINAL = (WON, LOST, ERROR, IDLE)
_MAX_FLOOD_RETRIES = 3


class AutoplayEngine:
    def __init__(self, config, send, on_activity, on_history, loop, now=None):
        self.config = config                 # dict (the user's autoplay settings)
        self._send = send
        self._on_activity = on_activity
        self._on_history = on_history
        self.loop = loop
        self._now = now or time.time
        self._lock = asyncio.Lock()

        # controls
        self.running = False
        self.paused = False

        # game state
        self.state = IDLE
        self.game_id = 0
        self.mode = None
        self.guess_number = 0
        self.current_board_text = ""
        self.last_board_key = None
        self.last_message_id = None
        self.last_guess = None
        self.next_guess = None
        self.candidate_count = None
        self.confidence = None
        self.last_solve_ms = None
        self.repeat_count = 0
        self.game_start_ts = None
        self.last_update_ts = None
        self.result_word = None
        self.result_seconds = None

        self.sent_keys = set()
        self._pending_key = None
        self._flood_retries = 0
        self.timeline = []

    # ------------------------------------------------------------------ #
    def _active(self):
        return bool(self.config.get("enabled")) and self.running and not self.paused

    def _timeline(self, kind, text):
        entry = {"ts": self._now(), "type": kind, "text": text[:160]}
        self.timeline.append(entry)
        self.timeline = self.timeline[-40:]
        try:
            self._on_activity(kind, text)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # controls
    # ------------------------------------------------------------------ #
    def set_enabled(self, value):
        self.config["enabled"] = bool(value)
        if not value:
            self.running = False
            self.state = IDLE
        self._timeline("system", f"Autoplay {'enabled' if value else 'disabled'}")

    def start(self):
        if not self.config.get("enabled"):
            self.config["enabled"] = True
        self.running = True
        self.paused = False
        if self.state in _TERMINAL:
            self.state = WAITING_FOR_BOARD
        self._timeline("system", "Autoplay started — waiting for a board")

    def stop(self):
        self.running = False
        self.paused = False
        self._pending_key = None
        self.state = IDLE
        self._timeline("system", "Autoplay stopped")

    def emergency_stop(self):
        self.running = False
        self.paused = False
        self._pending_key = None
        self.state = IDLE
        self._timeline("error", "EMERGENCY STOP — all sending halted")

    def pause(self):
        self.paused = True
        if self.state in _ACTIVE:
            self.state = PAUSED
        self._timeline("system", "Autoplay paused")

    def resume(self):
        self.paused = False
        if self.state == PAUSED:
            self.state = WAITING_FOR_UPDATE
        self._timeline("system", "Autoplay resumed")

    def update_config(self, patch):
        for key in ("max_guesses", "delay_ms", "cooldown_ms", "min_confidence",
                    "auto_start_on_new_game", "auto_stop_after_game",
                    "send_new_game_command", "new_game_command", "max_repeat"):
            if key in patch:
                self.config[key] = patch[key]

    # ------------------------------------------------------------------ #
    # message classification
    # ------------------------------------------------------------------ #
    def _classify(self, text):
        d = detect_board(text)
        rows = d["rows"] if d else []
        board_key = d["key"] if d else None
        mode = (d["mode"] if d else None) or detect_mode(text)
        is_win = False
        win_word = None
        if rows:
            guess, tiles = rows[-1]
            if tiles and all(t == GREEN for t in tiles):
                is_win = True
                win_word = guess
        low = text.lower()
        if any(k in low for k in WIN_WORDS):
            is_win = True
        return {
            "rows": rows,
            "board_key": board_key,
            "mode": mode,
            "is_win": is_win,
            "win_word": win_word,
            "is_loss": any(k in low for k in LOSS_WORDS),
            "is_new_game": any(k in low for k in NEW_GAME_WORDS),
        }

    # ------------------------------------------------------------------ #
    # main entry — called for every message in the selected group
    # ------------------------------------------------------------------ #
    async def handle(self, text, message_id):
        if not self.config.get("enabled"):
            return
        async with self._lock:
            decision = self._decide(text, message_id)
        if decision:
            kind, payload = decision
            if kind == "guess":
                self._schedule_send(*payload)
            elif kind == "command":
                self._schedule_command(payload)

    def _decide(self, text, message_id):
        now = self._now()
        self.last_message_id = message_id
        cls = self._classify(text)
        rows, mode, board_key = cls["rows"], cls["mode"], cls["board_key"]
        rowcount = len(rows)

        if self.paused:
            self.state = PAUSED
            return None
        if not self.running:
            return None

        in_game = self.state in _ACTIVE
        # An IDENTICAL board is always a repeat, never a new game — this takes
        # precedence over the grid-reset heuristic below.
        same_board = board_key is not None and board_key == self.last_board_key

        new_game = False
        if cls["is_new_game"] and (self.config.get("auto_start_on_new_game") or not in_game):
            new_game = True
        elif not in_game and (board_key is not None or mode):
            if self.config.get("auto_start_on_new_game") or self.state == IDLE:
                new_game = True
        elif in_game and rowcount and rowcount < self.guess_number and not same_board:
            new_game = True  # grid reset (fewer rows, different board) -> a fresh game

        if new_game:
            self._new_game(mode)
            in_game = True

        if self.state in (WON, LOST, ERROR) and not new_game:
            return None

        self.last_update_ts = now

        # --- win / loss detection first ---
        if cls["is_win"]:
            by_us = bool(cls["win_word"] and self.last_guess and cls["win_word"].lower() == self.last_guess.lower())
            self._win(cls, by_us)
            return self._maybe_new_game_command()
        if cls["is_loss"] or (rowcount and rowcount >= int(self.config.get("max_guesses", 6)) and not cls["is_win"]):
            self._loss("Board reached the attempt limit" if rowcount else "Game ended")
            return self._maybe_new_game_command()

        # --- board de-dupe / confirmed-update gate ---
        if board_key is not None:
            if board_key == self.last_board_key:
                self.repeat_count += 1
                if self.repeat_count >= int(self.config.get("max_repeat", 3)):
                    self._enter_error("Same board repeated with no progress")
                return None
            self.repeat_count = 0
            self.last_board_key = board_key
            self.current_board_text = text
            if mode:
                self.mode = mode
            self.state = BOARD_DETECTED

        if not self.mode:
            self.state = WAITING_FOR_BOARD
            return None

        sent_key = board_key if board_key is not None else f"open:{self.game_id}"
        if sent_key in self.sent_keys or self._pending_key == sent_key:
            return None

        if self.guess_number >= int(self.config.get("max_guesses", 6)):
            self._loss("Reached maximum guesses")
            return self._maybe_new_game_command()

        # --- solve (recomputes from ALL feedback in the board) ---
        self.state = SOLVING
        t0 = time.perf_counter()
        try:
            result = solve_board(self.current_board_text or "", self.mode)
        except Exception:
            self._enter_error("Solver error")
            return None
        self.last_solve_ms = round((time.perf_counter() - t0) * 1000, 1)

        if not result["answer"]:
            self._enter_error("No candidate matches the board (inconsistent or already solved elsewhere)")
            return None

        self.candidate_count = result["count"]
        self.confidence = 100 if result["count"] <= 1 else max(1, round(100 / result["count"]))
        self.next_guess = result["answer"]

        if self.confidence < int(self.config.get("min_confidence", 0)):
            self.state = WAITING_TO_SEND
            self._timeline("system", f"Best guess {result['answer'].upper()} below confidence "
                                     f"({self.confidence}% < {self.config.get('min_confidence')}%) — holding")
            return None

        self.state = WAITING_TO_SEND
        self._pending_key = sent_key
        self._flood_retries = 0
        return ("guess", (result["answer"], sent_key, self.game_id))

    # ------------------------------------------------------------------ #
    # transitions
    # ------------------------------------------------------------------ #
    def _new_game(self, mode):
        self.game_id += 1
        self.mode = mode or None
        self.guess_number = 0
        self.current_board_text = ""
        self.last_board_key = None
        self.repeat_count = 0
        self.sent_keys.clear()
        self._pending_key = None
        self.last_guess = None
        self.next_guess = None
        self.result_word = None
        self.result_seconds = None
        self.candidate_count = None
        self.confidence = None
        self.game_start_ts = self._now()
        self.state = WAITING_FOR_BOARD
        self._timeline("system", f"New game #{self.game_id}" + (f" · {mode}-letter" if mode else ""))

    def _win(self, cls, by_us):
        self.state = WON
        self.result_seconds = round(self._now() - self.game_start_ts, 1) if self.game_start_ts else None
        self.result_word = (cls.get("win_word") or (self.last_guess or "")).upper()
        note = "" if by_us else " (solved by another player)"
        self._timeline("win", f"WON in {self.guess_number} guesses{note}")
        self._history({"result": "won", "word": self.result_word, "guesses": self.guess_number,
                       "seconds": self.result_seconds, "mode": self.mode, "by_us": by_us})
        if self.config.get("auto_stop_after_game"):
            self.running = False

    def _loss(self, reason):
        self.state = LOST
        self.result_seconds = round(self._now() - self.game_start_ts, 1) if self.game_start_ts else None
        self._timeline("loss", f"Game lost — {reason}")
        self._history({"result": "lost", "word": (self.last_guess or "").upper(), "guesses": self.guess_number,
                       "seconds": self.result_seconds, "mode": self.mode, "by_us": False})
        if self.config.get("auto_stop_after_game"):
            self.running = False

    def _enter_error(self, reason):
        self.state = ERROR
        self.running = False
        self._pending_key = None
        self._timeline("error", reason)

    def _history(self, entry):
        try:
            self._on_history(entry)
        except Exception:
            pass

    def _maybe_new_game_command(self):
        if self.config.get("send_new_game_command") and self.running and not self.paused:
            cmd = (self.config.get("new_game_command") or "/new").strip()
            if cmd:
                return ("command", cmd)
        return None

    # ------------------------------------------------------------------ #
    # sending (scheduled tasks; re-validated under lock before each send)
    # ------------------------------------------------------------------ #
    def _schedule_send(self, word, board_key, game_id):
        self.loop.create_task(self._delayed_send(word, board_key, game_id))

    def _schedule_command(self, text):
        self.loop.create_task(self._delayed_command(text))

    async def _delayed_send(self, word, board_key, game_id):
        delay = max(0, int(self.config.get("delay_ms", 1500))) / 1000.0
        cooldown_wait = max(0.0, getattr(self, "_cooldown_until", 0.0) - self._now())
        wait = max(delay, cooldown_wait)
        if wait > 0:
            await asyncio.sleep(wait)

        async with self._lock:
            if game_id != self.game_id or self._pending_key != board_key:
                return
            if not self._active() or self.state != WAITING_TO_SEND or board_key in self.sent_keys:
                self._pending_key = None
                return
            self.state = GUESS_SENT

        result = await self._send(word)

        async with self._lock:
            if result.get("flood_wait"):
                fw = int(result["flood_wait"])
                self._cooldown_until = self._now() + fw
                self._timeline("system", f"FloodWait {fw}s — will retry this guess")
                self._flood_retries += 1
                if self._flood_retries <= _MAX_FLOOD_RETRIES and game_id == self.game_id:
                    self.state = WAITING_TO_SEND
                    self._schedule_send(word, board_key, game_id)
                else:
                    self._enter_error("Too many FloodWaits — stopping")
                return
            if result.get("ok"):
                self.sent_keys.add(board_key)
                self.guess_number += 1
                self.last_guess = word
                self._cooldown_until = self._now() + int(self.config.get("cooldown_ms", 4000)) / 1000.0
                self.state = WAITING_FOR_UPDATE
                self._pending_key = None
                self._flood_retries = 0
                self._timeline("guess", f"Sent guess #{self.guess_number}: {word.upper()}")
            else:
                self._pending_key = None
                self._enter_error(result.get("error") or "Send failed / rejected")

    async def _delayed_command(self, text):
        await asyncio.sleep(1.0)
        if not (self.running and not self.paused):
            return
        res = await self._send(text)
        if res.get("ok"):
            self._timeline("system", f"Sent new-game command: {text}")

    # ------------------------------------------------------------------ #
    def status(self):
        since = None
        if self.last_update_ts:
            since = round(self._now() - self.last_update_ts, 1)
        return {
            "state": PAUSED if self.paused and self.running else self.state,
            "enabled": bool(self.config.get("enabled")),
            "running": self.running,
            "paused": self.paused,
            "game_id": self.game_id,
            "mode": self.mode,
            "guess_number": self.guess_number,
            "max_guesses": int(self.config.get("max_guesses", 6)),
            "last_guess": (self.last_guess or "").upper() or None,
            "next_guess": (self.next_guess or "").upper() or None,
            "candidate_count": self.candidate_count,
            "confidence": self.confidence,
            "solve_ms": self.last_solve_ms,
            "seconds_since_update": since,
            "result_word": self.result_word,
            "result_seconds": self.result_seconds,
            "timeline": self.timeline[-14:][::-1],
            "config": {
                "enabled": bool(self.config.get("enabled")),
                "max_guesses": int(self.config.get("max_guesses", 6)),
                "delay_ms": int(self.config.get("delay_ms", 1500)),
                "cooldown_ms": int(self.config.get("cooldown_ms", 4000)),
                "min_confidence": int(self.config.get("min_confidence", 0)),
                "auto_start_on_new_game": bool(self.config.get("auto_start_on_new_game")),
                "auto_stop_after_game": bool(self.config.get("auto_stop_after_game")),
                "send_new_game_command": bool(self.config.get("send_new_game_command")),
                "new_game_command": self.config.get("new_game_command") or "/new",
                "max_repeat": int(self.config.get("max_repeat", 3)),
            },
        }

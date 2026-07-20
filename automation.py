"""Automation: custom messages + user-defined rules, fired on game events.

Events (detected from Telegram messages, independent of autoplay):
  - "new_game" : the bot announced a new round (e.g. after /new)
  - "won"      : a win was announced
  - "lost"     : a loss was announced

On each event this runs, in order:
  1) the user's Custom Messages (random or sequential, delay, max), and
  2) each enabled Automation rule whose trigger matches, executing its actions.

Everything is bounded (max actions, capped waits) and serialized per user so a
burst of events can't spawn overlapping senders.
"""
import asyncio
import random
import time

WIN_WORDS = ("you won", "you win", "congrat", "correct word", "🎉", "🏆", "winner", "guessed it correctly")
LOSS_WORDS = ("game over", "you lost", "you lose", "better luck", "the word was",
              "out of guesses", "no more guesses", "failed", "ran out")
NEW_GAME_WORDS = ("game started", "new game", "new word", "round started", "starting a new")

MAX_WAIT_MS = 20000
_ACTION_CAP = 25


def event_of(text: str):
    """Classify a message into one automation event, or None."""
    low = (text or "").lower()
    if any(k in low for k in WIN_WORDS):
        return "won"
    if any(k in low for k in LOSS_WORDS):
        return "lost"
    if any(k in low for k in NEW_GAME_WORDS):
        return "new_game"
    return None


class Automation:
    def __init__(self, send, get_messages, get_settings, get_rules, engine, log, loop, now=None, get_weighted=None):
        self.send = send                 # async (text) -> dict {ok,...}
        self.get_messages = get_messages  # () -> [str]
        self.get_weighted = get_weighted or (lambda: [(t, 1) for t in get_messages()])  # () -> [(text, weight)]
        self.get_settings = get_settings  # () -> dict (message settings)
        self.get_rules = get_rules        # () -> [rule dict]
        self.engine = engine
        self.log = log                    # (kind, text)
        self.loop = loop
        self.now = now or time.time
        self._seq = 0
        self._busy = False
        self._rand = random.Random()

    def fire(self, event):
        """Schedule a run (called from the message handler)."""
        self.loop.create_task(self._run(event))

    async def _run(self, event):
        if self._busy:
            return
        self._busy = True
        try:
            await self._custom_messages(event)
            await self._rules(event)
        except Exception:
            pass
        finally:
            self._busy = False

    # ------------------------------------------------------------------ #
    async def _custom_messages(self, event):
        s = self.get_settings() or {}
        if not s.get("enabled"):
            return
        if event == "new_game" and not s.get("on_new_game", True):
            return
        if event == "won" and not (s.get("after_every_game") or s.get("only_wins")):
            return
        if event == "lost" and not (s.get("after_every_game") or s.get("only_losses")):
            return

        msgs = self.get_messages() or []
        if not msgs:
            return
        count = max(1, min(int(s.get("max_messages", 1)), len(msgs)))
        mode = s.get("mode")
        if mode == "sequential":
            chosen = [msgs[(self._seq + i) % len(msgs)] for i in range(count)]
            self._seq = (self._seq + count) % len(msgs)
        elif mode == "weighted":
            chosen = self._weighted_pick(self.get_weighted() or [], count)
        else:
            chosen = self._rand.sample(msgs, count)

        delay = max(0, min(MAX_WAIT_MS, int(s.get("delay_ms", 0)))) / 1000.0
        for m in chosen:
            if delay:
                await asyncio.sleep(delay)
            res = await self.send(m)
            if res.get("ok"):
                self.log("automation", f"Sent message: {m}")
            elif res.get("flood_wait"):
                await asyncio.sleep(min(int(res["flood_wait"]), 30))

    def _weighted_pick(self, pairs, count):
        """Weighted random selection without replacement."""
        pool = [(t, max(1, int(w))) for t, w in pairs if t]
        out = []
        for _ in range(min(count, len(pool))):
            total = sum(w for _, w in pool)
            r = self._rand.uniform(0, total)
            acc = 0
            for i, (t, w) in enumerate(pool):
                acc += w
                if r <= acc:
                    out.append(t)
                    pool.pop(i)
                    break
        return out

    async def _rules(self, event):
        for rule in self.get_rules() or []:
            if not rule.get("enabled") or rule.get("trigger") != event:
                continue
            await self._run_actions(rule)

    async def _run_actions(self, rule):
        self.log("automation", f"Rule fired: {rule.get('name', 'Rule')}")
        for act in (rule.get("actions") or [])[:_ACTION_CAP]:
            t = act.get("type")
            if t == "wait":
                await asyncio.sleep(max(0, min(MAX_WAIT_MS, int(act.get("ms", 0)))) / 1000.0)
            elif t == "send_text":
                txt = str(act.get("text", "")).strip()
                if txt:
                    await self.send(txt)
                    self.log("automation", f"Sent: {txt}")
            elif t == "send_message":
                msgs = self.get_messages() or []
                if msgs:
                    mode = act.get("mode")
                    if mode == "sequential":
                        m = msgs[self._seq % len(msgs)]
                        self._seq += 1
                    elif mode == "weighted":
                        picks = self._weighted_pick(self.get_weighted() or [], 1)
                        m = picks[0] if picks else self._rand.choice(msgs)
                    else:
                        m = self._rand.choice(msgs)
                    await self.send(m)
                    self.log("automation", f"Sent message: {m}")
            elif t == "start_autoplay":
                self.engine.start()
                self.log("automation", "Autoplay started (by rule)")
            elif t == "stop_autoplay":
                self.engine.stop()
                self.log("automation", "Autoplay stopped (by rule)")
            elif t == "new_game_command":
                cmd = (self.engine.config.get("new_game_command") or "/new").strip()
                if cmd:
                    await self.send(cmd)
                    self.log("automation", f"Sent new-game command: {cmd}")

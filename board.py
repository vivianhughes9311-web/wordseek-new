"""Board detection helpers for the Telegram monitor.

Reuses the existing solver parser so the solving algorithm is unchanged.
A "board" is any message that contains WordSeek tile emojis and at least one
row whose letter-count and tile-count match a 4- or 5-letter mode.
"""
import re

from solver import parse_board

EMOJIS = ("🟩", "🟨", "🟥")
_EMOJI_RE = re.compile(r"[🟩🟨🟥]")


def detect_mode(text: str):
    """Return 4, 5 or None from an arbitrary message."""
    if re.search(r"4[\s-]?letter", text, re.I):
        return 4
    if re.search(r"5[\s-]?letter", text, re.I):
        return 5
    for line in text.splitlines():
        if any(e in line for e in EMOJIS):
            n = len(_EMOJI_RE.findall(line))
            if n in (4, 5):
                return n
    return None


def detect_board(text: str):
    """Return {mode, rows, key} for a valid board, else None.

    `key` is a stable fingerprint used to avoid processing/sending the same
    board twice (independent of surrounding whitespace or extra chatter).
    """
    if not text or not any(e in text for e in EMOJIS):
        return None
    mode = detect_mode(text)
    if mode not in (4, 5):
        return None
    rows = parse_board(text, mode)
    if not rows:
        return None
    key = f"{mode}:" + "|".join(g + "".join(e) for g, e in rows)
    return {"mode": mode, "rows": rows, "key": key}


def clean_board_text(text: str, mode: int) -> str:
    """Return only the recognised board rows, for tidy dashboard display."""
    rows = parse_board(text, mode)
    return "\n".join(f"{g.upper()} {''.join(e)}" for g, e in rows)

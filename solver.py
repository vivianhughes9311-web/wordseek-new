import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path

from wordfreq import zipf_frequency

GREEN = "🟩"
YELLOW = "🟨"
RED = "🟥"
BASE_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=2)
def load_words(length: int) -> tuple[str, ...]:
    if length not in (4, 5):
        raise ValueError("Mode must be 4 or 5")

    word_file = BASE_DIR / f"words{length}.txt"
    with word_file.open(encoding="utf-8") as file:
        return tuple(
            word.strip().lower()
            for word in file
            if len(word.strip()) == length and word.strip().isalpha()
        )


def parse_board(board: str, length: int) -> list[tuple[str, list[str]]]:
    rows = []
    for line in board.splitlines():
        if not any(symbol in line for symbol in (GREEN, YELLOW, RED)):
            continue

        emojis = re.findall(r"[🟩🟨🟥]", line)
        normalized = unicodedata.normalize("NFKD", line)
        guess = "".join(character for character in normalized if character.isalpha()).lower()

        if len(emojis) == length and len(guess) == length:
            rows.append((guess, emojis))

    return rows


def score(secret: str, guess: str) -> list[str]:
    result = [RED] * len(secret)
    remaining_secret = list(secret)
    remaining_guess = list(guess)

    for index in range(len(secret)):
        if remaining_guess[index] == remaining_secret[index]:
            result[index] = GREEN
            remaining_secret[index] = ""
            remaining_guess[index] = ""

    for index, letter in enumerate(remaining_guess):
        if letter and letter in remaining_secret:
            result[index] = YELLOW
            remaining_secret[remaining_secret.index(letter)] = ""

    return result


def solve_board(board: str, length: int, limit: int = 10) -> dict:
    words = load_words(length)
    rows = parse_board(board, length)

    candidates = [
        word
        for word in words
        if all(score(word, guess) == expected for guess, expected in rows)
    ]

    if not candidates:
        return {"answer": None, "guesses": [], "count": 0, "message": "No candidates found."}

    letter_frequency = Counter()
    for candidate in candidates:
        letter_frequency.update(set(candidate))

    def rank(word: str) -> tuple[float, int, str]:
        commonness = zipf_frequency(word, "en")
        coverage = sum(letter_frequency[letter] for letter in set(word))
        return (-commonness, -coverage, word)

    candidates.sort(key=rank)
    guesses = candidates[:limit]
    return {
        "answer": guesses[0],
        "guesses": guesses,
        "count": len(candidates),
        "message": guesses[0],
    }

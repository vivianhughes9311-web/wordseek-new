"""Authenticated encryption for at-rest Telegram session strings.

Uses AES-256-CBC + HMAC-SHA256 (encrypt-then-MAC) via pure-Python `pyaes`
(already a Telethon dependency), so there is no extra native dependency.

The key is derived from the SECRET_KEY environment variable. If SECRET_KEY
changes, previously stored sessions can no longer be decrypted (users simply
re-login) — so set a strong, STABLE SECRET_KEY in production.
"""
import base64
import hashlib
import hmac
import os

import pyaes

_SALT = b"wordseek-session-v1"


def _keys():
    secret = (os.environ.get("SECRET_KEY", "") or "wordseek-dev-insecure-key").encode()
    material = hashlib.pbkdf2_hmac("sha256", secret, _SALT, 100_000, dklen=64)
    return material[:32], material[32:]  # (aes_key, mac_key)


def _pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16 or len(data) < pad:
        raise ValueError("bad padding")
    return data[:-pad]


def encrypt(plaintext: str) -> str:
    aes_key, mac_key = _keys()
    iv = os.urandom(16)
    enc = pyaes.Encrypter(pyaes.AESModeOfOperationCBC(aes_key, iv))
    ct = enc.feed(_pad(plaintext.encode("utf-8"))) + enc.feed()
    mac = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(iv + ct + mac).decode("ascii")


def decrypt(token: str):
    try:
        aes_key, mac_key = _keys()
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        iv, ct, mac = raw[:16], raw[16:-32], raw[-32:]
        expected = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return None
        dec = pyaes.Decrypter(pyaes.AESModeOfOperationCBC(aes_key, iv))
        pt = dec.feed(ct) + dec.feed()
        return _unpad(pt).decode("utf-8")
    except Exception:
        return None

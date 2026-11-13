"""Pairing for PenLink: enter a PIN once on the iPad, after that it is remembered.

A random secret is stored next to the program. The iPad keeps a token derived from it,
so pairing survives restarts. Deleting penlink_secret.txt forgets every paired iPad.
"""
import hashlib
import hmac
import secrets
from pathlib import Path

SECRET_FILE = Path(__file__).resolve().parent / "penlink_secret.txt"


def load_secret(path=SECRET_FILE):
    try:
        text = Path(path).read_text().strip()
        if len(text) >= 32:
            return text
    except OSError:
        pass
    text = secrets.token_hex(32)
    try:
        Path(path).write_text(text)
    except OSError:
        pass  # still works for this run, but pairing will not survive a restart
    return text


def device_token(secret):
    return hmac.new(secret.encode(), b"penlink device token", hashlib.sha256).hexdigest()


def new_pin():
    return "%06d" % secrets.randbelow(10 ** 6)


def same(a, b):
    return hmac.compare_digest(str(a).encode(), str(b).encode())

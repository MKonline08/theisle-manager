"""Encrypted-at-rest helper for game and RCON passwords."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _cipher() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings().jwt_secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Supports an upgrade from databases that stored this value plainly.
        return value

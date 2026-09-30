"""Authenticated encryption helpers for persisted Kakao tokens.

SHUTTLE_TOKEN_ENCRYPTION_KEY must be retained as a long-lived deployment
secret. Replacing it makes previously encrypted tokens unreadable.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


TOKEN_KEY_ENV = "SHUTTLE_TOKEN_ENCRYPTION_KEY"


class TokenCryptoError(RuntimeError):
    """Token encryption configuration or decryption failed."""


def validate_encryption_key(key: str | bytes | None = None) -> bytes:
    """Return a valid Fernet key without logging key material."""
    value = key if key is not None else os.getenv(TOKEN_KEY_ENV)
    if isinstance(value, str):
        try:
            value = value.strip().encode("ascii", errors="strict")
        except UnicodeEncodeError:
            raise TokenCryptoError(f"{TOKEN_KEY_ENV} is invalid.") from None
    if not value:
        raise TokenCryptoError(f"{TOKEN_KEY_ENV} is not configured.")
    try:
        Fernet(value)
    except (ValueError, TypeError):
        raise TokenCryptoError(f"{TOKEN_KEY_ENV} is invalid.") from None
    return value


def encrypt_token(token: str | None, key: str | bytes | None = None) -> bytes | None:
    """Encrypt a non-empty token; empty and NULL tokens are stored as NULL."""
    if token is None or token == "":
        return None
    if not isinstance(token, str):
        raise TokenCryptoError("Token must be text or NULL.")
    return Fernet(validate_encryption_key(key)).encrypt(token.encode("utf-8"))


def decrypt_token(ciphertext: bytes | None, key: str | bytes | None = None) -> str | None:
    """Decrypt a token, returning NULL for an absent ciphertext."""
    if ciphertext is None:
        return None
    try:
        plaintext = Fernet(validate_encryption_key(key)).decrypt(bytes(ciphertext))
        return plaintext.decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, TypeError, ValueError):
        raise TokenCryptoError("Token decryption failed.") from None

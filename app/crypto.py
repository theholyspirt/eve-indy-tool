"""Encryption at rest for EVE OAuth tokens.

WHY THIS EXISTS
---------------
With hundreds of members, the characters table holds hundreds of refresh
tokens — and the app's ESI scopes include write access (send mail, edit
contacts/fittings). A leaked database file therefore means an attacker can ACT
AS corp members, not just read their data. Encrypting the tokens makes a
leaked DB file useless without the separate key.

Fernet (AES-128-CBC + HMAC, via the cryptography package) — symmetric,
authenticated, and tamper-evident.

KEY MANAGEMENT
--------------
TOKEN_ENCRYPTION_KEY in the environment (.env), generated with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

- The key must NEVER live in the same place as the database backup.
- Losing the key = every member has to re-link their characters (annoying but
  recoverable). Leaking key+DB together = full compromise, same as before.

If TOKEN_ENCRYPTION_KEY is unset, tokens are stored as-is (dev mode). The app
warns loudly at startup in that case so it can't go unnoticed in production.
"""
import os

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc$v1$"  # marks a value as encrypted, enables gradual migration

_fernet = None
_warned = False


def _get_fernet():
    global _fernet, _warned
    if _fernet is not None:
        return _fernet
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        if not _warned:
            print("[crypto] WARNING: TOKEN_ENCRYPTION_KEY is not set — EVE "
                  "tokens are stored UNENCRYPTED. Fine for local dev, not for "
                  "a deployment.")
            _warned = True
        return None
    _fernet = Fernet(key.encode())
    return _fernet


def encrypt_token(value):
    """Encrypt a token for storage. Pass-through when no key is configured."""
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        return value
    return _PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_token(stored):
    """Decrypt a stored token.

    Values without the prefix are returned as-is — that's what lets a
    pre-encryption database keep working, and _migrate_encrypt_tokens() in
    __init__.py upgrade rows in place.

    A prefixed value that fails to decrypt raises: it means the key changed or
    the ciphertext was tampered with, and silently returning garbage would
    just produce confusing 401s at the ESI boundary instead of a clear error.
    """
    if stored is None:
        return None
    if not stored.startswith(_PREFIX):
        return stored
    f = _get_fernet()
    if f is None:
        raise RuntimeError(
            "found an encrypted token but TOKEN_ENCRYPTION_KEY is not set — "
            "restore the key this database was encrypted with"
        )
    try:
        return f.decrypt(stored[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError(
            "token decryption failed — TOKEN_ENCRYPTION_KEY does not match "
            "the key this value was encrypted with"
        )


def is_encrypted(stored):
    return bool(stored) and stored.startswith(_PREFIX)

"""User-management helpers: password hashing, username normalization.

Pure utility module — no DB access. Imported by ``eXam.db`` (bootstrap admin)
and by ``web/user_auth.py`` (login verify) / ``web/user_validation.py`` (signup).

Password hash format: ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``.
Storing the algo + iteration count alongside the hash keeps the format
forward-migratable (bump iterations later without breaking existing rows).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata
from typing import Final

_PBKDF2_ITER_DEFAULT: Final[int] = 600_000  # OWASP 2023 floor for PBKDF2-SHA256
_HASH_PREFIX: Final[str] = "pbkdf2_sha256"


def normalize_username_key(name: str) -> str:
    """Fold a display name to its uniqueness key.

    NFC-normalize → casefold → strip → collapse internal whitespace. So
    ``"Maya Patel"``, ``"maya patel"``, and ``"MAYA  PATEL"`` all map to the
    same key. Used for the ``users.username_key`` UNIQUE constraint and for
    login lookup.
    """
    s = unicodedata.normalize("NFC", name).casefold().strip()
    return re.sub(r"\s+", " ", s)


def hash_password(password: str, *, iterations: int = _PBKDF2_ITER_DEFAULT) -> str:
    """Hash *password* with PBKDF2-SHA256 + random 16-byte salt."""
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(h).decode("ascii")
    return f"{_HASH_PREFIX}${iterations}${salt_b64}${hash_b64}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time compare *password* against a stored hash. Tolerates a
    malformed stored value (returns False) — never raises."""
    try:
        algo, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
    except ValueError:
        return False
    if algo != _HASH_PREFIX:
        return False
    try:
        iterations = int(iter_s)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(hash_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False
    if iterations < 1 or len(salt) < 8 or len(expected) < 16:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)

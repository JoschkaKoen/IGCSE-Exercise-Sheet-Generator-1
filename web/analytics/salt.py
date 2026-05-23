# -*- coding: utf-8 -*-
"""Persistent per-install random salt for hashing IPs and student IDs.

The salt is generated on first call (32 random bytes via :mod:`secrets`) and
written to ``ANALYTICS_SALT_PATH`` (default ``<repo>/output/analytics/.salt``,
which inside the Docker container resolves to ``/app/output/analytics/.salt``
under the existing ``output_data`` volume mount). Mode 0o600, atomic
``os.O_EXCL`` first-write to avoid TOCTOU.

Why a salt: storing ``sha256(ip)`` directly would let an attacker who acquires
the DB rebuild an IP→identity map by hashing IPs they already know. A secret
salt makes that lookup impossible without also stealing the salt file.

Failure mode: if the filesystem refuses writes (e.g. read-only mount), the
module falls back to a **process-local** salt — analytics still works but
IP correlation is lost across process restarts. Logged once at WARNING.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
from pathlib import Path

# Repo root is …/eXercise/; this file is at …/eXercise/web/analytics/salt.py
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_PATH = _REPO_ROOT / "output" / "analytics" / ".salt"

_cached_salt: bytes | None = None
_lock = threading.Lock()
_log = logging.getLogger(__name__)


def _salt_path() -> Path:
    env = os.environ.get("ANALYTICS_SALT_PATH")
    return Path(env) if env else _DEFAULT_PATH


def _persist_or_fallback(path: Path) -> bytes:
    """Try to read/create the salt at *path*. Return process-local random on filesystem failure."""
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("analytics salt: cannot read %s (%s); using process-local salt", path, exc)
        return secrets.token_bytes(32)

    new_salt = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(fd, new_salt)
        finally:
            os.close(fd)
        return new_salt
    except FileExistsError:
        # Lost the race; another worker wrote it. Read whatever's there.
        try:
            data = path.read_bytes()
            return data if len(data) == 32 else new_salt
        except OSError:
            return new_salt
    except OSError as exc:
        _log.warning(
            "analytics salt: cannot write %s (%s); using process-local salt",
            path, exc,
        )
        return new_salt


def get_salt() -> bytes:
    """Return the 32-byte salt, generating + persisting it on first call."""
    global _cached_salt
    if _cached_salt is not None:
        return _cached_salt
    with _lock:
        if _cached_salt is not None:
            return _cached_salt
        _cached_salt = _persist_or_fallback(_salt_path())
        return _cached_salt


def hash_ip(ip: str) -> str:
    """Return a 16-hex-char salted hash of *ip*. Empty/error → empty string."""
    if not ip:
        return ""
    try:
        return hashlib.sha256(get_salt() + ip.encode("utf-8")).hexdigest()[:16]
    except Exception:
        _log.exception("hash_ip failed")
        return ""


def hash_id(value: str | int) -> str:
    """Return a 12-hex-char salted hash for a stable identifier (e.g. student row id)."""
    try:
        s = str(value).encode("utf-8") if value is not None else b""
        if not s:
            return ""
        return hashlib.sha256(get_salt() + s).hexdigest()[:12]
    except Exception:
        _log.exception("hash_id failed")
        return ""

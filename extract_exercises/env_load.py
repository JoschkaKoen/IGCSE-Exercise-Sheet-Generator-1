# -*- coding: utf-8 -*-
"""Load environment: committed defaults, then local secrets."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from .config import PROJECT_ROOT

_DEFAULT_ENV = PROJECT_ROOT / "default.env"
_LOCAL_ENV = PROJECT_ROOT / ".env"


def load_project_env() -> None:
    """Load ``default.env`` (committed), then ``.env`` at project root, then cwd ``.env``.

    Order:
    1. ``default.env`` — non-secret defaults; does not override variables already
       set in the process environment (e.g. Docker ``-e`` / CI).
    2. Project ``.env`` — secrets and overrides; always applied for keys present.
    3. Current working directory ``.env`` — last wins for overlapping keys.

    ``.env`` is gitignored; ``default.env`` is committed.
    """
    if _DEFAULT_ENV.is_file():
        load_dotenv(_DEFAULT_ENV, override=False)
    if _LOCAL_ENV.is_file():
        load_dotenv(_LOCAL_ENV, override=True)
    cwd_env = Path.cwd() / ".env"
    if cwd_env != _LOCAL_ENV and cwd_env.is_file():
        load_dotenv(cwd_env, override=True)

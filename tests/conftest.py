# -*- coding: utf-8 -*-
"""pytest configuration: stub heavy C-extensions and prevent eXercise.__init__
from triggering pipeline → fitz imports during test collection.

This conftest runs before any test module is imported, so stubs are in place
before the package's __init__.py would normally execute.
"""

import importlib.util
import pathlib
import sys
import types

# ---------------------------------------------------------------------------
# Stub C-extensions / optional packages not installed in the dev environment
# ---------------------------------------------------------------------------

for _mod in ("fitz", "dotenv"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

# Provide the load_dotenv symbol that env_load.py imports from dotenv
_dotenv_stub = sys.modules["dotenv"]
if not hasattr(_dotenv_stub, "load_dotenv"):
    _dotenv_stub.load_dotenv = lambda *a, **kw: None  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Pre-register a minimal eXercise package so __init__.py is NOT executed.
# Tests import sub-modules directly; the package just needs to be discoverable.
# ---------------------------------------------------------------------------

_pkg_dir = str(pathlib.Path(__file__).parent.parent / "eXercise")

if "eXercise" not in sys.modules:
    _pkg = types.ModuleType("eXercise")
    _pkg.__path__ = [_pkg_dir]       # type: ignore[attr-defined]
    _pkg.__package__ = "eXercise"
    _pkg.__spec__ = importlib.util.spec_from_file_location(
        "eXercise",
        str(pathlib.Path(_pkg_dir) / "__init__.py"),
        submodule_search_locations=[_pkg_dir],
    )
    sys.modules["eXercise"] = _pkg

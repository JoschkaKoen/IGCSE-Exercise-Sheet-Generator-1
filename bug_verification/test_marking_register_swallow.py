"""Verify Bug #10: Marking register v1 swallow-all exceptions.

File: xscore/steps/geometry.py
Issue: build_marking_register_v1 caught all exceptions, printed a warning,
and returned normally. run_step recorded step status as "ok" even though
the register was not built.

The fix removes the broad except so failures propagate up.
"""

import sys

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

# Source-only check — geometry.py imports fitz transitively, so we read
# the file directly rather than importing the module.
src = open(
    "/Users/joschka/Desktop/Programming/eXercise/xscore/steps/geometry.py",
    encoding="utf-8",
).read()

# Find the function body.
fn_marker = "def build_marking_register_v1"
start = src.find(fn_marker)
assert start >= 0, "build_marking_register_v1 not found"
end = src.find("\ndef ", start + 1)
fn_body = src[start:end] if end >= 0 else src[start:]

assert "except Exception" not in fn_body, (
    "build_marking_register_v1 still has a broad except clause:\n" + fn_body
)
print("FIX VERIFIED: no broad except in build_marking_register_v1.")
sys.exit(0)

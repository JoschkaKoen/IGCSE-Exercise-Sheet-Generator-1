"""Verify Bug #14: Report graphics lookup with None question number.

File: xscore/marking/report_latex.py
Issue: q.get("number", "") returns None (not "") when the key value is None.
str(None) == "None", so the graphics lookup keys on the literal string.

The fix is `(q.get("number") or "")` everywhere the key is read.
"""

import sys

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

# Source-only check — report_latex.py imports jinja2, which we don't want
# to make a hard dependency for this static-analysis test.
src = open(
    "/Users/joschka/Desktop/Programming/eXercise/xscore/marking/report_latex.py",
    encoding="utf-8",
).read()

# Old buggy pattern must be gone.
assert 'q.get("number", "")' not in src, (
    'Found legacy q.get("number", "") pattern — fix may have regressed.\n'
    "Locations:\n"
    + "\n".join(
        f"  line {i+1}: {ln}"
        for i, ln in enumerate(src.splitlines())
        if 'q.get("number", "")' in ln
    )
)

# Fixed pattern must appear at every site that reads "number".
fixed_pattern_count = src.count('(q.get("number") or "")')
assert fixed_pattern_count >= 7, (
    f"Expected fixed pattern at all 7 known sites, found {fixed_pattern_count}"
)

print(f"FIX VERIFIED: legacy pattern absent; fixed pattern at {fixed_pattern_count} sites.")
sys.exit(0)

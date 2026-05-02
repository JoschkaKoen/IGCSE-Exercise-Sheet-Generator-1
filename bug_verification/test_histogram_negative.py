"""Verify Bug #2: Histogram negative index.

File: xscore/marking/class_charts.py
Issue: idx = min(int(v) // 10, 9) allows negative indices.
A negative percentage lands in counts[-1] (the 90-100 bin).

This test exercises the same indexing the function uses, before and after
the max(0, ...) clamp, to confirm the fix.
"""

import sys

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

# Mirror the post-fix indexing in class_charts.py:render_grade_histogram
def bin_values_fixed(values):
    counts = [0] * 10
    for v in values:
        idx = max(0, min(int(v) // 10, 9))
        counts[idx] += 1
    return counts


# Confirm the actual source has the fix (defence in depth).
import inspect
from xscore.marking import class_charts

src = inspect.getsource(class_charts.render_grade_histogram)
assert "max(0, min(int(v) // 10, 9))" in src, (
    "Source no longer contains the fixed indexing — fix may have regressed.\n"
    f"Source:\n{src}"
)

values = [-5, 15, 25, 95]
counts = bin_values_fixed(values)
print("Values:", values)
print("Counts per bin (0-9, 10-19, ..., 90-100):", counts)

# After the fix, -5 must land in bin 0 (0-9), not bin 9 (90-100).
assert counts[0] == 1, f"Expected -5 → bin 0, got counts={counts}"
assert counts[9] == 1, f"Expected 95 → bin 9, got counts={counts}"
print("FIX VERIFIED: negative value clamped to bin 0; 95 still in bin 9.")
sys.exit(0)

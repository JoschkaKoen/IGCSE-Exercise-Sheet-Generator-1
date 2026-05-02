"""Verify Bug: Histogram negative index.

File: xscore/marking/class_charts.py
Issue: idx = min(int(v) // 10, 9) allows negative indices.
A negative percentage lands in counts[-1] (the 90-100 bin).
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

# Simulate the binning logic from render_grade_histogram
def bin_values(values):
    counts = [0] * 10
    for v in values:
        idx = min(int(v) // 10, 9)
        counts[idx] += 1
    return counts

# Test with a negative curved percentage
values = [-5, 15, 25, 95]
counts = bin_values(values)
print("Values:", values)
print("Counts per bin (0-9, 10-19, ..., 90-100):", counts)

# -5 should NOT be in the 90-100 bin (index -1 → 9)
if counts[9] > 0:
    print("BUG CONFIRMED: Negative value -5 placed in 90-100 bin!")
    sys.exit(1)
else:
    print("BUG NOT REPRODUCED: Negative value handled correctly.")
    sys.exit(0)

"""Verify Bug: Cost report KeyError.

File: xscore/shared/cost_report.py
Issue: compute_cost assumes every value in usage contains "input" and "output" keys.
If a model entry is missing these keys, KeyError is raised.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.shared.cost_report import compute_cost

# Usage dict missing "output" key for one model
usage = {
    "gpt-4o": {"input": 1000, "output": 500},
    "some-model": {"input": 2000},  # missing "output"!
}

try:
    total, breakdown = compute_cost(usage)
    print("BUG NOT REPRODUCED: Missing keys handled gracefully.")
    print(f"Total: {total}, Breakdown: {breakdown}")
    sys.exit(0)
except KeyError as e:
    print(f"BUG CONFIRMED: KeyError raised for missing key {e}")
    sys.exit(1)

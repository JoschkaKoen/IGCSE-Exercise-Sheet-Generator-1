"""Verify Bug: Ground truth header mis-detection.

File: xscore/shared/load_ground_truth.py
Issue: If header row contains numeric question labels (e.g., "Name 1 2 3"),
_is_data_row returns True, so the header is treated as student data.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.shared.load_ground_truth import load_ground_truth

# Create a ground truth file where header has numeric labels
content = "Name\t1\t2\t3\nAlice\tA\tB\tC\nBob\tD\tA\tB\n"

with tempfile.TemporaryDirectory() as tmpdir:
    path = Path(tmpdir) / "ground_truth.txt"
    path.write_text(content, encoding="utf-8")
    result = load_ground_truth(Path(tmpdir))

    print("Result:", result)

    # The header row "Name 1 2 3" should NOT appear as a student named "Name"
    if "Name" in result:
        print("BUG CONFIRMED: Header row 'Name 1 2 3' was parsed as student data!")
        print(f"  Student 'Name' has answers: {result['Name']}")
        print(f"  Alice's answers: {result.get('Alice')}")
        sys.exit(1)
    else:
        print("BUG NOT REPRODUCED: Header correctly skipped.")
        sys.exit(0)

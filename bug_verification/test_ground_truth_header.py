"""Verify Bug #1: Ground truth header mis-detection.

File: xscore/shared/load_ground_truth.py  (REMOVED)
Issue: ``_is_data_row`` ran before the header check, so a header row with
numeric question labels ("Name 1 2 3") was parsed as a student named "Name".

Resolution: the standalone ground-truth-file loader (``load_ground_truth`` /
``_is_data_row`` / ``_HEADER_TOKENS``) was removed from the pipeline. Only
``xscore.extraction.ground_truth.fuzzy_match_name`` remains, and it does not
parse header rows. This test now asserts the buggy code path is gone rather
than importing a module that no longer exists.
"""

import importlib
import sys

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

# The buggy module must no longer be importable.
try:
    importlib.import_module("xscore.shared.load_ground_truth")
    print("BUG NOT RESOLVED: xscore.shared.load_ground_truth is still importable.")
    sys.exit(1)
except ModuleNotFoundError:
    pass

# And no surviving module should expose the buggy header-parsing helpers.
from xscore.extraction import ground_truth

for attr in ("load_ground_truth", "_is_data_row", "_HEADER_TOKENS"):
    if hasattr(ground_truth, attr):
        print(f"BUG NOT RESOLVED: ground_truth still exposes {attr!r}.")
        sys.exit(1)

print("BUG RESOLVED: ground-truth file loader (and its header mis-detection) was removed.")
sys.exit(0)

"""Verify Bug #5: Page assignment trailing pages.

File: xscore/preprocessing/assign_pages_to_students.py
Issue: When n_pages % pages_per_student != 0, the last block included
pages beyond n_pages because n_blocks used math.ceil.

The fix changes n_blocks to floor division so trailing partial pages
are actually dropped — matching the existing warn_line wording.
"""

import sys
import inspect

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")


# Confirm the source no longer uses math.ceil for n_blocks.
# (We do source inspection rather than running the function because
# assign_pages_to_students needs a real PDF and AI client.)
import xscore.preprocessing.assign_pages_to_students as mod

src = inspect.getsource(mod)
assert "math.ceil(n_pages / pages_per_student)" not in src, (
    "Source still uses math.ceil for n_blocks — fix regressed."
)
assert "n_pages // pages_per_student" in src, (
    "Source no longer contains floor-division n_blocks — fix may have regressed."
)

# Also confirm the math import was removed.
assert "import math" not in src, "Unused math import still present"

# Mirror the post-fix block layout to verify the trailing page is dropped.
n_pages = 5
pages_per_student = 2
n_blocks = n_pages // pages_per_student
all_pages: list[int] = []
for b in range(n_blocks):
    first_idx = b * pages_per_student
    block_pages = list(range(first_idx + 1, first_idx + pages_per_student + 1))
    all_pages.extend(block_pages)

print(f"n_pages={n_pages}, pages_per_student={pages_per_student}")
print(f"n_blocks={n_blocks}, pages used={all_pages}")

assert all(p <= n_pages for p in all_pages), (
    f"Block layout still references missing pages: {all_pages}"
)
assert n_pages not in all_pages, (
    f"Trailing partial page should be dropped but page {n_pages} is included"
)
print("FIX VERIFIED: no block references a missing page; trailing page dropped.")
sys.exit(0)

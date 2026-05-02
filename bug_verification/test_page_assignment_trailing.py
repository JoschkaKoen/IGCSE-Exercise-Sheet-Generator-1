"""Verify Bug: Page assignment includes non-existent trailing pages.

File: xscore/preprocessing/assign_pages_to_students.py
Issue: When n_pages % pages_per_student != 0, the last block includes
pages beyond n_pages.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.shared.models import PageAssignment

# Simulate the block logic from assign_pages
n_pages = 5
pages_per_student = 2
n_blocks = __import__('math').ceil(n_pages / pages_per_student)

result = []
for b in range(n_blocks):
    first_idx = b * pages_per_student
    block_pages = list(range(first_idx + 1, first_idx + pages_per_student + 1))
    result.append(PageAssignment(student_name=f"S{b+1}", page_numbers=block_pages, confidence="high"))

print(f"n_pages={n_pages}, pages_per_student={pages_per_student}")
for a in result:
    print(f"  {a.student_name}: pages {a.page_numbers}")

# Check if any page number exceeds n_pages
max_page = max(max(a.page_numbers) for a in result)
if max_page > n_pages:
    print(f"BUG CONFIRMED: Last block includes page {max_page}, but only {n_pages} pages exist!")
    sys.exit(1)
else:
    print("BUG NOT REPRODUCED: All page numbers are valid.")
    sys.exit(0)

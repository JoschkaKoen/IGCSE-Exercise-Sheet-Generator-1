"""Verify Bug: Marking page register uses assert for runtime validation.

File: xscore/marking/marking_page_register.py
Issue: assert ctx.artifact_dir is not None is stripped with python -O.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.marking import marking_page_register
import inspect

src = inspect.getsource(marking_page_register)
asserts_found = []
for i, line in enumerate(src.splitlines(), 1):
    stripped = line.strip()
    if stripped.startswith("assert "):
        asserts_found.append((i, stripped))

if asserts_found:
    print("BUG CONFIRMED: Production assert statements found in marking_page_register.py:")
    for line, text in asserts_found:
        print(f"  line {line}: {text}")
    sys.exit(1)
else:
    print("BUG NOT REPRODUCED: No assert statements found.")
    sys.exit(0)

"""Verify Bug: Blueprint None crash.

File: xscore/marking/blueprints.py
Issue: re.sub(r"_\d+$", "", q.number) crashes if q.number is None.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from dataclasses import dataclass
import re

# Simulate the code from blueprints.py
@dataclass
class FakeQuestion:
    number: str | None

q = FakeQuestion(number=None)

try:
    result = re.sub(r"_\d+$", "", q.number)
    print(f"Result: {result}")
    print("BUG NOT REPRODUCED: None handled gracefully.")
    sys.exit(0)
except TypeError as e:
    print(f"BUG CONFIRMED: TypeError when q.number is None: {e}")
    sys.exit(1)

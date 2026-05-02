"""Verify Bug: Question __post_init__ null-pointer risk.

File: xscore/shared/models.py
Issue: if bbox=None is passed, accessing self.bbox.page raises AttributeError.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.shared.models import Question

try:
    q = Question(number="1", question_type="short_answer", text="test", marks=1, bbox=None)
    print(f"Question created: {q}")
    print("BUG NOT REPRODUCED: None bbox handled gracefully.")
    sys.exit(0)
except AttributeError as e:
    print(f"BUG CONFIRMED: AttributeError when bbox is None: {e}")
    sys.exit(1)

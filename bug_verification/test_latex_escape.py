"""Verify Bug: LaTeX double-escape.

File: xscore/marking/report_latex_text.py
Issue: _latex_escape regex matches every bare backslash indiscriminately.
If text already contains escaped sequences like \\textbackslash{},
it double-escapes them.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.marking.report_latex_text import _latex_escape

# AI already emitted a pre-escaped backslash
text = r"Use \textbackslash{} to escape."
result = _latex_escape(text)
print(f"Input:  {text!r}")
print(f"Output: {result!r}")

# The output should preserve the existing escape, not corrupt it
if result != text and "textbackslash" in result:
    # Check if it was corrupted
    if "textbackslash{}textbackslash" in result or "textbackslash\\{\\}" in result:
        print("BUG CONFIRMED: Pre-escaped text was corrupted by double-escaping!")
        sys.exit(1)

print("BUG NOT REPRODUCED: Pre-escaped text preserved correctly.")
sys.exit(0)

"""Verify Bug: Parse instruction import-time load.

File: xscore/marking/parse_instruction.py
Issue: _SYSTEM_PROMPT = load_prompt(...) executed at import time.
Missing prompt file breaks module import entirely.
"""

import sys
import os
import tempfile

# We can't easily test this without temporarily removing the prompt file,
# which could break other things. Instead, let's inspect the source.

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.marking import parse_instruction
import inspect

src = inspect.getsource(parse_instruction)
# Look for module-level load_prompt call
for i, line in enumerate(src.splitlines(), 1):
    if line.strip().startswith("_SYSTEM_PROMPT") and "load_prompt" in line:
        print(f"BUG CONFIRMED: Module-level prompt load at line {i}:")
        print(f"  {line.strip()}")
        print("  If the prompt file is missing, importing this module will crash.")
        sys.exit(1)

print("BUG NOT REPRODUCED: No module-level prompt load found.")
sys.exit(0)

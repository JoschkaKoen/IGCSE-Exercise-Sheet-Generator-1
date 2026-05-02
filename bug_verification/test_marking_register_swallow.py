"""Verify Bug: Marking register v1 swallow-all exceptions.

File: xscore/steps/geometry.py
Issue: build_marking_register_v1 catches all exceptions, prints warning,
returns normally. run_step records step status as "ok" even though
register was not built.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.steps.geometry import build_marking_register_v1
import inspect

src = inspect.getsource(build_marking_register_v1)

# Check if it catches Exception broadly and returns without re-raising
has_broad_except = "except Exception" in src
has_return_in_except = False

lines = src.splitlines()
for i, line in enumerate(lines):
    if "except Exception" in line:
        # Check next few lines for return without raise
        for j in range(i+1, min(i+5, len(lines))):
            if lines[j].strip().startswith("return"):
                has_return_in_except = True
                break

if has_broad_except and has_return_in_except:
    print("BUG CONFIRMED: build_marking_register_v1 catches Exception broadly and returns without re-raising.")
    print("  This causes run_step to log status='ok' even when the register was not built.")
    sys.exit(1)
else:
    print("BUG NOT REPRODUCED: Exception handling looks correct.")
    sys.exit(0)

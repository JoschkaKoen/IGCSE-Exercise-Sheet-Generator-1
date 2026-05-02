"""Verify Bug: Response parsing naive quote counting.

File: xscore/shared/response_parsing.py
Issue: fixed.count('"') % 2 == 1 does not account for escaped quotes (\\").
A JSON with many escaped quotes could incorrectly trigger repair.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.shared.response_parsing import parse_json_safe

# Valid JSON with an ODD number of total quote chars due to escaped quotes
# 3 unescaped + 4 escaped = 7 total quotes → triggers the naive repair
raw = '{"a": "\\"x\\"", "b": 1}'
print(f"Input: {raw!r}")
print(f"Total quote chars: {raw.count(chr(34))}")

result = parse_json_safe(raw)
print(f"Result: {result!r}")

# This should parse correctly since it's valid JSON
expected_a = '"x"'
if result is None:
    print("BUG CONFIRMED: Valid JSON with escaped quotes was corrupted by repair logic!")
    sys.exit(1)
elif result.get("a") != expected_a:
    print(f"BUG CONFIRMED: Parsed result is corrupted! Expected a={expected_a!r}, got {result.get('a')!r}")
    sys.exit(1)
else:
    print("BUG NOT REPRODUCED: Escaped quotes handled correctly.")
    sys.exit(0)

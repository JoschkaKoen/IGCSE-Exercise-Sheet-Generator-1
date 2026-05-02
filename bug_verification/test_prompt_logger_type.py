"""Verify Bug: Prompt logger crashes on string image_url.

File: xscore/shared/prompt_logger.py
Issue: url = (part.get("image_url") or {}).get("url", "")
If part["image_url"] is a string instead of dict, AttributeError is raised.
The exception is caught by outer try/except and silently fails.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.shared.prompt_logger import save_prompt
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmpdir:
    path = Path(tmpdir) / "prompt.md"

    # Malformed part where image_url is a string, not a dict
    messages = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": "http://example.com/img.jpg"}  # string, not dict!
        ]}
    ]

    save_prompt(path, model="test", messages=messages)

    # The save_prompt should have written the file, but due to the bug
    # the exception is swallowed and nothing is written
    if not path.exists():
        print("BUG CONFIRMED: Prompt was silently not logged (swallowed AttributeError)")
        sys.exit(1)
    else:
        content = path.read_text(encoding="utf-8")
        if "[image_url]" in content:
            print("BUG NOT REPRODUCED: String image_url handled gracefully")
            sys.exit(0)
        else:
            print("BUG CONFIRMED: Prompt was silently not logged (swallowed exception)")
            sys.exit(1)

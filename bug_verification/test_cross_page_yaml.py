"""Verify Bug: Cross-page context hardcodes YAML.

File: xscore/steps/scaffold.py
Issue: detect_cross_page_context hardcodes fmt="yaml" when looking for
exam questions artifact, but extract_exam_questions writes using fmt.artifact_ext()
which could be json or xml.
"""

import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.steps.scaffold import detect_cross_page_context
import inspect

src = inspect.getsource(detect_cross_page_context)

# Check if yaml is hardcoded
if 'fmt="yaml"' in src or "fmt='yaml'" in src:
    print("BUG CONFIRMED: detect_cross_page_context hardcodes fmt='yaml' when loading exam questions.")
    print("  If the scaffold format is json or xml, the artifact will not be found.")
    sys.exit(1)
else:
    print("BUG NOT REPRODUCED: Format is dynamically determined.")
    sys.exit(0)

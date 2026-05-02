"""Verify Bug: Production assert statements stripped with python -O.

Files: Multiple (xscore/pipeline/resume.py, xscore/steps/geometry.py,
       xscore/steps/scaffold.py, xscore/marking/marking_page_register.py)
Issue: assert is used for runtime validation but is stripped by `python -O`.
"""

import sys
import subprocess

# Test that assert is stripped with -O
code = """
import sys
sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

# Simulate what happens with -O when assert is stripped
# We can't easily test the pipeline without mocking, but we can verify
# that the assert statements exist and would be stripped.

from xscore.pipeline.resume import resume_pipeline
from xscore.steps.geometry import exam_geometry
from xscore.steps.scaffold import scaffold_setup

import inspect

asserts_found = []
for name, obj in [("resume_pipeline", resume_pipeline),
                   ("exam_geometry", exam_geometry),
                   ("scaffold_setup", scaffold_setup)]:
    src = inspect.getsource(obj)
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("assert "):
            asserts_found.append((name, i, stripped))

if asserts_found:
    print("BUG CONFIRMED: Production assert statements found:")
    for func, line, text in asserts_found:
        print(f"  {func}:{line}  {text}")
    sys.exit(1)
else:
    print("BUG NOT REPRODUCED: No assert statements found in target functions.")
    sys.exit(0)
"""

result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
sys.exit(result.returncode)

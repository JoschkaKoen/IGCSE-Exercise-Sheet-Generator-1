"""Verify Bug #13: Scaffold resume KeyError.

File: xscore/steps/scaffold.py
Issue: When resuming from step 21 (--from-step 21), scaffold_setup
populated only exam_pdf/answer_pdf/client/fmt/phase_t0. Steps 22+ then
crashed with KeyError because state['raw_questions'] / state['raw_layout']
were never set.

The fix adds _rehydrate_scaffold_state_on_resume which loads the step-20
exam_questions.yaml artifact back into scaffold_state.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

# Source-only check — calling scaffold_setup itself needs a Gemini client.
src = open(
    "/Users/joschka/Desktop/Programming/eXercise/xscore/steps/scaffold.py",
    encoding="utf-8",
).read()

assert "_rehydrate_scaffold_state_on_resume" in src, (
    "Rehydration helper missing from scaffold.py — fix regressed."
)
assert 'state["raw_questions"]' in src, "state['raw_questions'] write missing"
assert 'state["raw_layout"]' in src, "state['raw_layout'] write missing"

# Behavioural test on the helper itself: build a fake artifact_dir with a
# realistic exam_questions.yaml and confirm rehydration populates state.
import yaml

# Need to import without triggering the full chain. The rehydrate helper
# imports lazily, so we can import its module by skipping the heavyweight
# fitz/jinja deps with a stub.
import types
_fake_fitz = types.ModuleType("fitz")
_fake_fitz.Matrix = lambda *a, **kw: None
_fake_fitz.csRGB = None
_fake_fitz.open = lambda *a, **kw: None
sys.modules.setdefault("fitz", _fake_fitz)

from xscore.steps.scaffold import _rehydrate_scaffold_state_on_resume
from xscore.scaffold.formats import get_scaffold_format
from xscore.shared.exam_paths import artifact_exam_questions_path


class _FakeCtx:
    def __init__(self, artifact_dir: Path, from_step: int):
        self.artifact_dir = artifact_dir
        self.from_step = from_step
        self.scaffold_state: dict = {"fmt": get_scaffold_format()}


with tempfile.TemporaryDirectory() as tmpdir:
    artifact_dir = Path(tmpdir)
    fmt = get_scaffold_format()
    qp = artifact_exam_questions_path(artifact_dir, fmt=fmt.artifact_ext())
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text(
        yaml.safe_dump(
            {
                "rows": 1,
                "cols": 1,
                "questions": [
                    {"number": "1", "text": "What is 2+2?"},
                    {"number": "2", "text": "What is the capital of France?"},
                ],
            }
        ),
        encoding="utf-8",
    )

    # Case 1: from_step=19 (detect_cross_page_context) → should rehydrate
    ctx = _FakeCtx(artifact_dir, from_step=19)
    _rehydrate_scaffold_state_on_resume(ctx)
    assert "raw_questions" in ctx.scaffold_state, (
        "raw_questions not populated after resume into detect_cross_page_context"
    )
    assert len(ctx.scaffold_state["raw_questions"]) == 2, (
        f"Expected 2 questions, got {ctx.scaffold_state['raw_questions']}"
    )
    assert ctx.scaffold_state["raw_layout"] == {"rows": 1, "cols": 1}, (
        f"Unexpected raw_layout: {ctx.scaffold_state['raw_layout']}"
    )

    # Case 2: from_step=18 (extract_exam_questions itself) → should NOT rehydrate
    ctx = _FakeCtx(artifact_dir, from_step=18)
    _rehydrate_scaffold_state_on_resume(ctx)
    assert "raw_questions" not in ctx.scaffold_state, (
        "rehydration ran for from_step=18 — should only fire for >18"
    )

    # Case 3: from_step=None (fresh run) → should NOT rehydrate
    ctx = _FakeCtx(artifact_dir, from_step=None)
    _rehydrate_scaffold_state_on_resume(ctx)
    assert "raw_questions" not in ctx.scaffold_state, (
        "rehydration ran for fresh run — should only fire when resuming"
    )

print("FIX VERIFIED: rehydration populates state for from_step>extract_exam_questions, not otherwise.")
sys.exit(0)

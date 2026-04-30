"""Steps 25–27: blueprints → extract answers → AI marking.

Timing is captured by ``run_step`` under the canonical keys
``ai_marking_blueprints``, ``extract_student_answers`` and ``ai_marking`` —
no per-step ``t0`` needed.
"""

from __future__ import annotations

from xscore.config import GEMINI_MAX_OUTPUT_TOKENS, MARKING_MODEL_DEFAULT
from xscore.marking.ai_mark import run_ai_marking
from xscore.marking.blueprints import build_blueprints
from xscore.marking.extract_answers import run_extract_student_answers
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import announce_step_model, ok_line


def step_24_blueprints(ctx: _Ctx) -> None:
    assert ctx.scaffold is not None and ctx.artifact_dir is not None
    blueprints = build_blueprints(ctx.scaffold, ctx.artifact_dir)
    ok_line(f"{len(blueprints)} page blueprint(s) written")


def step_26_extract_answers(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="EXTRACT_ANSWERS_MODEL",
        default_model="qwen3.6-plus, off",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    ctx.extract_answers_api_calls = run_extract_student_answers(ctx, dpi=ctx.instruction.dpi)
    n_calls = len(ctx.extract_answers_api_calls)
    n_failed = len(getattr(ctx, "extract_answers_failures", []))
    n_total = n_calls + n_failed
    ok_line(
        f"{n_calls}/{n_total} pages extracted"
        + (f"  ·  {n_failed} failed (will fall back to AI transcription during marking)" if n_failed else "")
    )


def step_25_mark(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="MARKING_MODEL",
        default_model=MARKING_MODEL_DEFAULT,
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    ctx.marking_api_calls = run_ai_marking(ctx, dpi=ctx.instruction.dpi)
    n_calls = len(ctx.marking_api_calls)
    n_failed = len(ctx.marking_failures)
    n_total = n_calls + n_failed
    ok_line(
        f"{n_calls}/{n_total} pages marked"
        + (f"  ·  {n_failed} failed" if n_failed else "")
    )

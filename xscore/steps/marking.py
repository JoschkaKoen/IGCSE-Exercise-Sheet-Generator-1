"""Steps 21–22: build per-page marking blueprints, then run AI vision marking.

Timing is captured by ``run_step`` under the canonical keys
``ai_marking_blueprints`` and ``ai_marking`` — no per-step ``t0`` needed.
"""

from __future__ import annotations

from xscore.marking.ai_mark import run_ai_marking
from xscore.marking.blueprints import build_blueprints
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import ok_line


def step_21_blueprints(ctx: _Ctx) -> None:
    assert ctx.scaffold is not None and ctx.artifact_dir is not None
    blueprints = build_blueprints(ctx.scaffold, ctx.artifact_dir)
    ok_line(f"{len(blueprints)} page blueprint(s) written")


def step_22_mark(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    ctx.marking_api_calls = run_ai_marking(ctx, dpi=ctx.instruction.dpi)
    n_calls = len(ctx.marking_api_calls)
    n_failed = len(ctx.marking_failures)
    n_total = n_calls + n_failed
    ok_line(
        f"{n_calls}/{n_total} pages marked"
        + (f"  ·  {n_failed} failed" if n_failed else "")
    )

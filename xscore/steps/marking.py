"""AI-marking step bodies: blueprints → extract answers → AI marking.

Timing is captured by ``run_step`` under the canonical keys
``ai_marking_blueprints``, ``extract_student_answers`` and ``ai_marking`` —
no per-step ``t0`` needed.
"""

from __future__ import annotations

import time

from xscore.config import GEMINI_MAX_OUTPUT_TOKENS, MARKING_MODEL_DEFAULT
from xscore.marking.ai_mark import run_ai_marking
from xscore.marking.blueprints import build_blueprints
from xscore.marking.extract_answers import run_extract_student_answers
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import (
    announce_step_model,
    blank_line,
    format_duration,
    info_line,
    ok_line,
)


def ai_marking_blueprints(ctx: _Ctx) -> None:
    assert ctx.scaffold is not None and ctx.artifact_dir is not None
    blueprints = build_blueprints(ctx.scaffold, ctx.artifact_dir)
    # build_blueprints returns one entry per page in the exam (1..page_count);
    # only entries with questions get a file written to disk (cover/blank/
    # question-free pages are skipped). Count the latter so the message
    # matches the on-disk artifact count.
    n_written = sum(1 for bp in blueprints if bp.get("questions"))
    ok_line(f"{n_written} page blueprint(s) written")


def extract_student_answers(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="EXTRACT_ANSWERS_MODEL",
        default_model="qwen3.6-plus, off",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    from xscore.config import MARKING_JPEG_QUALITY  # noqa: PLC0415
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
    announce_ai_input(
        kind="JPEG", dpi=ctx.instruction.dpi, quality=MARKING_JPEG_QUALITY,
        note="re-encode; embedded JPEGs passed verbatim on fast path",
    )
    ctx.extract_answers_api_calls = run_extract_student_answers(ctx, dpi=ctx.instruction.dpi)
    n_calls = len(ctx.extract_answers_api_calls)
    n_failed = len(getattr(ctx, "extract_answers_failures", []))
    n_total = n_calls + n_failed
    blank_line()
    ok_line(
        f"{n_calls}/{n_total} pages extracted"
        + (f"  ·  {n_failed} failed (will fall back to AI transcription during marking)" if n_failed else "")
    )


def ai_marking(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="MARKING_MODEL",
        default_model=MARKING_MODEL_DEFAULT,
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    import os as _os  # noqa: PLC0415
    from eXercise.ai_client import resolve_active_model  # noqa: PLC0415
    from xscore.config import MARKING_JPEG_QUALITY  # noqa: PLC0415
    from xscore.shared.exam_paths import artifact_mark_scheme_graphics_dir  # noqa: PLC0415
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
    _marking_model, _marking_provider, _ = resolve_active_model(
        ("MARKING_MODEL",), default=MARKING_MODEL_DEFAULT,
    )
    if _marking_provider == "gemini":
        announce_ai_input(
            kind="PDF",
            note="Gemini, native bytes — re-extracted from cleaned scan",
        )
    else:
        announce_ai_input(
            kind="JPEG", dpi=ctx.instruction.dpi, quality=MARKING_JPEG_QUALITY,
        )
    _gfx_dir = artifact_mark_scheme_graphics_dir(ctx.artifact_dir)
    if _gfx_dir.is_dir() and any(_gfx_dir.glob("*.png")):
        announce_ai_input(
            label="scheme graphics", kind="PNG",
            dpi=int(_os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300")),
        )
    t0 = time.perf_counter()
    ctx.marking_api_calls = run_ai_marking(ctx, dpi=ctx.instruction.dpi)
    elapsed = time.perf_counter() - t0
    n_calls = len(ctx.marking_api_calls)
    n_failed = len(ctx.marking_failures)
    n_total = n_calls + n_failed
    ok_line(
        f"{n_calls} / {n_total} pages marked  ·  {format_duration(elapsed)}"
        + (f"  ·  {n_failed} failed" if n_failed else "")
    )

    # Audit log + console summary of MCQ corrections the marker applied via
    # the corrected_student_answer field. The audit YAML is always written
    # (empty list when nothing was corrected) so a downstream tool can rely
    # on its existence; the console table only renders when there's something
    # to report.
    import yaml as _yaml
    from xscore.shared.path_builders import artifact_mcq_corrections_path
    corrections = list(getattr(ctx, "mcq_corrections", []) or [])
    _path = artifact_mcq_corrections_path(ctx.artifact_dir)
    _path.parent.mkdir(parents=True, exist_ok=True)
    _path.write_text(
        _yaml.safe_dump(
            {"total_corrections": len(corrections), "corrections": corrections},
            default_flow_style=False, sort_keys=False, allow_unicode=True,
        ),
        encoding="utf-8",
    )
    if corrections:
        blank_line()
        info_line(f"MCQ corrections ({len(corrections)}):")
        _w = max(len(str(c.get("student", ""))) for c in corrections)
        _wp = max(len(str(c.get("page", ""))) for c in corrections)
        _ws = max(len(str(c.get("scan_page", ""))) for c in corrections)
        for c in corrections:
            info_line(
                f"  {str(c.get('student', '')):<{_w}}  "
                f"ans p{str(c.get('page', '')):>{_wp}}  "
                f"scan p{str(c.get('scan_page', '')):>{_ws}}  "
                f"Q{c.get('number')}: {c.get('from')} → {c.get('to')}"
            )

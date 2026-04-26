"""Steps 1–2: parse grading instructions, locate exam folder + bootstrap resume.

Both steps are marked ``bootstrap=True`` in the registry so they always run
even when ``--from-step N`` (N > 1) is supplied — they populate
``ctx.instruction``, ``ctx.folder``, ``ctx.artifact_dir`` that later steps
depend on.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

from xscore.config import GEMINI_MAX_OUTPUT_TOKENS
from xscore.shared.find_exam_folder import find_folder, validate_input_files
from xscore.marking.parse_instruction import parse_prompt
from xscore.pipeline.resume import copy_input_files, resume_pipeline
from xscore.shared.exam_paths import artifact_parse_summary_path
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import announce_step_model, format_duration, ok_line


def step_01_parse(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="01_INTERPRET_PROMPT_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_model="gemini-2.5-flash",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    t0 = time.perf_counter()
    ctx.instruction = parse_prompt(ctx.args.prompt, dpi_override=ctx.args.dpi)
    ctx.parse_elapsed = time.perf_counter() - t0
    assert ctx.instruction is not None
    inst = ctx.instruction

    ctx.force_clean_scan = ctx.args.force_clean_scan or inst.force_clean_scan
    if ctx.from_step is None and inst.from_step is not None:
        ctx.from_step = inst.from_step

    task_labels = {
        "check_answers": "Grade answers",
        "check_mc": "Multiple choice only",
        "count_marks": "Count marks",
        "build_scaffold": "Build structure",
        "clean_scan": "Clean scan",
    }
    task_label = task_labels.get(inst.task_type, inst.task_type.replace("_", " ").strip())
    sf = inst.student_filter
    if sf.mode == "all":
        scope = "all students"
    elif sf.mode == "first_n" and sf.n > 0:
        scope = f"first {sf.n} students"
    elif sf.names:
        scope = f"{len(sf.names)} named students"
    else:
        scope = sf.mode.replace("_", " ")
    ok_line(
        f"{task_label}  ·  {scope}  ·  {inst.dpi} DPI  ·  "
        f"{format_duration(ctx.parse_elapsed)}"
    )


def step_02_folder(ctx: _Ctx) -> None:
    assert ctx.instruction is not None
    ctx.folder = find_folder(
        instruction_hint=ctx.instruction.folder_hint,
        cli_override=ctx.args.folder,
        ai_folder_path=None if ctx.args.folder else ctx.instruction.folder_path,
    )
    assert ctx.folder is not None
    stem = ctx.folder.name.replace(" ", "_")
    exam_output_root = Path("output") / "xscore" / stem
    exam_output_root.mkdir(parents=True, exist_ok=True)
    ctx.artifact_dir = exam_output_root / ctx.timestamp
    suffix = 1
    while ctx.artifact_dir.exists():
        suffix += 1
        ctx.artifact_dir = exam_output_root / f"{ctx.timestamp}_{suffix}"
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    if ctx.from_step:
        resume_pipeline(ctx)
    ok_line(f"Output: {ctx.artifact_dir}")
    (ctx.artifact_dir / "command.txt").write_text(
        "python " + shlex.join([Path(sys.argv[0]).name] + sys.argv[1:]),
        encoding="utf-8",
    )

    # Write step 1 summary now that artifact_dir exists (created here, not in step 1)
    inst = ctx.instruction
    step1_summary = {
        "step": 1,
        "elapsed_s": round(ctx.parse_elapsed, 3),
        "task_type": inst.task_type,
        "dpi": inst.dpi,
        "status": "ok",
    }
    p1 = artifact_parse_summary_path(ctx.artifact_dir)
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_text(json.dumps(step1_summary, indent=2, ensure_ascii=False), encoding="utf-8")

    ok_line(ctx.folder.name)
    validate_input_files(ctx.folder)
    copy_input_files(ctx.folder, ctx.artifact_dir)

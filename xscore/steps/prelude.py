"""Bootstrap step bodies: parse grading instructions, locate exam folder + resume setup.

Both step bodies are marked ``bootstrap=True`` in the registry so they always
run even when ``--from-step`` is supplied — they populate ``ctx.instruction``,
``ctx.folder``, ``ctx.artifact_dir`` that later steps depend on.
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
from xscore.shared.exam_paths import artifact_parse_prompt_path, artifact_parse_summary_path
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.pipeline_steps import step_by_name
from xscore.shared.terminal_ui import announce_step_model, format_duration, ok_line


def parse_grading_instructions(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="INTERPRET_PROMPT_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_model="gemini-2.5-flash",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    t0 = time.perf_counter()
    ctx.parse_prompt_debug = {}
    ctx.instruction = parse_prompt(
        ctx.args.prompt,
        out=ctx.parse_prompt_debug,
    )
    ctx.parse_elapsed = time.perf_counter() - t0
    assert ctx.instruction is not None
    inst = ctx.instruction

    ctx.force_clean_scan = ctx.args.force_clean_scan or inst.force_clean_scan
    if ctx.from_step is None and inst.from_step is not None:
        ctx.from_step = inst.from_step
    if ctx.args.stop_after is None and inst.stop_after is not None:
        ctx.stop_after = inst.stop_after

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


def locate_exam_folder(ctx: _Ctx) -> None:
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

    # Write the parse-grading-instructions summary now that artifact_dir exists
    # (created here, not earlier).
    inst = ctx.instruction
    step1_summary = {
        "step": step_by_name("parse_grading_instructions").number,
        "elapsed_s": round(ctx.parse_elapsed, 3),
        "task_type": inst.task_type,
        "status": "ok",
    }
    p1 = artifact_parse_summary_path(ctx.artifact_dir)
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_text(json.dumps(step1_summary, indent=2, ensure_ascii=False), encoding="utf-8")

    # Write the parse-grading-instructions prompt + response (deferred because artifact_dir
    # didn't exist yet). Skipped silently if the parse used the heuristic
    # fallback before the AI call populated the buffer.
    debug = ctx.parse_prompt_debug or {}
    if debug.get("raw"):
        prompt_path = artifact_parse_prompt_path(ctx.artifact_dir)
        save_prompt(
            prompt_path,
            model=debug.get("model", ""),
            system=debug.get("system", ""),
            messages=[{"role": "user", "content": debug.get("user", "")}],
        )
        save_response(prompt_path, debug["raw"], thinking=debug.get("thinking", ""))

    ok_line(ctx.folder.name)
    validate_input_files(ctx.folder)
    copy_input_files(ctx.folder, ctx.artifact_dir)

"""Generate sample marking prompts for human review BEFORE any API calls.

After Phase 3 changes the blueprint rendering, we want to see what the model
will receive — without spending tokens. This mirrors what `_mark_page` would
do up to the API call: load the on-disk blueprint, patch with step-28 student
answers, strip the answer key, render via `_blueprint_for_prompt`, build the
full system + user prompt, and save as .txt.

Run from repo root:
    .venv/bin/python bug_verification/generate_phase3_sample_prompts.py

Output goes to bug_verification/phase3_sample_prompts/.

NO API CALLS are made. Image attachments are NOT generated; the prompts
note where the page image and any mark-scheme graphics would be attached.
"""

from __future__ import annotations

from pathlib import Path

from xscore.marking.blueprints import is_all_mcq_page
from xscore.marking.extract_answers import (
    blueprint_for_marking,
    load_student_answers,
    patch_blueprint_with_answers,
)
from xscore.marking.formats import get_marking_format
from xscore.marking.mark_page import _blueprint_for_prompt, _build_marking_system_prompt
from xscore.prompts.loader import load_prompt
from xscore.shared.exam_paths import artifact_blueprint_path


RUN_DIR = Path(
    "output/xscore/w23_23_Unit_Test/2026-05-10_18-58-37"
)
OUT_DIR = Path("bug_verification/phase3_sample_prompts")

# Pages picked to span the variety of marking scenarios:
#   - p2: MCQ-only (uses the ai_marking_mcq.md prompt path; not affected by
#         Phase 3 since that prompt is unchanged — included for comparison).
#   - p11: short_answer with the long blank trace-table that triggered
#          the original bug.
#   - p14: continuation page (overflow scan attached as a second image).
#   - p3:  short_answer with a mark-scheme graphic attached.
SAMPLE_PAGES = [
    ("Linus", 2, False),   # is_continuation
    ("Linus", 11, False),
    ("Linus", 14, True),
    ("Linus", 3, False),
]


def render_full_prompt(student: str, page: int, is_continuation: bool) -> str:
    fmt = get_marking_format()

    bp_path = artifact_blueprint_path(RUN_DIR, page, fmt=fmt.artifact_ext())
    blueprint_str = bp_path.read_text(encoding="utf-8")

    answers_map = load_student_answers(RUN_DIR, student, page) or {}
    if answers_map:
        blueprint_str = patch_blueprint_with_answers(blueprint_str, answers_map, fmt)
    blueprint_str = blueprint_for_marking(blueprint_str)

    blueprint = fmt.deserialize_blueprint(blueprint_str)
    blueprint["student_name"] = student
    is_all_mcq = is_all_mcq_page(blueprint.get("questions") or [])

    # System prompt — full assembly with conditional fragments.
    # Mirrors _mark_page's call to _build_marking_system_prompt (sans real
    # mark-scheme-graphics list — sample-only, kept empty for simplicity).
    system_prompt = _build_marking_system_prompt(
        blueprint,
        scheme_graphics=(),
        has_continuation=is_continuation,
        fmt=fmt,
        is_cs=True,                     # w23_23 is a Computer Science exam
        has_student_answers=bool(answers_map),
        is_all_mcq=is_all_mcq,
    )

    # User prompt — section name varies by all-MCQ.
    user_prompt_name = "ai_marking_mcq" if is_all_mcq else fmt.prompt_name()
    _, user_text = load_prompt(
        user_prompt_name, section="user",
        blueprint=_blueprint_for_prompt(blueprint_str),
    )

    image_note = "  (page image attached as the first image part)"
    if is_continuation:
        image_note += "\n  (continuation-page image(s) attached after the primary page)"

    return (
        "================================================================\n"
        f"  Sample marking prompt — student={student!r} page={page}\n"
        f"  is_all_mcq={is_all_mcq}  is_continuation={is_continuation}\n"
        f"  Source blueprint: {bp_path}\n"
        "================================================================\n\n"
        "----- IMAGE ATTACHMENTS (not shown here) -----\n"
        f"{image_note}\n\n"
        "----- SYSTEM MESSAGE -----\n"
        f"{system_prompt}\n\n"
        "----- USER MESSAGE (text part — image parts go before this) -----\n"
        f"{user_text}\n"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for student, page, is_cont in SAMPLE_PAGES:
        try:
            text = render_full_prompt(student, page, is_cont)
        except FileNotFoundError as exc:
            print(f"SKIP {student} p{page}: {exc}")
            continue
        out_path = OUT_DIR / f"sample_{student}_p{page:02d}.txt"
        out_path.write_text(text, encoding="utf-8")
        print(f"WROTE {out_path}  ({len(text)} chars)")


if __name__ == "__main__":
    main()

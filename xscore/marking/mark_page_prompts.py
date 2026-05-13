"""Prompt construction for the AI marking call.

Three helpers:

- :func:`_bq_key` — group key for blueprint questions (used both here for
  retry-blueprint pairing and in :mod:`xscore.marking.mark_page_postprocess`
  for response → blueprint matching).
- :func:`_blueprint_for_prompt` — render the on-disk YAML blueprint as the
  prose+minimal-YAML body the marking AI is shown for ``$blueprint``
  substitution.
- :func:`_build_marking_system_prompt` — assemble sections A–H of the
  ``ai_marking.md`` system prompt (or the short ``ai_marking_mcq.md`` fast
  path for all-MCQ pages).

Extracted from ``mark_page.py`` as part of the file-split into prompts /
call+retry / postprocess.
"""

from __future__ import annotations

import re

import yaml

from xscore.marking.formats.base import MarkingFormat
from xscore.prompts.loader import load_prompt


def _bq_key(bq: dict) -> tuple:
    """Group key for a blueprint question: (bare_number, subpage_row, subpage_col).

    The _N suffix is stripped so Q38 and Q38_2 share the same group; blueprint
    questions consume positionally so Q38 gets group[0] and Q38_2 gets group[1].
    """
    _row = bq.get("subpage_row")
    _col = bq.get("subpage_col")
    num = re.sub(r'_\d+$', '', str(bq.get("number", "")))
    return (
        num,
        int(_row) if _row is not None else 1,
        int(_col) if _col is not None else 1,
    )


def _blueprint_for_prompt(blueprint_str: str) -> str:
    """Render the YAML blueprint as a prose context section + minimal YAML
    form, for the marking prompt's ``$blueprint`` substitution.

    The on-disk blueprint format (a single YAML document with question_text,
    options, criteria, student_answer, plus the four empty fill-target slots)
    is preserved on disk. Only the model-facing rendering is restructured:

    - **Prose context** (markdown): per-question header (number, type,
      max_marks, subpage if grid) + question_text + options + mark-scheme
      guidance + student_answer (read-only, from extract_student_answers).
    - **Minimal YAML form**: per-question entry with only ``number`` and the
      empty fill-target slots (``assigned_marks``/``explanation`` for
      non-MCQ, ``confidence``/``problem`` always; MCQ entries omit
      ``assigned_marks``/``explanation`` since marks/explanations are
      auto-computed downstream).

    Removing question_text and other long content from the YAML form
    eliminates the corruption surface where the model's echo of those fields
    could break parsing (see run 2026-05-10_18-58-37 — Linus p11 q8a/q8b
    defaulted to 0 after the model re-emitted question_text and added a
    literal ``......`` abbreviation line that broke the YAML block scalar).

    The parser is unchanged: it already only consumes ``number`` plus the
    fill fields and silently ignores any echoed extras.
    """
    data = yaml.safe_load(blueprint_str) or {}
    page = data.get("page", 1)
    layout = data.get("layout") or {"rows": 1, "cols": 1}
    is_grid = int(layout.get("rows", 1)) > 1 or int(layout.get("cols", 1)) > 1
    questions = data.get("questions") or []

    out: list[str] = [f"# Page {page} — questions to mark", ""]

    for q in questions:
        num = q.get("number", "")
        qtype = str(q.get("type", "short_answer"))
        mm = int(q.get("max_marks", 0) or 0)
        header = f"## Q{num} — {qtype}, max {mm} mark" + ("s" if mm != 1 else "")
        if is_grid:
            header += (
                f" (subpage row {q.get('subpage_row', 1)},"
                f" col {q.get('subpage_col', 1)})"
            )
        out.append(header)
        out.append("")

        qtext = str(q.get("question_text") or "").rstrip()
        if qtext:
            out.append("Question:")
            out.append(qtext)
            out.append("")

        opts = q.get("options") or []
        if opts:
            out.append("Options:")
            for o in opts:
                out.append(f"- {o.get('letter', '')}: {o.get('text', '')}")
            out.append("")

        crits = q.get("criteria") or []
        if crits:
            out.append("Mark scheme guidance:")
            for c in crits:
                out.append(f"- {c.get('mark', '')}: {c.get('criterion', '')}")
            out.append("")

        sa = str(q.get("student_answer") or "").rstrip()
        out.append(
            "Student answer (read-only — verbatim from extract_student_answers; "
            "do not modify or re-emit):"
        )
        out.append(sa if sa else "[blank]")
        out.append("")

    out.append("---")
    out.append("")
    out.append(
        "Fill the form below for the questions above. "
        "Reply with ONLY this YAML structure:"
    )
    out.append("")
    out.append("```yaml")
    out.append(f"page: {page}")
    if is_grid:
        out.append(
            f"layout: {{rows: {int(layout.get('rows', 1))}, "
            f"cols: {int(layout.get('cols', 1))}}}"
        )
    out.append("questions:")
    for q in questions:
        num = q.get("number", "")
        qtype = str(q.get("type", "short_answer"))
        out.append(f"  - number: '{num}'")
        if is_grid:
            out.append(f"    subpage_row: {int(q.get('subpage_row', 1))}")
            out.append(f"    subpage_col: {int(q.get('subpage_col', 1))}")
        if qtype == "multiple_choice":
            out.append("    confidence:")
            out.append("    problem:")
        else:
            out.append("    assigned_marks:")
            out.append("    explanation:")
            out.append("    confidence:")
            out.append("    problem:")
    out.append("```")

    return "\n".join(out)


def _build_marking_system_prompt(
    blueprint: dict,
    scheme_graphics: "list[tuple[str, int, str, str]]" = (),
    *,
    has_continuation: bool = False,
    fmt: "MarkingFormat | None" = None,
    is_cs: bool = False,
    has_student_answers: bool = False,
    is_all_mcq: bool = False,
) -> str:
    """Build the system prompt shared by the JPEG and Gemini PDF marking paths.

    Step extract_student_answers (``extract_student_answers``) always runs before ai_marking in the
    live pipeline; the blueprint reaches this function with student answers
    already transcribed and renamed to ``transcribed_answer``. The
    FIELD_RULES fragment instructs the marker to treat that field as
    read-only input and emit only ``assigned_marks``, ``explanation``,
    ``confidence``, and ``problem`` (plus ``corrected_student_answer`` for
    MCQs only).

    *is_all_mcq* — when True, swap in the short ``ai_marking_mcq`` system
    prompt (no FIELD_RULES, CONTINUATION, or CODE_FORMATTING). MCQ pages
    don't carry continuation overflow and aren't code; their marks are
    auto-computed downstream.

    *has_student_answers* — accepted for backward compat with callers that
    haven't been updated yet; ignored.
    """
    if fmt is None:
        fmt = MarkingFormat()
    layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
    rows, cols = int(layout.get("rows", 1)), int(layout.get("cols", 1))

    # --- Sections A + B + C + D: role/task, field rules, output format, format validity ---
    # The ai_marking.md SYSTEM section embeds A, C, D around a $field_rules
    # placeholder. The FIELD_RULES section (same file) is loaded first with
    # $criterion_ref so the assembled system prompt is byte-identical to the
    # pre-merge two-file layout. The all-MCQ fast path skips FIELD_RULES
    # entirely — ai_marking_mcq.md is self-contained.
    if is_all_mcq:
        _, system_prompt = load_prompt("ai_marking_mcq", section="system")
    else:
        _, _b = load_prompt(
            "ai_marking", section="field_rules", criterion_ref=fmt.criterion_ref(),
        )
        _, system_prompt = load_prompt(
            fmt.prompt_name(), section="system", field_rules=_b.rstrip("\n"),
        )
    system_prompt = system_prompt.rstrip("\n")

    # --- Section E: grid navigation (only for multi-subpage layouts) ---
    if rows > 1 or cols > 1:
        _, _e = load_prompt(
            "ai_marking",
            section="grid",
            rows=rows,
            cols=cols,
            subpage_ref=fmt.subpage_ref(),
        )
        system_prompt += "\n\n" + _e.rstrip("\n")

    # --- Section F: mark-scheme graphics (only when present) ---
    if scheme_graphics:
        _seen: dict[str, int] = {}
        for _qn, _, _, _ in scheme_graphics:
            _seen[_qn] = _seen.get(_qn, 0) + 1
        _idx: dict[str, int] = {}
        _lines: list[str] = []
        for _qn, _, _, _transcript in scheme_graphics:
            _idx[_qn] = _idx.get(_qn, 0) + 1
            _label = f"image {_idx[_qn]}" if _seen[_qn] > 1 else "image"
            _hdr = f"  • Question {_qn} expected answer → {_label}"
            _t = (_transcript or "").strip()
            if _t:
                _indented = "\n".join(f"      {ln}" for ln in _t.splitlines())
                _lines.append(f"{_hdr}\n    Transcription:\n{_indented}")
            else:
                _lines.append(_hdr)
        _, _f = load_prompt(
            "ai_marking", section="graphics", graphics_lines="\n".join(_lines),
        )
        system_prompt += "\n\n" + _f.rstrip("\n")

    # --- Section G: continuation pages ---
    # Skipped on all-MCQ pages: single-letter answers can't overflow.
    if has_continuation and not is_all_mcq:
        _, _g = load_prompt("ai_marking", section="continuation")
        system_prompt += "\n\n" + _g.rstrip("\n")

    # --- Section H: code formatting (only for Computer Science exams) ---
    # Skipped on all-MCQ pages: MCQs aren't code.
    if is_cs and not is_all_mcq:
        _, _h = load_prompt("ai_marking", section="code_formatting")
        system_prompt += "\n\n" + _h.rstrip("\n")

    return system_prompt

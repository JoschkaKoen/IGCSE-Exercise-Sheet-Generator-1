"""Per-page rendering and AI marking call for the grading pipeline."""

from __future__ import annotations

import base64
import re
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eXercise.ai_client import collect_streamed_response, is_503_error
from xscore.config import MARKING_JPEG_QUALITY
from xscore.marking.formats.base import FormatParseError, MarkingFormat
from xscore.marking.mark_xml import MarkingFailure
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import warn_line


def _render_page_b64(doc: Any, page_idx: int, dpi: int = 300) -> str:
    """Render a fitz Document page at *page_idx* as base64 JPEG.

    The document must be already open; the caller owns its lifecycle.
    Default DPI matches MARKING_DPI (300); override via the dpi parameter.
    """
    import fitz
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = doc[page_idx].get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return base64.b64encode(pix.tobytes("jpeg", jpg_quality=MARKING_JPEG_QUALITY)).decode()


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


def _build_marking_system_prompt(
    blueprint: dict,
    scheme_graphics: "list[tuple[str, int, str]]" = (),
    *,
    has_continuation: bool = False,
    fmt: "MarkingFormat | None" = None,
) -> str:
    """Build the system prompt shared by the JPEG and Gemini PDF marking paths."""
    if fmt is None:
        from xscore.marking.formats.xml_format import XmlMarkingFormat
        fmt = XmlMarkingFormat()
    layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
    rows, cols = int(layout.get("rows", 1)), int(layout.get("cols", 1))

    # --- Section A: role + task ---
    system_prompt = fmt.section_A()

    # --- Section B: field rules ---
    system_prompt += (
        "\n\nFill each field as follows:\n"
        "1. student_answer — transcribe exactly what the student wrote:\n"
        "   • multiple_choice: report the single letter the student physically marked "
        "(written, circled, crossed, or ticked). Report '?' if nothing is marked. "
        "Do NOT infer from the question or your subject knowledge — only report what is physically visible.\n"
        "   • calculation: transcribe the student's full working and final answer verbatim.\n"
        "   • all other types: copy the student's written answer verbatim. "
        "Mark unreadable words with [?].\n"
        "   The output is placed verbatim in a LaTeX document. "
        "Escape characters that appear literally in the student's answer: "
        "% → \\%, $ → \\$, # → \\#, _ → \\_, { → \\{, } → \\}, "
        "backslash → \\textbackslash{}. "
        "Use \\newline for line breaks; do not include literal newlines.\n"
        "2. assigned_marks — an integer 0–max_marks.\n"
        "   • Award 1 mark for each criterion the student satisfies, up to max_marks.\n"
        "   • For 'any N from' lists, each listed item is a separate mark point.\n"
        f"   • If {fmt.criterion_ref()} are absent or empty, use the correct_answer field "
        "and good judgement to assess the student's answer; accept semantically equivalent "
        "answers, not only verbatim matches.\n"
        "   • For multiple_choice: compare student_answer to correct_answer; "
        "award max_marks if they match, 0 otherwise.\n"
        "3. explanation: clear, easy to understand, short, simple english. Avoid difficult English words "
        "(non native, high school english speakers). "
        "Address the student directly using 'you'. "
        "You can make important words bold using LaTeX syntax \\textbf{word}: only for important words. "
        "NEVER use markdown bold **word** — it breaks the PDF renderer. "
        "Escape non-math special characters that appear literally in your prose: "
        "% → \\%, _ → \\_. "
        "Use \\newline for line breaks. "
        "Use \\newline to break into a new paragraph after each idea. "
        "Break long dense blocks into paragraphs. "
        "Do not append a mark tally (e.g. '— 1 mark.') at the end."
    )

    # --- Section C: output format ---
    system_prompt += fmt.section_C(rows, cols)

    # --- Section D: format validity + LaTeX ---
    system_prompt += fmt.section_D()

    # --- Section E: grid navigation (only for multi-subpage layouts) ---
    if rows > 1 or cols > 1:
        system_prompt += (
            f"\n\nThis page is divided into a {rows}×{cols} grid — "
            f"the {fmt.subpage_ref()} at the top of the blueprint label each quadrant. "
            "Each question's subpage_row and subpage_col identify its quadrant; "
            "do not confuse answers from different quadrants. "
            "order_in_subpage (1 = topmost) gives the vertical position within a quadrant. "
            "The same question number may appear more than once — always identify questions "
            "by subpage_row + subpage_col + question text, not by number alone."
        )

    # --- Section F: mark-scheme graphics (only when present) ---
    if scheme_graphics:
        _seen: dict[str, int] = {}
        for _qn, _, _ in scheme_graphics:
            _seen[_qn] = _seen.get(_qn, 0) + 1
        _idx: dict[str, int] = {}
        _lines = [
            "\n\nThe mark scheme for the following question(s) includes a diagram or graph "
            "as the expected answer. The corresponding mark-scheme images are appended "
            "after the student's page in the order listed below:"
        ]
        for _qn, _, _ in scheme_graphics:
            _idx[_qn] = _idx.get(_qn, 0) + 1
            _label = f"image {_idx[_qn]}" if _seen[_qn] > 1 else "image"
            _lines.append(f"  • Question {_qn} expected answer → {_label}")
        _lines.append("Use these images when assessing the student's diagram or graph for the listed questions.")
        system_prompt += "\n".join(_lines)

    # --- Section G: continuation pages (Gemini PDF multi-page path only) ---
    if has_continuation:
        system_prompt += (
            "\n\nThe student used continuation pages for additional writing. "
            "All pages are included in this document. Mark them together as one answer."
        )

    return system_prompt


def _mark_page(
    client: Any,
    model_id: str,
    b64: str,
    blueprint: dict,
    thinking_kw: dict,
    blueprint_xml: str = "",
    use_stream: bool = False,
    prompt_save_path: Path | None = None,
    warn: Callable[[str], None] = warn_line,
    scheme_graphics: list[tuple[str, int, str]] = (),
    fmt: "MarkingFormat | None" = None,
    extra_b64: list[str] = (),
) -> dict:
    """Vision call to fill in a marking blueprint for one scan page.

    Raises :class:`MarkingFailure` if all attempts are exhausted.
    *extra_b64* — additional continuation-page images appended after the main image.
    """
    if fmt is None:
        from xscore.marking.formats.xml_format import XmlMarkingFormat
        fmt = XmlMarkingFormat()
    use_stream = use_stream and fmt.prefer_stream()
    system_prompt = _build_marking_system_prompt(
        blueprint, scheme_graphics, has_continuation=bool(extra_b64), fmt=fmt
    )

    user_text = fmt.build_user_text(blueprint_xml)
    _user_content: list[dict] = [
        {"type": "text", "text": user_text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    for _cb64 in extra_b64:
        _user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_cb64}"}})
    for _, _, _g_b64 in scheme_graphics:
        _user_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_g_b64}"}})
    kwargs: dict[str, Any] = dict(
        model=model_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _user_content},
        ],
    )
    kwargs.update(thinking_kw)
    kwargs.update(fmt.api_extra_kwargs(model_id))

    save_prompt(prompt_save_path, model=model_id, messages=kwargs["messages"])

    _last_exc: BaseException = RuntimeError("no attempts made")
    _last_raw: str = ""
    _actual_attempts = 0
    for attempt in range(2):  # initial attempt + 1 retry on 503
        _actual_attempts += 1
        try:
            if use_stream:
                stream = client.chat.completions.create(**kwargs, stream=True)
                raw = collect_streamed_response(stream)
            else:
                resp = client.chat.completions.create(**kwargs)
                raw = resp.choices[0].message.content or ""
            _last_raw = raw
            try:
                parsed_questions = fmt.parse_response(raw)
            except FormatParseError as exc:
                warn(f"Marking parse error — marking aborted ({exc})")
                _last_exc = exc
                break
            result = blueprint.copy()
            fill_groups: dict[tuple, list] = defaultdict(list)
            for q in parsed_questions:
                fill_groups[_bq_key(q)].append(q)

            fill_group_idx: dict[tuple, int] = defaultdict(int)
            _unfilled = []
            for bq in result.get("questions", []):
                key = _bq_key(bq)
                idx = fill_group_idx[key]
                fill_group_idx[key] += 1
                group = fill_groups.get(key, [])
                if idx < len(group):
                    fq = group[idx]
                    bq["student_answer"] = fq['student_answer']
                    bq["assigned_marks"] = fq['assigned_marks']
                    bq["explanation"] = fq['explanation']
                else:
                    _unfilled.append(bq.get("number"))

            if _unfilled:
                warn(f"Marking: {len(_unfilled)} blueprint question(s) skipped by AI: {_unfilled}")
            _unmatched: list[str] = []
            for key, grp in fill_groups.items():
                excess = len(grp) - fill_group_idx.get(key, 0)
                for fq in grp[fill_group_idx.get(key, 0):fill_group_idx.get(key, 0) + max(0, excess)]:
                    _unmatched.append(fq.get("number") or str(key))
            if _unmatched:
                warn(f"Marking: AI returned question(s) with no blueprint match: {_unmatched}")
            _fix_mc_marks(result)
            for bq in result.get("questions", []):
                if not (bq.get("student_answer") or "").strip() and bq.get("assigned_marks") in (None, 0):
                    bq["explanation"] = "Blank answer."
            for bq in result.get("questions", []):
                max_m = bq.get("max_marks")
                if max_m is None:
                    continue
                m = bq.get("assigned_marks", 0)
                if not isinstance(m, int) or m < 0 or m > int(max_m):
                    warn(
                        f"Marking: Q{bq.get('number')} assigned_marks={m} out of range "
                        f"[0, {max_m}] — clamping"
                    )
                    try:
                        m_int = int(m)
                    except (TypeError, ValueError):
                        m_int = 0
                    bq["assigned_marks"] = max(0, min(m_int, int(max_m)))
            return result
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            _last_exc = exc
            if attempt == 0 and is_503_error(exc):
                warn("Marking error (503) — retrying in 0.1 s")
                time.sleep(0.1)
            else:
                break

    raise MarkingFailure(attempts=_actual_attempts, last_exc=_last_exc, last_raw=_last_raw)


def _fix_mc_marks(result: dict) -> None:
    """Normalise student_answer and recompute assigned_marks for MCQ questions in-place.

    The AI is not shown the correct answer for MCQs, so it cannot award marks
    reliably. This function overrides assigned_marks deterministically and
    normalises the extracted letter (e.g. "b." → "B").

    Keyed by question_text (not number) because duplicate question numbers
    (e.g. two Q38s on the same page) share the same stripped number after
    _2 is removed from blueprints.
    """
    mc_correct: dict[str, str] = {
        (q.get("question_text") or "").strip(): (q.get("correct_answer") or "").strip().upper()
        for q in result.get("questions", [])
        if q.get("question_type") == "multiple_choice" and q.get("correct_answer")
    }
    if not mc_correct:
        return
    for q in result.get("questions", []):
        qt = (q.get("question_text") or "").strip()
        if qt not in mc_correct:
            continue
        raw_ans = (q.get("student_answer") or "").strip()
        student_ans = raw_ans[0].upper() if raw_ans and raw_ans[0].isalpha() else "?"
        q["student_answer"] = student_ans
        max_m = int(q.get("max_marks") or 1)
        correct = student_ans == mc_correct[qt]
        q["assigned_marks"] = max_m if correct else 0
        q["explanation"] = "Correct." if correct else "Incorrect."

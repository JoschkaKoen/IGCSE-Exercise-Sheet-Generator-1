"""Step 14 — AI marking: iterate over student scan pages and fill blueprint JSONs.

Uses the MARKING_MODEL env var (default: qwen3.6-plus, off) via make_ai_client().
Requires DASHSCOPE_API_KEY to be set in .env.

Students are processed in parallel (MARKING_WORKERS workers, default 4).
Each worker opens its own fitz document handle (fitz is not thread-safe).
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections.abc import Callable
from typing import Any

import xml.etree.ElementTree as ET

from eXercise.ai_client import collect_streamed_response
from xscore.config import MAX_RETRIES
from xscore.marking.blueprints import marked_to_md
from xscore.shared.exam_paths import artifact_blueprint_xml_path, artifact_marked_failed_path, artifact_marked_json_path, artifact_marked_md_path, artifact_prompt_path
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import format_duration, get_console, icon, info_line, warn_line


_DEFAULT_MARKING_MODEL = "qwen3.6-plus, low"


class MarkingFailure(Exception):
    """Raised when all retry attempts to mark a page are exhausted."""
    def __init__(self, *, attempts: int, last_exc: BaseException, last_raw: str = "") -> None:
        super().__init__(f"All {attempts} marking attempts failed: {last_exc}")
        self.attempts = attempts
        self.last_exc = last_exc
        self.last_raw = last_raw


def _repair_mismatched_leaf_tags(raw: str) -> str:
    """Fix the observed model error: leaf element closed with the wrong sibling tag.

    e.g. <explanation>long text</student_answer> → <explanation>long text</explanation>
    Applied per <question> block to avoid cross-question interference.
    """
    _LEAF = ('student_answer', 'assigned_marks', 'explanation')

    def _fix_within_question(q_text: str) -> str:
        for tag in _LEAF:
            for wrong in _LEAF:
                if wrong == tag:
                    continue
                q_text = re.sub(
                    r'(<' + tag + r'(?:\s[^>]*)?>)(.*?)</' + wrong + r'>',
                    r'\1\2</' + tag + r'>',
                    q_text,
                    flags=re.DOTALL,
                )
        return q_text

    return re.sub(
        r'(<question\b[^>]*>)(.*?)(</question>)',
        lambda m: m.group(1) + _fix_within_question(m.group(2)) + m.group(3),
        raw,
        flags=re.DOTALL,
    )


def _parse_xml_response(raw: str) -> list[dict]:
    """Parse the AI's XML marking response into a list of question dicts."""
    raw = raw.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[^\n]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw.strip())
    # Extract the <marking>…</marking> block, discarding any surrounding
    # reasoning text or stray duplicate </marking> tags the model may emit.
    m = re.search(r'(<marking\b.*?</marking>)', raw, re.DOTALL)
    if m:
        raw = m.group(1)
    # Replace HTML <br> variants with a space (not valid XML void elements)
    raw = re.sub(r'<br\s*/?>', ' ', raw, flags=re.IGNORECASE)
    # Fix unescaped & in element text (e.g. student wrote "P & Q")
    raw = re.sub(r'&(?![a-zA-Z#]\w*;)', '&amp;', raw)
    # Fix bare < in text content (e.g. "x < y", "< 50%") — leave valid tag starts intact
    raw = re.sub(r'<(?!/?[a-zA-Z_:!?])', '&lt;', raw)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raw = _repair_mismatched_leaf_tags(raw)
        root = ET.fromstring(raw)  # raises ET.ParseError if still malformed
    questions = []
    for q in root.findall('question'):
        sa_el = q.find('student_answer')
        am_el = q.find('assigned_marks')
        re_el = q.find('explanation')
        # assigned_marks: prefer child element (new format), fall back to attribute (legacy)
        if am_el is not None and (am_el.text or '').strip():
            try:
                assigned_marks = int(am_el.text.strip())
            except ValueError:
                assigned_marks = 0
        else:
            assigned_marks = int(q.get('assigned_marks', 0))
        questions.append({
            'number':         q.get('number', ''),
            'subpage_row':    int(q.get('subpage_row', 1)),
            'subpage_col':    int(q.get('subpage_col', 1)),
            'assigned_marks': assigned_marks,
            'student_answer': (sa_el.text or '').strip() if sa_el is not None else '',
            'explanation':    (re_el.text or '').strip() if re_el is not None else '',
            'question_text':  q.get('question_text', ''),
        })
    return questions


def _blueprint_xml_to_dict(xml_str: str) -> dict:
    """Parse <marking page rows cols> XML into the blueprint dict format."""
    root = ET.fromstring(xml_str)
    questions = []
    for qel in root.findall("question"):
        text_el = qel.find("text")
        answer_options = [
            {"letter": o.get("letter", ""), "text": (o.text or "").strip()}
            for o in qel.findall("option")
        ]
        mark_scheme = [
            {"mark": c.get("mark", ""), "criterion": (c.text or "").strip()}
            for c in qel.findall("criterion")
            if (c.text or "").strip()
        ]
        sa_el = qel.find("student_answer")
        am_el = qel.find("assigned_marks")
        ex_el = qel.find("explanation")
        questions.append({
            "number":          qel.get("number", ""),
            "question_type":   qel.get("type", "short_answer"),
            "subpage_row":     int(qel.get("subpage_row", 1)),
            "subpage_col":     int(qel.get("subpage_col", 1)),
            "order_in_subpage": int(qel.get("order_in_subpage", 1)),
            "question_text":   (text_el.text or "").strip() if text_el is not None else "",
            "answer_options":  answer_options,
            "correct_answer":  qel.get("correct_answer") or None,
            "max_marks":       int(qel.get("max_marks", 0)),
            "mark_scheme":     mark_scheme,
            "student_answer":  (sa_el.text or "").strip() if sa_el is not None else "",
            "assigned_marks":  None if am_el is None or not (am_el.text or "").strip() else int(am_el.text),
            "explanation":     (ex_el.text or "").strip() if ex_el is not None else "",
        })
    return {
        "page":     int(root.get("page", 1)),
        "layout":   {"rows": int(root.get("rows", 1)), "cols": int(root.get("cols", 1))},
        "questions": questions,
    }


def _render_page_b64(doc: Any, page_idx: int, dpi: int = 150) -> str:
    """Render a fitz Document page at *page_idx* as base64 JPEG.

    The document must be already open; the caller owns its lifecycle.
    """
    import numpy as np
    from PIL import Image

    import fitz
    from xscore.extraction.images import to_jpeg_bytes

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = doc[page_idx].get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    img = Image.fromarray(
        np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    )
    return base64.b64encode(to_jpeg_bytes(img, quality=90)).decode()



def _quadrant_label(row: int, col: int, total_rows: int, total_cols: int) -> str:
    v = "top" if row == 1 else "bottom" if row == total_rows else f"row {row}"
    h = "left" if col == 1 else "right" if col == total_cols else f"col {col}"
    return f"{v}-{h}"


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
) -> dict:
    """Vision call to fill in a marking blueprint for one scan page.

    Raises :class:`MarkingFailure` if all attempts are exhausted.
    """
    layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
    rows, cols = int(layout.get("rows", 1)), int(layout.get("cols", 1))

    # --- Section A: role + task ---
    system_prompt = (
        "You are an expert exam marker. You will be shown one page of a student's exam paper "
        "and a Blueprint XML listing every question. The blueprint is a form: each question has "
        "three empty fields for you to fill in — <student_answer>, <assigned_marks>, and "
        "<explanation>. Fill every field for every question in the list."
    )

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
        "2. assigned_marks — an integer 0–max_marks.\n"
        "   • Award 1 mark for each criterion the student satisfies, up to max_marks.\n"
        "   • For 'any N from' lists, each listed item is a separate mark point.\n"
        "   • If <criterion> elements are absent or empty, use the correct_answer field "
        "and good judgement to assess the student's answer; accept semantically equivalent "
        "answers, not only verbatim matches.\n"
        "   • For multiple_choice: compare student_answer to correct_answer; "
        "award max_marks if they match, 0 otherwise.\n"
        "3. explanation — 1–2 sentences. State what the student wrote, whether it is correct, "
        "and the mark outcome. Write a finished verdict, not a thought process: "
        "no deliberation, no working-out, no self-corrections.\n"
        "   Examples: 'Student correctly identified Newton's third law — 1 mark.' "
        "/ 'Student wrote F=ma but omitted units — 1 of 2 marks.' "
        "/ 'Student selected B; correct answer is C — 0 marks.'"
    )

    # --- Section C: output format + CRITICAL tag rule ---
    system_prompt += (
        "\n\nReturn ONLY the filled Blueprint XML — no markdown fences, no surrounding text. "
        "Fill in the three empty XML fields in each <question>: "
        "<student_answer>, <assigned_marks>, and <explanation>. "
        "Do not change any other content.\n"
        "CRITICAL — write each field exactly once, final answer only. "
        "Do NOT reason, deliberate, or self-correct inside any XML field or anywhere in the output. "
        "Decide first, then write the answer directly into the field and close the tag immediately.\n"
        "CRITICAL — each element must be closed with its own matching tag. "
        "WRONG: <explanation>text</student_answer>. "
        "RIGHT: <explanation>text</explanation>. "
        "Never close <explanation> with </student_answer> or vice versa."
    )

    # --- Section D: XML validity + LaTeX ---
    system_prompt += (
        "\n\nXML validity:\n"
        "• In element text use &lt; for <, &gt; for >, &amp; for &.\n"
        "• Do not use HTML tags (e.g. <br>) — use a space or comma instead.\n"
        "• LaTeX: wrap all math in $...$  "
        "(e.g. $v = 2\\pi r / T$, $3.0 \\times 10^4$ m/s, $\\frac{d}{v}$). "
        "Use \\times, \\approx, \\frac{}{}, \\pi, \\rightarrow, \\% etc. "
        "Failing to wrap math in $...$ will crash the PDF renderer."
    )

    # --- Section E: grid navigation (only for multi-subpage layouts) ---
    if rows > 1 or cols > 1:
        system_prompt += (
            f"\n\nThis page is divided into a {rows}×{cols} grid — "
            "the <subpage> elements at the top of the blueprint label each quadrant. "
            "Each question's subpage_row and subpage_col identify its quadrant; "
            "do not confuse answers from different quadrants. "
            "order_in_subpage (1 = topmost) gives the vertical position within a quadrant. "
            "The same question number may appear more than once — always identify questions "
            "by subpage_row + subpage_col + question text, not by number alone."
        )

    user_text = (
        "Fill in the three empty fields for each question "
        "(<student_answer>, <assigned_marks>, <explanation>):\n"
        f"{blueprint_xml}"
    )
    kwargs: dict[str, Any] = dict(
        model=model_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
    )
    kwargs.update(thinking_kw)

    save_prompt(prompt_save_path, model=model_id, messages=kwargs["messages"])

    _last_exc: BaseException = RuntimeError("no attempts made")
    _last_raw: str = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            if use_stream:
                stream = client.chat.completions.create(**kwargs, stream=True)
                raw = collect_streamed_response(stream)
            else:
                resp = client.chat.completions.create(**kwargs)
                raw = resp.choices[0].message.content or ""
            _last_raw = raw
            try:
                parsed_questions = _parse_xml_response(raw)
            except ET.ParseError as exc:
                warn("Marking XML parse error — XML repair failed, marking aborted")
                _last_exc = exc
                break
            result = blueprint.copy()
            # Group AI responses by (bare_number, subpage_row, subpage_col).
            # The _N suffix is stripped so Q38 and Q38_2 share the same group;
            # blueprint questions consume positionally so Q38 gets group[0] and
            # Q38_2 gets group[1], matching the order the AI echoes them back.
            def _bq_key(bq: dict) -> tuple:
                _row = bq.get("subpage_row")
                _col = bq.get("subpage_col")
                num = re.sub(r'_\d+$', '', str(bq.get("number", "")))
                return (
                    num,
                    int(_row) if _row is not None else 1,
                    int(_col) if _col is not None else 1,
                )

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
            _unmatched_count = sum(
                max(0, len(grp) - fill_group_idx.get(key, 0))
                for key, grp in fill_groups.items()
            )
            if _unmatched_count:
                warn(f"Marking: {_unmatched_count} AI entries had no matching blueprint question")
            _fix_mc_marks(result)
            for bq in result.get("questions", []):
                if not (bq.get("student_answer") or "").strip():
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
                    bq["assigned_marks"] = max(0, min(int(m) if isinstance(m, (int, float)) else 0, int(max_m)))
            return result
        except Exception as exc:  # noqa: BLE001
            warn(f"Marking error — retrying ({attempt + 1}/{MAX_RETRIES + 1})")
            _last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 ** (attempt + 1))

    raise MarkingFailure(attempts=MAX_RETRIES + 1, last_exc=_last_exc, last_raw=_last_raw)


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



def run_ai_marking(ctx: Any, *, dpi: int | None = None) -> list[dict]:
    """Run the full AI marking loop for all students and pages.

    Reads page assignments from ``8_exam_student_list.json`` (written by step 8)
    so each student's scan pages are determined by name detection, not position.
    Students are processed in parallel (MARKING_WORKERS env var, default varies with cpu_count).
    *dpi* defaults to ``MARKING_DPI`` when not supplied.
    Returns a list of API call timing records for step 15.
    """
    from xscore.config import MARKING_DPI
    if dpi is None:
        dpi = MARKING_DPI

    import fitz

    from eXercise.ai_client import make_ai_client, build_thinking_kwargs
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    result = make_ai_client(model_env="MARKING_MODEL", default_model=_DEFAULT_MARKING_MODEL)
    if result is None:
        raise RuntimeError(
            "MARKING_MODEL client could not be created — check DASHSCOPE_API_KEY in .env"
        )
    client, model_id, _provider, _effort = result
    _use_stream, _thinking_kw = build_thinking_kwargs(_provider, _effort)

    # Load page assignments produced by step 8 name detection.
    list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    if not list_path.exists():
        raise FileNotFoundError(
            f"8_exam_student_list.json not found at {list_path} — run step 8 first"
        )
    raw_assignments: list[dict] = json.loads(list_path.read_text(encoding="utf-8"))
    # Each entry: {"student_name": str, "page_numbers": [int, ...], "confidence": str}

    _instr = getattr(ctx, "instruction", None)
    if _instr is not None:
        sf = _instr.student_filter
        if sf.mode == "specific" and sf.names:
            raw_assignments = [a for a in raw_assignments if a["student_name"] in sf.names]
        elif sf.mode == "first_n" and sf.n:
            raw_assignments = raw_assignments[: sf.n]

    workers = int(os.environ.get("MARKING_WORKERS", str(min(os.cpu_count() or 4, 16))))
    timings_lock = threading.Lock()
    api_call_timings: list[dict] = []

    # Pre-render all pages to b64 before spawning API workers so every worker
    # has its image ready and all API calls fire within milliseconds of each other.
    _total_pages = sum(len(a["page_numbers"]) for a in raw_assignments)
    info_line(f"Rendering {_total_pages} page(s) for {len(raw_assignments)} students at {dpi} DPI …")
    _b64_cache: dict[tuple[str, int], str] = {}
    _pre_doc = fitz.open(str(ctx.cleaned_pdf))
    try:
        for _a in raw_assignments:
            for _p_label, _scan_page in enumerate(_a["page_numbers"], 1):
                _b64_cache[(_a["student_name"], _p_label)] = _render_page_b64(
                    _pre_doc, _scan_page - 1, dpi=dpi
                )
    finally:
        _pre_doc.close()

    import contextlib
    import sys
    from rich.live import Live

    _use_live = sys.stdout.isatty() and not hasattr(sys.stdout, '_log')
    _display_lock = threading.Lock()
    _student_lines: dict[str, str] = {}

    def _render() -> str:  # caller must hold _display_lock
        return "\n".join(_student_lines.values()) if _student_lines else ""

    def _mark_student(assignment: dict) -> tuple[list[dict], list[dict]]:
        """Mark one student's pages using pre-rendered b64 images.

        Returns (timings, failures) where failures is non-empty if any page exhausted all retries.
        """
        student_name: str = assignment["student_name"]
        page_numbers: list[int] = assignment["page_numbers"]  # 1-based scan pages
        # cover_page_number is set when step 8 detected a cover page for this student.
        # The cover page is always p_label=1 (first page of the block); skip it entirely —
        # no AI call, no output file written (neither success nor failure path).
        has_cover: bool = assignment.get("cover_page_number") is not None
        answer_page_count = len(page_numbers) - (1 if has_cover else 0)
        local_timings: list[dict] = []
        local_failures: list[dict] = []
        for p_label, _ in enumerate(page_numbers, 1):
            if has_cover and p_label == 1:
                continue  # skip cover page — not an answer page
            answer_label = p_label - (1 if has_cover else 0)  # 1-based answer page index
            key = f"{student_name}_{p_label}"
            with _display_lock:
                _student_lines[key] = f"[dim]  {icon('info')}  Student '{student_name}' · page {answer_label}/{answer_page_count}[/]"
                if _use_live:
                    live.update(_render())

            b64 = _b64_cache[(student_name, p_label)]
            blueprint_xml = artifact_blueprint_xml_path(ctx.artifact_dir, p_label).read_text(
                encoding="utf-8"
            )
            blueprint = _blueprint_xml_to_dict(blueprint_xml)
            safe_name = student_name or f"Unknown_{p_label}"

            t0 = time.perf_counter()
            try:
                filled = _mark_page(
                    client, model_id, b64, blueprint, _thinking_kw,
                    blueprint_xml=blueprint_xml,
                    use_stream=_use_stream,
                    prompt_save_path=artifact_prompt_path(
                        ctx.artifact_dir, f"14_marked_{safe_name}_{p_label}"
                    ),
                    warn=_warn,
                )
            except MarkingFailure as mf:
                filled = blueprint.copy()
                filled["student_name"] = student_name
                local_failures.append({
                    "student": student_name,
                    "page": p_label,
                    "attempts": mf.attempts,
                    "error": str(mf.last_exc),
                    "raw_response": mf.last_raw or None,
                })
                out_json = artifact_marked_json_path(ctx.artifact_dir, safe_name, p_label)
                out_json.parent.mkdir(parents=True, exist_ok=True)
                out_json.write_text(
                    json.dumps(filled, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
                    marked_to_md(filled), encoding="utf-8"
                )
                failed_path = artifact_marked_failed_path(ctx.artifact_dir, safe_name, p_label)
                failed_path.parent.mkdir(parents=True, exist_ok=True)
                failed_path.write_text(
                    json.dumps(local_failures[-1], indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                with _display_lock:
                    _student_lines[key] = (
                        f"[red]  {icon('warn')}  Student '{student_name}' · page {answer_label}/{answer_page_count}"
                        f"  ·  FAILED[/]"
                    )
                    if _use_live:
                        live.update(_render())
                    else:
                        get_console().print(_student_lines[key])
                continue

            mark_dur = round(time.perf_counter() - t0, 2)
            with _display_lock:
                _student_lines[key] = (
                    f"[dim]  {icon('info')}  Student '{student_name}' · page {answer_label}/{answer_page_count}"
                    f"  ·  {format_duration(mark_dur)}[/]"
                )
                if _use_live:
                    live.update(_render())
                else:
                    get_console().print(_student_lines[key])
            local_timings.append({
                "phase": "marking",
                "student": student_name,
                "page": p_label,
                "duration_s": mark_dur,
            })

            filled["student_name"] = student_name
            out_json = artifact_marked_json_path(ctx.artifact_dir, safe_name, p_label)
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(
                json.dumps(filled, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
                marked_to_md(filled), encoding="utf-8"
            )
        return local_timings, local_failures

    all_failures: list[dict] = []
    _live_ctx = Live("", console=get_console(), refresh_per_second=4) if _use_live else contextlib.nullcontext()
    with _live_ctx as live:
        def _warn(msg: str) -> None:
            if _use_live:
                with _display_lock:
                    live.console.print(f"[yellow]  {icon('warn')}  {msg}[/]")
            else:
                warn_line(msg)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_mark_student, a): a["student_name"] for a in raw_assignments
            }
            for fut in as_completed(futures):
                timings, failures = fut.result()
                with timings_lock:
                    api_call_timings.extend(timings)
                    all_failures.extend(failures)

    ctx.marking_failures = all_failures
    return api_call_timings

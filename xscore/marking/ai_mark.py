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
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS, MARKING_JPEG_QUALITY, MAX_RETRIES
from xscore.marking.blueprints import marked_to_md
from xscore.shared.exam_paths import artifact_blueprint_xml_path, artifact_marked_failed_path, artifact_marked_md_path, artifact_marked_xml_path, artifact_prompt_path
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import format_duration, get_console, icon, info_line, ok_line, warn_line


_DEFAULT_MARKING_MODEL = "qwen3.6-plus, low"


def filled_to_xml(filled: dict) -> str:
    """Serialise a filled marking blueprint dict to Blueprint XML.

    Using the in-memory ``filled`` dict (rather than the raw AI response)
    guarantees every question is present even if the model omitted some.
    LaTeX content is stored verbatim as element text — no JSON escaping layer.
    """
    layout = filled.get("layout") or {}
    root = ET.Element("marking")
    root.set("page", str(filled.get("page", "")))
    root.set("rows", str(layout.get("rows", 1)))
    root.set("cols", str(layout.get("cols", 1)))
    root.set("student_name", str(filled.get("student_name") or ""))

    for q in filled.get("questions") or []:
        qel = ET.SubElement(root, "question")
        qel.set("number", str(q.get("number", "")))
        qel.set("type", str(q.get("question_type", "")))
        qel.set("subpage_row", str(q.get("subpage_row", 1)))
        qel.set("subpage_col", str(q.get("subpage_col", 1)))
        qel.set("order_in_subpage", str(q.get("order_in_subpage", 1)))
        qel.set("max_marks", str(q.get("max_marks", 0)))
        qel.set("correct_answer", str(q.get("correct_answer") or ""))

        text_el = ET.SubElement(qel, "text")
        text_el.text = str(q.get("question_text") or "")

        for crit in q.get("mark_scheme") or []:
            crit_el = ET.SubElement(qel, "criterion")
            crit_el.set("mark", str(crit.get("mark") or ""))
            crit_el.text = str(crit.get("criterion") or "")

        for opt in q.get("answer_options") or []:
            opt_el = ET.SubElement(qel, "option")
            opt_el.set("letter", str(opt.get("letter") or ""))
            opt_el.text = str(opt.get("text") or "")

        sa_el = ET.SubElement(qel, "student_answer")
        sa_el.text = str(q.get("student_answer") or "")

        am_el = ET.SubElement(qel, "assigned_marks")
        am_val = q.get("assigned_marks")
        am_el.text = str(am_val) if am_val is not None else ""

        exp_el = ET.SubElement(qel, "explanation")
        exp_el.text = str(q.get("explanation") or "")

    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


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


def _render_page_b64(doc: Any, page_idx: int, dpi: int = 300) -> str:
    """Render a fitz Document page at *page_idx* as base64 JPEG.

    The document must be already open; the caller owns its lifecycle.
    Default DPI matches MARKING_DPI (300); override via the dpi parameter.
    """
    import fitz
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = doc[page_idx].get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return base64.b64encode(pix.tobytes("jpeg", jpg_quality=MARKING_JPEG_QUALITY)).decode()



def _quadrant_label(row: int, col: int, total_rows: int, total_cols: int) -> str:
    v = "top" if row == 1 else "bottom" if row == total_rows else f"row {row}"
    h = "left" if col == 1 else "right" if col == total_cols else f"col {col}"
    return f"{v}-{h}"


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
        "   The output is placed verbatim in a LaTeX document. "
        "Escape characters that appear literally in the student's answer: "
        "% → \\%, $ → \\$, # → \\#, _ → \\_, { → \\{, } → \\}, "
        "backslash → \\textbackslash{}, "
        "literal ampersand → \\&amp; (\\& for LaTeX + &amp; for XML, combined). "
        "Use \\newline for line breaks; do not include literal newlines.\n"
        "2. assigned_marks — an integer 0–max_marks.\n"
        "   • Award 1 mark for each criterion the student satisfies, up to max_marks.\n"
        "   • For 'any N from' lists, each listed item is a separate mark point.\n"
        "   • If <criterion> elements are absent or empty, use the correct_answer field "
        "and good judgement to assess the student's answer; accept semantically equivalent "
        "answers, not only verbatim matches.\n"
        "   • For multiple_choice: compare student_answer to correct_answer; "
        "award max_marks if they match, 0 otherwise.\n"
        "3. explanation: clear, easy to understand, short, simple english. Avoid difficult English words "
        "(non native, high school english speakers). "
        "Address the student directly using 'you'. "
        "You can make important words bold using LaTeX syntax \\textbf{word}: only for important words. "
        "Escape non-math special characters that appear literally in your prose: "
        "% → \\%, _ → \\_, literal ampersand → \\&amp;. "
        "Use \\newline for line breaks. "
        "Do not append a mark tally (e.g. '— 1 mark.') at the end."
    )

    # --- Section C: output format + CRITICAL tag rule ---
    system_prompt += (
        "\n\nReturn ONLY the filled Blueprint XML — no markdown fences, no surrounding text. "
        "Fill in the three empty XML fields in each <question>: "
        "<student_answer>, <assigned_marks>, and <explanation>. "
        "Do not change any other content.\n"
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
        "Failing to wrap math in $...$ will crash the PDF renderer.\n"
        "• Do not append a mark tally ('— X marks.') at the end of any field."
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

    user_text = (
        "Fill in the three empty fields for each question "
        "(<student_answer>, <assigned_marks>, <explanation>):\n"
        f"{blueprint_xml}"
    )
    _user_content: list[dict] = [
        {"type": "text", "text": user_text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
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
        except KeyboardInterrupt:
            raise
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



def render_pages_b64(
    cleaned_pdf: Path,
    artifact_dir: Path,
    dpi: int,
    workers: int,
    *,
    instruction: Any = None,
) -> dict[tuple[str, int], str]:
    """Render all scan pages to base64 JPEG, parallelised.

    Reads 8_exam_student_list.json directly (same source as run_ai_marking).
    Each worker opens its own fitz.Document — fitz is not thread-safe.
    Returns {(student_name, page_label): b64_str}.
    """
    import fitz
    from concurrent.futures import as_completed
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    list_path = artifact_exam_student_list_json_path(artifact_dir)
    raw: list[dict] = json.loads(list_path.read_text(encoding="utf-8"))

    if instruction is not None:
        sf = instruction.student_filter
        if sf.mode == "specific" and sf.names:
            raw = [a for a in raw if a["student_name"] in sf.names]
        elif sf.mode == "first_n" and sf.n:
            raw = raw[: sf.n]

    tasks: list[tuple[str, int, int]] = []
    for a in raw:
        for p_label, scan_page in enumerate(a["page_numbers"], 1):
            tasks.append((a["student_name"], p_label, scan_page - 1))

    cache: dict[tuple[str, int], str] = {}
    if not tasks:
        return cache

    def _render_one(student: str, p_label: int, page_0idx: int) -> tuple[tuple[str, int], str]:
        doc = fitz.open(str(cleaned_pdf))
        try:
            b64 = _render_page_b64(doc, page_0idx, dpi=dpi)
        finally:
            doc.close()
        return (student, p_label), b64

    n_workers = min(len(tasks), workers)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = {pool.submit(_render_one, s, pl, p0): None for s, pl, p0 in tasks}
        for fut in as_completed(futs):
            key, b64 = fut.result()
            cache[key] = b64

    return cache


def _mark_page_pdf(
    pdf_path: str,
    blueprint: dict,
    blueprint_xml: str,
    prompt_save_path: Path | None,
    warn: Callable[[str], None],
) -> dict:
    """Upload a pre-built multi-page PDF to Gemini and mark it.

    pdf_path is a temporary file built by the caller (exercise page + blank continuation pages).
    Raises MarkingFailure if all retries are exhausted.
    """
    import os
    from google import genai as gai
    from google.genai import types as gai_types
    from xscore.shared.prompt_logger import save_response
    from eXercise.ai_client import parse_model_effort

    _api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not _api_key:
        raise RuntimeError("GEMINI_API_KEY not set — cannot upload multi-page PDF for blank continuation pages")

    _model_env = os.environ.get("MARKING_MODEL", "")
    model_id, _ = parse_model_effort(_model_env) if _model_env else ("gemini-2.5-flash", None)
    gai_client = gai.Client(api_key=_api_key)

    system_prompt = (
        "You are marking a student's exam answer. The uploaded PDF contains the exercise page "
        "followed by one or more continuation pages the student used for additional writing. "
        "Mark all pages together as one answer."
    )
    user_text = (
        "Fill in the three empty fields for each question "
        "(<student_answer>, <assigned_marks>, <explanation>):\n"
        f"{blueprint_xml}"
    )
    save_prompt(prompt_save_path, model=model_id, messages=[{"role": "user", "content": user_text}])

    _last_exc: BaseException = RuntimeError("no attempts made")
    _last_raw: str = ""
    uploaded = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            if uploaded is None:
                uploaded = gai_client.files.upload(
                    file=pdf_path,
                    config=gai_types.UploadFileConfig(mime_type="application/pdf"),
                )
            resp = gai_client.models.generate_content(
                model=model_id,
                contents=[
                    gai_types.Part.from_uri(file_uri=uploaded.uri, mime_type="application/pdf"),
                    gai_types.Part.from_text(text=user_text),
                ],
                config=gai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
                ),
            )
            raw = resp.text or ""
            _last_raw = raw
            save_response(prompt_save_path, raw)
            parsed_questions = _parse_xml_response(raw)
            result = blueprint.copy()
            fill_groups: dict[tuple, list] = defaultdict(list)
            for q in parsed_questions:
                fill_groups[_bq_key(q)].append(q)
            fill_group_idx: dict[tuple, int] = defaultdict(int)
            for bq in result.get("questions", []):
                grp_key = _bq_key(bq)
                idx = fill_group_idx[grp_key]
                fill_group_idx[grp_key] += 1
                if fill_groups[grp_key] and idx < len(fill_groups[grp_key]):
                    src_q = fill_groups[grp_key][idx]
                    bq["student_answer"] = src_q.get("student_answer", "")
                    bq["assigned_marks"] = src_q.get("assigned_marks", 0)
                    bq["explanation"] = src_q.get("explanation", "")
            try:
                gai_client.files.delete(name=uploaded.name)
            except Exception:
                pass
            return result
        except ET.ParseError as exc:
            warn("Marking XML parse error (PDF upload path) — XML repair failed, marking aborted")
            _last_exc = exc
            break
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    if uploaded is not None:
        try:
            gai_client.files.delete(name=uploaded.name)
        except Exception:
            pass
    raise MarkingFailure(
        attempts=MAX_RETRIES + 1, last_exc=_last_exc, last_raw=_last_raw
    )


def _scheme_graphics_for_page(
    blueprint: dict,
    graphics_map: dict[str, list[Path]],
) -> list[tuple[str, int, str]]:
    """Return (question_number, ms_page, base64_png) tuples for mark-scheme graphics on this page."""
    out = []
    for q in blueprint.get("questions", []):
        qnum = str(q.get("number", ""))
        safe_num = re.sub(r"[^\w]", "_", qnum)
        for png_path in graphics_map.get(safe_num, []):
            page_prefix = png_path.name.split("_")[0]
            ms_page = int(page_prefix) if page_prefix.isdigit() else 0
            out.append((qnum, ms_page, base64.b64encode(png_path.read_bytes()).decode()))
    return out


def run_ai_marking(ctx: Any, *, dpi: int | None = None) -> list[dict]:
    """Run the full AI marking loop for all students and pages.

    Reads page assignments from ``8_exam_student_list.json`` (written by step 8)
    so each student's scan pages are determined by name detection, not position.
    Pages are processed in parallel (MARKING_WORKERS env var, default varies with cpu_count).
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

    # Load blank page detection results (written by step 8 blank_page_detection).
    _blank_json = ctx.artifact_dir / "8_blank_pages.json"
    # Keys: student_name → set of scan_pages to skip (blank, no handwriting)
    _skip_scan_pages_by_student: dict[str, set[int]] = {}
    # Keys: student_name → {exercise_scan_page → [extra_blank_scan_pages_with_handwriting]}
    _extra_by_student: dict[str, dict[int, list[int]]] = {}
    if _blank_json.exists():
        _bdata = json.loads(_blank_json.read_text(encoding="utf-8"))
        for _s in _bdata.get("students", []):
            _skip: set[int] = set()
            _extras: dict[int, list[int]] = {}
            for _bp in _s["blank_scan_pages"]:
                if not _bp["has_handwriting"]:
                    _skip.add(_bp["scan_page"])
                elif _bp.get("attach_to_scan_page") is not None:
                    _extras.setdefault(_bp["attach_to_scan_page"], []).append(_bp["scan_page"])
            _skip_scan_pages_by_student[_s["student_name"]] = _skip
            _extra_by_student[_s["student_name"]] = _extras

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

    b64_future = getattr(ctx, "b64_future", None)
    if b64_future is not None:
        _b64_cache = b64_future.result()   # instant if BG finished; brief wait if not
        ok_line(f"Pre-rendering done  ·  {len(_b64_cache)} page(s) ready")
    else:
        _total_pages = sum(len(a["page_numbers"]) for a in raw_assignments)
        info_line(f"Rendering {_total_pages} page(s) for {len(raw_assignments)} students at {dpi} DPI …")
        _b64_cache = render_pages_b64(
            ctx.cleaned_pdf, ctx.artifact_dir, dpi, workers,
            instruction=getattr(ctx, "instruction", None),
        )

    # Pre-build mark-scheme graphics map: safe_qnum → sorted list of PNG paths
    _graphics_dir = ctx.artifact_dir / "11_mark_scheme_graphics"
    _graphics_map: dict[str, list[Path]] = {}
    if _graphics_dir.is_dir():
        _gfx_re = re.compile(r"^\d+_(.+)_(\d+)\.png$")
        for _p in sorted(_graphics_dir.glob("*.png")):
            _m = _gfx_re.match(_p.name)
            if _m:
                _graphics_map.setdefault(_m.group(1), []).append(_p)
        for _v in _graphics_map.values():
            _v.sort()

    # Build flat per-page task list — cover pages, out-of-range pages, and blank exam pages
    # without handwriting are excluded here.
    page_tasks: list[tuple[dict, int, int, int, list[int]]] = []
    for a in raw_assignments:
        has_cover = a.get("cover_page_number") is not None
        answer_page_count = len(a["page_numbers"]) - (1 if has_cover else 0)
        student_skip = _skip_scan_pages_by_student.get(a["student_name"], set())
        student_extras = _extra_by_student.get(a["student_name"], {})
        for p_label, _ in enumerate(a["page_numbers"], 1):
            if has_cover and p_label == 1:
                continue
            _cover_offset = 1 if (has_cover and not ctx.empty_exam_has_cover) else 0
            answer_label = p_label - _cover_offset
            scan_page = a["page_numbers"][p_label - 1]
            if ctx.scaffold is not None and answer_label > ctx.scaffold.page_count:
                continue
            if scan_page in student_skip:
                continue
            extra_scan_pages = student_extras.get(scan_page, [])
            page_tasks.append((a, p_label, answer_label, answer_page_count, extra_scan_pages))

    import contextlib
    import sys
    from rich.live import Live

    _use_live = sys.stdout.isatty() and not hasattr(sys.stdout, '_log')
    _display_lock = threading.Lock()
    _student_lines: dict[str, str] = {}

    def _render() -> str:  # caller must hold _display_lock
        return "\n".join(_student_lines.values()) if _student_lines else ""

    def _mark_one_page(
        assignment: dict, p_label: int, answer_label: int, answer_page_count: int,
        extra_scan_pages: list[int],
    ) -> tuple[dict | None, dict | None]:
        student_name: str = assignment["student_name"]
        safe_name = student_name or f"Unknown_{p_label}"
        key = f"{student_name}_{p_label}"

        _total_pages = len(assignment["page_numbers"])
        with _display_lock:
            _student_lines[key] = (
                f"[dim]  {icon('info')}  Student '{student_name}'"
                f" · page {p_label}/{_total_pages}[/]"
            )
            if _use_live:
                live.update(_render())

        blueprint_xml = artifact_blueprint_xml_path(ctx.artifact_dir, answer_label).read_text(
            encoding="utf-8"
        )
        blueprint = _blueprint_xml_to_dict(blueprint_xml)

        t0 = time.perf_counter()
        prompt_save = artifact_prompt_path(ctx.artifact_dir, f"14_marked_{safe_name}_{p_label}")
        try:
            _page_graphics: list = []
            _use_pdf_path = extra_scan_pages and (
                os.environ.get("GEMINI_API_KEY", "").strip()
                or os.environ.get("GOOGLE_API_KEY", "").strip()
            )
            if extra_scan_pages and not _use_pdf_path:
                _warn(
                    f"GEMINI_API_KEY not set — blank continuation pages for "
                    f"'{student_name}' page {p_label} will be omitted from marking"
                )
            if _use_pdf_path:
                import shutil
                import tempfile
                exercise_scan_page = assignment["page_numbers"][p_label - 1]
                all_scan_pages = [exercise_scan_page] + extra_scan_pages
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as _tmp:
                    tmp_path = _tmp.name
                try:
                    with fitz.open(str(ctx.cleaned_pdf)) as _src:
                        _out = fitz.open()
                        try:
                            for sp in all_scan_pages:
                                _out.insert_pdf(_src, from_page=sp - 1, to_page=sp - 1)
                            _out.save(tmp_path)
                        finally:
                            _out.close()
                    local_pdf = ctx.artifact_dir / f"14_upload_{safe_name}_{p_label}.pdf"
                    shutil.copy(tmp_path, local_pdf)
                    filled = _mark_page_pdf(
                        tmp_path, blueprint, blueprint_xml,
                        prompt_save_path=prompt_save,
                        warn=_warn,
                    )
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                b64 = _b64_cache[(student_name, p_label)]
                _page_graphics = _scheme_graphics_for_page(blueprint, _graphics_map)
                filled = _mark_page(
                    client, model_id, b64, blueprint, _thinking_kw,
                    blueprint_xml=blueprint_xml,
                    use_stream=_use_stream,
                    prompt_save_path=prompt_save,
                    warn=_warn,
                    scheme_graphics=_page_graphics,
                )
        except MarkingFailure as mf:
            filled = blueprint.copy()
            filled["student_name"] = student_name
            failure = {
                "student": student_name, "page": p_label,
                "attempts": mf.attempts, "error": str(mf.last_exc),
                "raw_response": mf.last_raw or None,
            }
            out_xml = artifact_marked_xml_path(ctx.artifact_dir, safe_name, p_label)
            out_xml.parent.mkdir(parents=True, exist_ok=True)
            out_xml.write_text(filled_to_xml(filled), encoding="utf-8")
            artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
                marked_to_md(filled), encoding="utf-8"
            )
            failed_path = artifact_marked_failed_path(ctx.artifact_dir, safe_name, p_label)
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            failed_path.write_text(json.dumps(failure, indent=2, ensure_ascii=False), encoding="utf-8")
            with _display_lock:
                _student_lines[key] = (
                    f"[red]  {icon('warn')}  Student '{student_name}'"
                    f" · page {p_label}/{_total_pages}  ·  FAILED[/]"
                )
                if _use_live:
                    live.update(_render())
                else:
                    get_console().print(_student_lines[key])
            return None, failure

        mark_dur = round(time.perf_counter() - t0, 2)
        if _page_graphics:
            _graphic_labels = [f"ms p{pg} Q{qn}" for qn, pg, _ in _page_graphics]
            _graphic_note = f"  · +graphic ({', '.join(_graphic_labels)})"
        else:
            _graphic_note = ""
        with _display_lock:
            _student_lines[key] = (
                f"[green]  {icon('ok')}  Student '{student_name}'"
                f" · page {p_label}/{_total_pages}  ·  {format_duration(mark_dur)}{_graphic_note}[/]"
            )
            if _use_live:
                live.update(_render())
            else:
                get_console().print(_student_lines[key])

        filled["student_name"] = student_name
        out_xml = artifact_marked_xml_path(ctx.artifact_dir, safe_name, p_label)
        out_xml.parent.mkdir(parents=True, exist_ok=True)
        out_xml.write_text(filled_to_xml(filled), encoding="utf-8")
        artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
            marked_to_md(filled), encoding="utf-8"
        )
        return {"phase": "marking", "student": student_name, "page": p_label,
                "duration_s": mark_dur}, None

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
                ex.submit(_mark_one_page, a, p_label, ans_lbl, ans_cnt, extras): (a["student_name"], p_label)
                for a, p_label, ans_lbl, ans_cnt, extras in page_tasks
            }
            for fut in as_completed(futures):
                timing, failure = fut.result()
                with timings_lock:
                    if timing:
                        api_call_timings.append(timing)
                    if failure:
                        all_failures.append(failure)

    ctx.marking_failures = all_failures
    return api_call_timings

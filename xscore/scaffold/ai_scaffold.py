"""AI-based exam scaffold extraction via Gemini.

Replaces the PyMuPDF heuristic parsing with two Gemini API calls:
  Call 1 — exam PDF  → question hierarchy (text, marks, page, subquestions, MC options)
  Call 2 — mark-scheme PDF (optional) → flat list of correct_answer + marking_criteria

Returns list[Question] with spatial BBox zeroed (page coord preserved) so the
overlay PDF generator produces a clean copy of the exam PDF with no annotations.
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pydantic import BaseModel

from xscore.shared.exam_paths import (
    artifact_exam_questions_raw_xml_path,
    artifact_exam_questions_xml_path,
    artifact_mark_scheme_xml_path,
    artifact_prompt_path,
)
from xscore.shared.models import BBox, ExamLayout, McAnswerOption, Question
from xscore.shared.prompt_logger import save_prompt


# ---------------------------------------------------------------------------
# Pydantic schema for layout detection (JSON — no LaTeX fields)
# ---------------------------------------------------------------------------

class _LayoutDetectSchema(BaseModel):
    rows: int = 1
    cols: int = 1
    reading_order: list[list[int]] = []
    # Each entry is [row, col] (1-based). Order = left-to-right reading sequence.
    # 4-up reading order: [[1,1],[1,2],[2,1],[2,2]]
    # 2-up landscape: [[1,1],[1,2]]
    # Empty list = fallback to row-major (left→right, top→bottom)


_LAYOUT_DETECT_JSON_SCHEMA: dict = _LayoutDetectSchema.model_json_schema()


# ---------------------------------------------------------------------------
# Model config — same pattern as load_student_list.py
# ---------------------------------------------------------------------------

def _parse_model(raw: str) -> tuple[str, str | None]:
    if "," in raw:
        model, effort = raw.split(",", 1)
        return model.strip(), effort.strip() or None
    return raw.strip(), None


def _exam_pdf_model_config() -> tuple[str, str | None]:
    return _parse_model(os.getenv("READ_EXAM_PDF_MODEL", os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash")))


def _mark_scheme_model_config() -> tuple[str, str | None]:
    return _parse_model(os.getenv("READ_MARK_SCHEME_MODEL", os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash")))


def _layout_detect_model_config() -> tuple[str, str | None]:
    return _parse_model(os.getenv("DETECT_LAYOUT_MODEL", "gemini-2.5-flash, low"))


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_EXAM = (
    "You are an expert at reading Cambridge IGCSE exam papers. "
    "Extract every question and sub-question as structured XML."
)

_USER_EXAM = """\
Return ONLY well-formed XML, no markdown fences or other text outside the XML.

First identify the page layout and set it as attributes on the root element:
  <exam rows="1 or 2" cols="1 or 2">

A standard single-page exam: rows="1" cols="1".
A 4-up exam (2×2 grid): rows="2" cols="2".

Then extract every question and sub-question at every nesting level as <question> elements.
Nested sub-questions are child <question> elements inside their parent.

Each <question> must have these attributes:
- number: the label as printed, run-together — "9", then "9a", then "9ai" (no parentheses or spaces)
- type: one of multiple_choice | short_answer | calculation | long_answer
- page: 1-based page number where this question first appears
- subpage_row: 1-based row of the quadrant (1 for 1x1 layout; 1=top, 2=bottom for 2x2)
- subpage_col: 1-based column of the quadrant (1 for 1x1 layout; 1=left, 2=right for 2x2)
- marks: integer mark allocation from [N] brackets; 0 if not printed

IMPORTANT — subpage assignment: assign based solely on where the question is
physically printed. The same question number can appear more than once in the same
quadrant; assign the quadrant each instance is physically in.

Each <question> must contain:
- <text>: complete question text in markdown; $...$ for inline math, $$...$$ for display math
- <option letter="A">text</option>: for multiple_choice only — one per answer option
- child <question> elements for any sub-questions

In XML text content use &lt; for <, &gt; for >, &amp; for &.
"""


def _build_user_exam_prompt(
    layout_result: "_LayoutDetectSchema | None",
    is_split: bool,
    n_split_pages: int,
) -> str:
    """Build the step-9 user prompt, injecting the layout known from step 8.

    Falls back to _USER_EXAM (which asks the AI to detect the layout) when
    layout_result is None, i.e. when split_subpages=False and step 8 was skipped.
    """
    if layout_result is None:
        return _USER_EXAM

    _QUAD = {
        (1, 1): "top-left", (1, 2): "top-right",
        (2, 1): "bottom-left", (2, 2): "bottom-right",
    }

    if is_split:
        rows, cols = layout_result.rows, layout_result.cols
        order = layout_result.reading_order or [
            [r + 1, c + 1] for r in range(rows) for c in range(cols)
        ]
        cells = len(order)
        order_labels = [_QUAD.get((rc[0], rc[1]), f"r{rc[0]}c{rc[1]}") for rc in order]
        reading_order_str = " → ".join(order_labels)

        mapping_lines = []
        for split_p in range(1, n_split_pages + 1):
            phys = (split_p - 1) // cells + 1
            rc = order[(split_p - 1) % cells]
            label = _QUAD.get((rc[0], rc[1]), f"row {rc[0]} col {rc[1]}")
            mapping_lines.append(
                f"  PDF page {split_p} → "
                f"page=\"{phys}\" subpage_row=\"{rc[0]}\" subpage_col=\"{rc[1]}\" ({label})"
            )
        mapping = "\n".join(mapping_lines)

        header = (
            f"The layout of this exam has already been detected: "
            f"{rows}\u00d7{cols} grid, reading order: {reading_order_str}.\n"
            f"This PDF has been pre-split into {n_split_pages} individual sub-pages "
            f"(one per PDF page).\n\n"
            "Return ONLY well-formed XML, no markdown fences or other text outside the XML.\n\n"
            f'Set the root element as: <exam rows="{rows}" cols="{cols}">\n\n'
            "Use this mapping to set page, subpage_row, and subpage_col for each question\n"
            "based on which PDF page the question physically appears on:\n"
            f"{mapping}\n\n"
        )
        page_desc      = "exam page from the mapping above"
        subpage_r_desc = "subpage_row from the mapping above"
        subpage_c_desc = "subpage_col from the mapping above"
    else:
        # 1×1 non-split (layout_result.rows == layout_result.cols == 1 always here)
        header = (
            "The layout of this exam has already been detected: 1\u00d71 (one sub-page per page).\n\n"
            "Return ONLY well-formed XML, no markdown fences or other text outside the XML.\n\n"
            'Set the root element as: <exam rows="1" cols="1">\n\n'
            'Set subpage_row="1" and subpage_col="1" for every question.\n\n'
        )
        page_desc      = "1-based page number where this question first appears"
        subpage_r_desc = "always 1"
        subpage_c_desc = "always 1"

    common_tail = (
        "Extract every question and sub-question at every nesting level as <question> elements.\n"
        "Nested sub-questions are child <question> elements inside their parent.\n\n"
        "Each <question> must have these attributes:\n"
        '- number: the label as printed, run-together — "9", then "9a", then "9ai"'
        " (no parentheses or spaces)\n"
        "- type: one of multiple_choice | short_answer | calculation | long_answer\n"
        f"- page: {page_desc}\n"
        f"- subpage_row: {subpage_r_desc}\n"
        f"- subpage_col: {subpage_c_desc}\n"
        "- marks: integer mark allocation from [N] brackets; 0 if not printed\n\n"
        "Each <question> must contain:\n"
        "- <text>: complete question text in markdown; $...$ for inline math, $$...$$ for display math\n"
        '- <option letter="A">text</option>: for multiple_choice only — one per answer option\n'
        "- child <question> elements for any sub-questions\n\n"
        "In XML text content use &lt; for <, &gt; for >, &amp; for &.\n"
    )

    return header + common_tail


_SYSTEM_SCHEME = (
    "You are an expert at reading Cambridge IGCSE mark schemes. "
    "Extract marking criteria as structured XML."
)

_USER_SCHEME = """\
Return ONLY well-formed XML, no markdown fences or other text outside the XML.

Below is a scaffold listing every question from the exam. Fill in the correct_answer \
attribute and add <criterion> children for each question, based on the mark scheme PDF.

{scaffold}

For each <question>:
- correct_answer attribute: model answer with $...$ for inline math \
(e.g. "$1.5 \\times 10^{{11}}$ m"); for multiple-choice just the letter
- <criterion> children: each has a mark attribute ("B1"/"M1"/"A1"/etc., or empty string); \
element text is the criterion description — use $...$ for any math
- Extract each criterion exactly as it appears in the mark scheme — do not merge multiple \
criteria into one <criterion> element and do not add any criteria that are not in the PDF
- Bullet points, numbered items, and semicolon-separated list entries should each become a \
separate <criterion> element, copied verbatim
- For multiple_choice questions: set correct_answer only; no <criterion> children needed
- Keep every <question> element present — even if marks cannot be found for it
- In XML text use &lt; for <, &gt; for >, &amp; for &
- If the mark scheme contains a diagram, graph, or image (NOT a table) as part of the \
expected answer for a question, add one <graphic page="N" x0="…" y0="…" x1="…" y1="…"/> \
child element per graphic, where N is the 1-based page number in the mark scheme PDF and \
x0/y0/x1/y1 are normalised bounding-box coordinates (0.0–1.0, top-left origin). \
Omit <graphic> entirely when there is no graphic.
"""

_SYSTEM_LAYOUT = "You are an expert at identifying exam paper printing layouts."

_USER_LAYOUT = """\
Look at this exam page image. Determine how many exam sub-pages are printed on this \
physical page and in what reading order they appear.

Return:
- "rows": number of rows of sub-pages (1 or 2)
- "cols": number of columns of sub-pages (1 or 2)
- "reading_order": list of [row, col] pairs (1-based) in the order a reader would \
read the sub-pages left-to-right, top-to-bottom

Standard single-page exam:
  {"rows":1,"cols":1,"reading_order":[[1,1]]}
Two-up landscape (left exam / right exam):
  {"rows":1,"cols":2,"reading_order":[[1,1],[1,2]]}
Four-up 2x2 grid, standard reading order:
  {"rows":2,"cols":2,"reading_order":[[1,1],[1,2],[2,1],[2,2]]}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(n: str) -> str:
    """Normalise a question number for matching: "(9)(a)(i)" → "9ai", "9A" → "9a"."""
    return re.sub(r"[()\s]", "", str(n)).lower()


def _merge_scheme(questions: list[dict], scheme_map: dict[str, dict]) -> None:
    """Recursively annotate *questions* in-place with correct_answer/marking_criteria."""
    for node in questions:
        key = _norm(node.get("number", ""))
        entry = scheme_map.get(key)
        if entry:
            node["correct_answer"] = entry.get("correct_answer")
            criteria_lines = []
            for m in (entry.get("mark_scheme") or []):
                criterion = m.get("criterion", "").lstrip("\t ")
                if not criterion:
                    continue
                mark_label = m.get("mark") or ""
                prefix = f"[{mark_label}] " if mark_label else ""
                criteria_lines.append(f"{prefix}{criterion}")
            node["marking_criteria"] = "\n".join(criteria_lines) or None
        else:
            node.setdefault("correct_answer", None)
            node.setdefault("marking_criteria", None)
        _merge_scheme(node.get("subquestions") or [], scheme_map)


def _build_scheme_scaffold(questions: list[dict]) -> str:
    """Build a flat XML scaffold from parsed exam questions for the mark scheme AI."""
    lines = ["<scheme>"]

    def _visit(node: dict) -> None:
        num = node.get("number", "")
        qtype = node.get("question_type", "")
        marks = node.get("marks", 0)
        lines.append(f'  <question number="{num}" type="{qtype}" marks="{marks}" correct_answer=""/>')
        for sub in (node.get("subquestions") or []):
            _visit(sub)

    for q in questions:
        _visit(q)

    lines.append("</scheme>")
    return "\n".join(lines)


def _json_to_question(node: dict, layout: ExamLayout) -> Question:
    """Convert a raw JSON dict from Gemini into a Question dataclass."""
    page = max(1, int(node.get("page") or 1))
    subpage_row = min(max(1, int(node.get("subpage_row") or 1)), layout.rows)
    subpage_col = min(max(1, int(node.get("subpage_col") or 1)), layout.cols)
    return Question(
        number=str(node["number"]),
        question_type=node.get("question_type", "short_answer"),
        text=node.get("text", ""),
        marks=max(0, int(node.get("marks") or 0)),
        bbox=BBox(0.0, 0.0, 0.0, 0.0, page),   # page preserved; spatial coords zeroed
        subpage_row=subpage_row,
        subpage_col=subpage_col,
        answer_options=[
            McAnswerOption(letter=str(o["letter"]), text=str(o.get("text") or ""))
            for o in (node.get("answer_options") or [])
            if isinstance(o, dict) and o.get("letter")
        ],
        subquestions=[_json_to_question(s, layout) for s in (node.get("subquestions") or [])],
        correct_answer=node.get("correct_answer"),
        marking_criteria=node.get("marking_criteria"),
    )


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _upload_and_poll(client, path: Path, label: str):
    """Upload *path* to the Gemini Files API, poll until ACTIVE, return the file object."""
    f = client.files.upload(file=path)
    for _ in range(120):  # up to 6 minutes at 3 s intervals
        if getattr(f.state, "name", str(f.state)) != "PROCESSING":
            break
        time.sleep(3)
        f = client.files.get(name=f.name)
    else:
        raise TimeoutError(f"Gemini file upload timed out after 6 min ({label}): {f.name}")
    state = getattr(f.state, "name", str(f.state))
    if state == "FAILED":
        raise RuntimeError(f"Gemini file processing failed ({label}): {f.name}")
    return f


def _extract_text(resp) -> str:
    """Return resp.text, tolerating None and empty-candidates responses."""
    try:
        return resp.text or ""
    except Exception:
        return ""


def _finish_reason(resp) -> str:
    """Return a human-readable diagnostic: finish_reason + block_reason if set."""
    parts = []
    try:
        if resp.candidates:
            parts.append(f"finish_reason={resp.candidates[0].finish_reason.name}")
        pf = getattr(resp, "prompt_feedback", None)
        if pf and getattr(pf, "block_reason", None):
            parts.append(f"block_reason={pf.block_reason.name}")
    except Exception:
        pass
    return ", ".join(parts) or "unknown"


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------

def _serialize_exam_xml(questions: list[dict], layout: dict) -> str:
    """Serialise post-remap question dicts to <exam rows cols> XML string."""
    import xml.etree.ElementTree as ET

    def _q_el(parent: ET.Element, q: dict) -> None:
        el = ET.SubElement(parent, "question")
        el.set("number", str(q.get("number", "")))
        el.set("type", str(q.get("question_type", "short_answer")))
        el.set("page", str(q.get("page", 1)))
        el.set("subpage_row", str(q.get("subpage_row", 1)))
        el.set("subpage_col", str(q.get("subpage_col", 1)))
        el.set("marks", str(q.get("marks", 0)))
        text_el = ET.SubElement(el, "text")
        text_el.text = q.get("text", "")
        for opt in (q.get("answer_options") or []):
            opt_el = ET.SubElement(el, "option")
            opt_el.set("letter", str(opt.get("letter", "")))
            opt_el.text = opt.get("text", "")
        for sub in (q.get("subquestions") or []):
            _q_el(el, sub)

    root = ET.Element("exam")
    root.set("rows", str(layout.get("rows", 1)))
    root.set("cols", str(layout.get("cols", 1)))
    for q in questions:
        _q_el(root, q)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def _save_exam_questions_xml(artifact_dir: Path, raw_questions: list[dict], layout: dict) -> None:
    from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown
    xml_path = artifact_exam_questions_xml_path(artifact_dir)
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(_serialize_exam_xml(raw_questions, layout), encoding="utf-8")
    write_raw_exam_markdown(artifact_dir, raw_questions)


# ---------------------------------------------------------------------------
# Layout detection helpers (split-subpages mode)
# ---------------------------------------------------------------------------

def _detect_layout(
    client, exam_pdf: Path, model: str, effort: "str | None" = None
) -> tuple["_LayoutDetectSchema", float, "str | None", "str | None"]:
    """Cheap layout detection: render first page as JPEG, ask Gemini for rows/cols/order.

    Returns (result, elapsed_s, raw_response_text, error_summary).
    On success: error_summary is None.
    On failure: falls back to 1×1; error_summary is a one-line description; raw_response_text
    may still be set if the API succeeded but JSON parsing failed.
    """
    from google.genai import types as gai_types
    import fitz

    with fitz.open(str(exam_pdf)) as doc:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.0, 1.0))  # 72 DPI
    img_bytes = pix.tobytes("jpeg")

    from xscore.config import GEMINI_MAX_OUTPUT_TOKENS
    _thinking_map = {"off": 0, "low": 1024, "high": 8192}
    cfg_kwargs: dict = {
        "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
        "response_mime_type": "application/json",
        "response_json_schema": _LAYOUT_DETECT_JSON_SCHEMA,
    }
    if effort in _thinking_map:
        cfg_kwargs["thinking_config"] = gai_types.ThinkingConfig(
            thinking_budget=_thinking_map[effort],
            include_thoughts=False,
        )
    cfg = gai_types.GenerateContentConfig(system_instruction=_SYSTEM_LAYOUT, **cfg_kwargs)

    raw_text: str | None = None
    t0 = time.perf_counter()
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[
                gai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                gai_types.Part.from_text(text=_USER_LAYOUT),
            ],
            config=cfg,
        )
        elapsed = time.perf_counter() - t0
        raw_text = resp.text
        result = _LayoutDetectSchema.model_validate_json(raw_text)
        return result, elapsed, raw_text, None
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        err_summary = str(exc).split("\n")[0]
        return _LayoutDetectSchema(rows=1, cols=1, reading_order=[]), elapsed, raw_text, err_summary


def _order_cells(page_rect, layout: "_LayoutDetectSchema") -> list:
    """Crop rects for *page_rect* in the detected reading order (row, col entries are 1-based)."""
    import fitz

    r = page_rect
    cw = r.width / layout.cols
    rh = r.height / layout.rows

    def cell(row: int, col: int) -> "fitz.Rect":
        return fitz.Rect(
            r.x0 + (col - 1) * cw, r.y0 + (row - 1) * rh,
            r.x0 + col * cw,       r.y0 + row * rh,
        )

    order = layout.reading_order
    if not order:
        order = [[row + 1, col + 1] for row in range(layout.rows) for col in range(layout.cols)]
    return [cell(rc[0], rc[1]) for rc in order]


def _split_pdf_by_layout(exam_pdf: Path, layout: "_LayoutDetectSchema") -> tuple[Path, int, int]:
    """Split *exam_pdf* into a temp PDF where each page = one sub-page in reading order.

    Returns *(temp_path, n_physical_pages, n_split_pages)*.
    The caller must delete *temp_path* when done.
    """
    import fitz
    import tempfile

    src = fitz.open(str(exam_pdf))
    dst = fitz.open()
    for page_idx in range(len(src)):
        for cell in _order_cells(src[page_idx].rect, layout):
            new_page = dst.new_page(width=cell.width, height=cell.height)
            new_page.show_pdf_page(new_page.rect, src, page_idx, clip=cell)
    n_physical = len(src)
    n_split = len(dst)
    src.close()

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    dst.save(str(tmp_path))
    dst.close()
    return tmp_path, n_physical, n_split



def _cell_label(row: int, col: int) -> str:
    return ("T" if row == 1 else "B") + ("L" if col == 1 else "R")


def _serialize_layout_xml(
    layout: "_LayoutDetectSchema",
    model: str,
    elapsed: float,
    n_physical: int,
    n_split: int,
) -> str:
    import xml.etree.ElementTree as ET
    _LABEL = {(1, 1): "TL", (1, 2): "TR", (2, 1): "BL", (2, 2): "BR"}
    root = ET.Element("layout")
    root.set("rows", str(layout.rows))
    root.set("cols", str(layout.cols))
    root.set("model", model)
    root.set("elapsed_s", f"{elapsed:.2f}")
    root.set("n_physical_pages", str(n_physical))
    root.set("n_split_pages", str(n_split))
    order = layout.reading_order or [
        [r + 1, c + 1] for r in range(layout.rows) for c in range(layout.cols)
    ]
    labels = [_LABEL.get((rc[0], rc[1]), f"r{rc[0]}c{rc[1]}") for rc in order]
    root.set("reading_order", " ".join(labels))
    for i, (rc, label) in enumerate(zip(order, labels)):
        cel = ET.SubElement(root, "cell")
        cel.set("position", str(i + 1))
        cel.set("row", str(rc[0]))
        cel.set("col", str(rc[1]))
        cel.set("label", label)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def _save_layout_artifact(
    artifact_dir: Path,
    layout: "_LayoutDetectSchema",
    model: str,
    elapsed: float,
    n_physical: int,
    n_split: int,
) -> None:
    """Write step-8 (split mode) artifacts: ``4a_exam_layout.json`` + ``.md``."""
    from xscore.shared.exam_paths import (
        artifact_exam_layout_xml_path,
        artifact_exam_layout_markdown_path,
    )

    n_cells = layout.rows * layout.cols
    order = layout.reading_order or [
        [r + 1, c + 1] for r in range(layout.rows) for c in range(layout.cols)
    ]
    order_labels = [_cell_label(rc[0], rc[1]) for rc in order]

    if n_cells > 1:
        layout_label = f"{layout.rows}×{layout.cols} ({n_cells}-up)"
    else:
        layout_label = "1×1 (single)"

    if n_cells > 1:
        order_str = " → ".join(order_labels)
        md_lines = [
            "# Exam Layout",
            "",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Layout | {layout_label} |",
            f"| Reading order | {order_str} |",
            f"| Physical pages | {n_physical} |",
            f"| Sub-pages | {n_split} |",
            f"| Model | {model} |",
            f"| Elapsed | {elapsed:.1f} s |",
            "",
        ]
    else:
        md_lines = [
            "# Exam Layout",
            "",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Layout | {layout_label} |",
            f"| Model | {model} |",
            f"| Elapsed | {elapsed:.1f} s |",
            "",
        ]

    try:
        p = artifact_exam_layout_xml_path(artifact_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_serialize_layout_xml(layout, model, elapsed, n_physical, n_split), encoding="utf-8")

        with open(artifact_exam_layout_markdown_path(artifact_dir), "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# XML parsing helpers
# ---------------------------------------------------------------------------

def _preprocess_xml(raw: str) -> str:
    """Strip markdown fences and fix unescaped & before XML parsing."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
    return re.sub(r"&(?![a-zA-Z#]\w*;)", "&amp;", raw)


def _parse_exam_xml(raw: str) -> tuple[list[dict], dict]:
    """Parse Gemini exam XML → (questions_list, layout_dict).
    Raises ET.ParseError / RuntimeError if malformed.
    """
    root = ET.fromstring(_preprocess_xml(raw))
    layout = {"rows": int(root.get("rows", 1)), "cols": int(root.get("cols", 1))}

    def _parse_q(el: ET.Element) -> dict:
        text_el = el.find("text")
        return {
            "number":        el.get("number", ""),
            "question_type": el.get("type", "short_answer"),
            "page":          int(el.get("page", 1)),
            "subpage_row":   int(el.get("subpage_row", 1)),
            "subpage_col":   int(el.get("subpage_col", 1)),
            "marks":         int(el.get("marks", 0)),
            "text":          (text_el.text or "").strip() if text_el is not None else "",
            "answer_options": [
                {"letter": opt.get("letter", ""), "text": (opt.text or "").strip()}
                for opt in el.findall("option")
            ],
            "subquestions": [_parse_q(child) for child in el.findall("question")],
        }

    return [_parse_q(q_el) for q_el in root.findall("question")], layout


def _parse_graphic(g_el) -> "dict | None":
    try:
        return {
            "page": int(g_el.get("page", 1)),
            "x0": float(g_el.get("x0", 0)), "y0": float(g_el.get("y0", 0)),
            "x1": float(g_el.get("x1", 1)), "y1": float(g_el.get("y1", 1)),
        }
    except (ValueError, TypeError):
        return None


def _parse_scheme_xml(raw: str) -> dict:
    """Parse Gemini mark scheme XML → scheme dict. Non-fatal: returns empty on error."""
    try:
        root = ET.fromstring(_preprocess_xml(raw))
    except ET.ParseError:
        return {"questions": []}
    questions = []
    for q_el in root.findall("question"):
        questions.append({
            "number":         q_el.get("number", ""),
            "correct_answer": q_el.get("correct_answer") or None,
            "mark_scheme": [
                {"mark": c.get("mark", ""), "criterion": (c.text or "").strip()}
                for c in q_el.findall("criterion")
            ],
            "graphics": [g for g_el in q_el.findall("graphic") if (g := _parse_graphic(g_el))],
        })
    return {"questions": questions}


def _extract_scheme_graphics(
    questions: list[dict],
    scheme_pdf: "Path",
    out_dir: "Path",
    dpi: int = 150,
) -> None:
    """Crop graphic bboxes from scheme_pdf and save as PNG files in out_dir."""
    import fitz
    out_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(str(scheme_pdf)) as doc:
        for q in questions:
            graphics = q.get("graphics") or []
            if not graphics:
                continue
            safe_num = re.sub(r"[^\w]", "_", str(q.get("number", "unknown")))
            for idx, g in enumerate(graphics, start=1):
                page_idx = g["page"] - 1
                if not (0 <= page_idx < doc.page_count):
                    continue
                page = doc[page_idx]
                w, h = page.rect.width, page.rect.height
                clip = fitz.Rect(g["x0"] * w, g["y0"] * h, g["x1"] * w, g["y1"] * h)
                if clip.is_empty or clip.is_infinite:
                    continue
                pix = page.get_pixmap(dpi=dpi, clip=clip)
                pix.save(str(out_dir / f"{g['page']}_{safe_num}_{idx}.png"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_ai_scaffold(
    exam_pdf: Path,
    marking_scheme_pdf: Path | None,
    *,
    split_subpages: bool = True,
    on_layout_complete: "Callable[[], None] | None" = None,
    on_cut_complete: "Callable[[bool], None] | None" = None,
    on_exam_complete: "Callable[[list[dict]], None] | None" = None,
    on_scheme_complete: "Callable[[list[dict]], None] | None" = None,
    artifact_dir: Path | None = None,
) -> tuple[list[Question], ExamLayout]:
    """Extract exam structure via Gemini and return a list[Question].

    Args:
        exam_pdf: Path to the exam question-paper PDF.
        marking_scheme_pdf: Optional mark-scheme PDF; skipped when None.
        split_subpages: When True (default), run a cheap layout-detection call first,
            then split the exam PDF into individual sub-pages before extraction.
            Disable with READ_EXAM_PDF_SPLIT=0 to use the legacy single-call path.
        on_exam_complete: Optional callback invoked with the raw question dicts
            after the first API call (exam extraction) completes successfully.
            Use this to advance the pipeline step counter between the two calls.
        on_scheme_complete: Optional callback invoked with the raw scheme question
            dicts after the second API call completes, but *before* the scheme is
            merged into the question tree.  Use this to advance the step counter
            to the merge step.  May raise SystemExit(0) to stop before merging.
        artifact_dir: If set, write intermediate JSON + Markdown snapshots:
            ``9_exam_layout.*`` after layout detection (split mode only),
            ``10_exam_questions.*`` after call 1, ``11_mark_scheme.*`` after call 2.
            Saves are best-effort; OSError is silently ignored.

    Returns:
        Tuple of (list[Question], ExamLayout). Questions have spatial BBox coordinates
        zeroed (page and subpage numbers preserved).

    Raises:
        RuntimeError: GOOGLE_API_KEY unset, file upload failed, or Gemini returns non-JSON.
    """
    try:
        from google import genai as gai
        from google.genai import types as gai_types
    except ImportError:
        raise RuntimeError("google-genai not installed; run: pip install google-genai")

    api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    exam_model, exam_effort = _exam_pdf_model_config()
    scheme_model, scheme_effort = _mark_scheme_model_config()
    client = gai.Client(api_key=api_key)

    thinking_map = {"off": 0, "low": 1024, "high": 8192}

    def _make_gen_config(
        effort: str | None, system: str, schema: dict | None = None
    ) -> "gai_types.GenerateContentConfig":
        cfg: dict = {"max_output_tokens": 65536}
        if schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = schema
        if effort in thinking_map:
            cfg["thinking_config"] = gai_types.ThinkingConfig(
                thinking_budget=thinking_map[effort],
                include_thoughts=False,
            )
        return gai_types.GenerateContentConfig(system_instruction=system, **cfg)

    # State tracked across the try/finally (split PDF must always be cleaned up)
    split_pdf_path: Path | None = None
    n_physical_pages: int = 0
    n_split_pages: int = 0
    layout_result: _LayoutDetectSchema | None = None
    layout_elapsed: float = 0.0
    layout_model: str = ""
    uploaded_files: dict[str, object] = {}

    try:
        from xscore.shared.terminal_ui import api_latency_line, ok_line, tool_line, warn_line

        # ---- Step A: cheap layout detection + PDF splitting (split mode) ---
        if split_subpages:
            layout_model, layout_effort = _layout_detect_model_config()

            # Save prompt before API call
            if artifact_dir is not None:
                save_prompt(
                    artifact_prompt_path(artifact_dir, "9_detect_layout"),
                    model=layout_model, system=_SYSTEM_LAYOUT,
                    messages=[{"role": "user", "content": _USER_LAYOUT}],
                )

            layout_result, layout_elapsed, layout_raw_text, layout_error = _detect_layout(
                client, exam_pdf, layout_model, layout_effort
            )

            # Save raw AI response immediately (even on failure, if we got a response)
            if artifact_dir is not None and layout_raw_text is not None:
                try:
                    from xscore.shared.exam_paths import artifact_exam_layout_json_path
                    raw_path = artifact_exam_layout_json_path(artifact_dir).parent / "9_exam_layout_raw.json"
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    raw_path.write_text(layout_raw_text, encoding="utf-8")
                except OSError:
                    pass

            # Terminal output
            n_cells = layout_result.rows * layout_result.cols
            if layout_error is not None:
                warn_line(
                    f"Layout detection failed — assuming 1×1"
                    f"  ·  {layout_model}  ·  {layout_elapsed:.1f}s"
                    f"\n    {layout_error}"
                )
            elif n_cells > 1:
                ok_line(
                    f"Layout {layout_result.rows}×{layout_result.cols} ({n_cells}-up)"
                    f"  ·  {layout_model}  ·  {layout_elapsed:.1f}s"
                )
            else:
                ok_line(f"Layout 1×1 (single)  ·  {layout_model}  ·  {layout_elapsed:.1f}s")

            if on_layout_complete is not None:
                on_layout_complete()

            if n_cells > 1:
                layout_label = f"{layout_result.rows}×{layout_result.cols}"
                tool_line("split", f"Splitting exam PDF ({layout_label} layout) …")
                split_pdf_path, n_physical_pages, n_split_pages = _split_pdf_by_layout(
                    exam_pdf, layout_result
                )
                ok_line(f"{n_physical_pages} physical page(s) → {n_split_pages} sub-pages")
                if artifact_dir is not None:
                    try:
                        import shutil
                        from xscore.shared.exam_paths import artifact_split_exam_pdf_path
                        dest = artifact_split_exam_pdf_path(artifact_dir)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(split_pdf_path), str(dest))
                    except OSError:
                        pass

            # Save layout artifact immediately — do not wait for exam call to finish
            if artifact_dir is not None:
                _save_layout_artifact(
                    artifact_dir, layout_result, layout_model, layout_elapsed,
                    n_physical_pages, n_split_pages,
                )
                # In 1×1 mode no split PDF is produced; copy the original so
                # the artifact directory always contains the PDF sent to Gemini.
                if n_cells == 1:
                    try:
                        import shutil
                        from xscore.shared.exam_paths import artifact_exam_input_pdf_path
                        dest = artifact_exam_input_pdf_path(artifact_dir)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(exam_pdf), str(dest))
                    except OSError:
                        pass

            if on_cut_complete is not None:
                on_cut_complete(n_cells == 1)

        # ---- Upload PDFs in parallel ----------------------------------------
        actual_exam_pdf = split_pdf_path if split_pdf_path is not None else exam_pdf
        pdfs_to_upload: list[tuple[str, Path]] = [("exam", actual_exam_pdf)]
        if marking_scheme_pdf is not None:
            pdfs_to_upload.append(("scheme", marking_scheme_pdf))

        def _upload(item: tuple[str, Path]):
            label, path = item
            return label, _upload_and_poll(client, path, label)

        with ThreadPoolExecutor(max_workers=2) as pool:
            for label, f in pool.map(_upload, pdfs_to_upload):
                uploaded_files[label] = f

        # ---- Inference closures (called from threads or inline) -----------

        def _do_exam_call() -> tuple[list[dict], dict]:
            user_exam = _build_user_exam_prompt(
                layout_result, split_pdf_path is not None, n_split_pages
            )
            _t0 = time.perf_counter()
            resp = client.models.generate_content(
                model=exam_model,
                contents=[
                    gai_types.Part.from_uri(
                        file_uri=uploaded_files["exam"].uri, mime_type="application/pdf"
                    ),
                    gai_types.Part.from_text(text=user_exam),
                ],
                config=_make_gen_config(exam_effort, _SYSTEM_EXAM),
            )
            api_latency_line(time.perf_counter() - _t0, label="exam")
            if artifact_dir is not None:
                save_prompt(
                    artifact_prompt_path(artifact_dir, "10_exam_questions"),
                    model=exam_model, system=_SYSTEM_EXAM,
                    messages=[{
                        "role": "user",
                        "content": f"[PDF: {actual_exam_pdf.name}]\n\n{user_exam}",
                    }],
                )
            raw_exam = _extract_text(resp)
            if not raw_exam:
                reason = _finish_reason(resp)
                warn_line(f"Exam API: empty response ({reason}) — retrying once …")
                _t0 = time.perf_counter()
                resp = client.models.generate_content(
                    model=exam_model,
                    contents=[
                        gai_types.Part.from_uri(
                            file_uri=uploaded_files["exam"].uri, mime_type="application/pdf"
                        ),
                        gai_types.Part.from_text(text=user_exam),
                    ],
                    config=_make_gen_config(exam_effort, _SYSTEM_EXAM),
                )
                api_latency_line(time.perf_counter() - _t0, label="exam retry")
                raw_exam = _extract_text(resp)
                if not raw_exam:
                    reason = _finish_reason(resp)
                    if artifact_dir is not None:
                        try:
                            p = artifact_exam_questions_raw_xml_path(artifact_dir)
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_text(f"<!-- empty response: {reason} -->", encoding="utf-8")
                        except OSError:
                            pass
                    raise RuntimeError(
                        f"Gemini exam response empty after retry — {reason}"
                    )
            if artifact_dir is not None:
                try:
                    p = artifact_exam_questions_raw_xml_path(artifact_dir)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(raw_exam, encoding="utf-8")
                except OSError:
                    pass
            try:
                return _parse_exam_xml(raw_exam)
            except Exception as exc:
                raise RuntimeError(
                    f"Gemini exam response failed XML parsing: {exc}: {raw_exam[:300]!r}"
                )

        def _do_scheme_call(scaffold: str) -> dict:
            user_msg = _USER_SCHEME.format(scaffold=scaffold)
            _t0 = time.perf_counter()
            resp = client.models.generate_content(
                model=scheme_model,
                contents=[
                    gai_types.Part.from_uri(
                        file_uri=uploaded_files["scheme"].uri, mime_type="application/pdf"
                    ),
                    gai_types.Part.from_text(text=user_msg),
                ],
                config=_make_gen_config(scheme_effort, _SYSTEM_SCHEME),
            )
            api_latency_line(time.perf_counter() - _t0, label="mark scheme")
            if artifact_dir is not None:
                save_prompt(
                    artifact_prompt_path(artifact_dir, "11_mark_scheme"),
                    model=scheme_model, system=_SYSTEM_SCHEME,
                    messages=[{
                        "role": "user",
                        "content": f"[PDF: {marking_scheme_pdf.name}]\n\n{user_msg}",
                    }],
                )
            raw_scheme = _extract_text(resp)
            if not raw_scheme:
                warn_line(f"Mark scheme API: empty response ({_finish_reason(resp)}) — skipping scheme")
            result = _parse_scheme_xml(raw_scheme)
            if artifact_dir is not None:
                try:
                    from xscore.scaffold.scaffold_markdown import write_mark_scheme_markdown
                    p = artifact_mark_scheme_xml_path(artifact_dir)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(_preprocess_xml(raw_scheme), encoding="utf-8")
                    write_mark_scheme_markdown(artifact_dir, result.get("questions", []))
                except Exception:
                    pass
            if artifact_dir is not None and marking_scheme_pdf is not None:
                _graphics_dpi = int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "150"))
                _n_graphics = sum(len(q.get("graphics") or []) for q in result.get("questions", []))
                if _n_graphics:
                    try:
                        _extract_scheme_graphics(
                            result.get("questions", []),
                            marking_scheme_pdf,
                            artifact_dir / "11_mark_scheme_graphics",
                            dpi=_graphics_dpi,
                        )
                        ok_line(f"Mark scheme: {_n_graphics} graphic(s) extracted")
                    except Exception:
                        warn_line("Mark scheme: graphic extraction failed")
                else:
                    ok_line("Mark scheme: no graphics detected")
            return result

        # ---- Step 9: exam extraction ----------------------------------------
        raw_layout: dict = {}
        raw_questions, raw_layout = _do_exam_call()

        # Use pre-detected layout (ignore raw_layout from extraction response in split mode).
        if layout_result is not None:
            raw_layout = {"rows": layout_result.rows, "cols": layout_result.cols}

        # Layout artifact already saved immediately after detection above.

        # Save step-9 artifacts BEFORE on_exam_complete — the callback may raise
        # SystemExit(0) when --through 9 is used, so anything after it won't run.
        if artifact_dir is not None:
            try:
                _save_exam_questions_xml(artifact_dir, raw_questions, raw_layout)
            except OSError:
                pass

        if on_exam_complete is not None:
            on_exam_complete(raw_questions)

        # ---- Step 10: mark scheme extraction (uses step-9 scaffold) ---------
        if "scheme" in uploaded_files:
            scaffold = _build_scheme_scaffold(raw_questions)
            try:
                scheme_data: dict = _do_scheme_call(scaffold)
            except Exception:
                scheme_data = {"questions": []}
        else:
            scheme_data = {"questions": []}

        if isinstance(scheme_data.get("questions"), list):
            # Step-10 XML + markdown already saved inside _do_scheme_call().
            # Notify caller that scheme parse is done, before merging.
            # The callback may raise SystemExit(0) for --through 5.
            if on_scheme_complete is not None:
                on_scheme_complete(scheme_data["questions"])

            # Suffix duplicate question numbers in exam questions so that
            # two questions both printed as "38" become "38" and "38_2".
            # Done after saving artifacts so 10_exam_questions.json retains original numbers.
            _seen_rq: dict[str, int] = {}
            for _node in raw_questions:
                _qnum = str(_node.get("number", ""))
                _seen_rq[_qnum] = _seen_rq.get(_qnum, 0) + 1
                if _seen_rq[_qnum] > 1:
                    _node["number"] = f"{_qnum}_{_seen_rq[_qnum]}"

            # Warn if duplicate-numbered questions share the same subpage (likely a scaffold error).
            import logging as _log
            _base_pos: dict[str, list] = {}
            for _node in raw_questions:
                _base = re.sub(r"_\d+$", "", str(_node.get("number", "")))
                _base_pos.setdefault(_base, []).append(
                    (_node.get("subpage_row"), _node.get("subpage_col"), _node.get("number"))
                )
            for _base, _bpos in _base_pos.items():
                if len(_bpos) > 1:
                    _coords = [(_r, _c) for _r, _c, _ in _bpos]
                    if len(set(_coords)) < len(_coords):
                        _log.warning(
                            "ai_scaffold: Q%s duplicates share the same subpage — "
                            "possible misclassification: %s", _base, _bpos
                        )

            # Apply the same suffix to mark scheme entries so scheme_map keys align.
            # Done after saving 11_mark_scheme.json to preserve original numbers there.
            _seen_sq: dict[str, int] = {}
            for _sq in scheme_data["questions"]:
                if not isinstance(_sq, dict) or not _sq.get("number"):
                    continue
                _snum = _norm(_sq.get("number", ""))
                _seen_sq[_snum] = _seen_sq.get(_snum, 0) + 1
                if _seen_sq[_snum] > 1:
                    _sq["number"] = f"{_sq['number']}_{_seen_sq[_snum]}"

            scheme_map: dict[str, dict] = {}
            for _sq in scheme_data["questions"]:
                if not isinstance(_sq, dict) or not _sq.get("number"):
                    continue
                _k = _norm(_sq["number"])
                scheme_map[_k] = _sq
                # The mark scheme AI may use "_alt" for a second occurrence of the
                # same question number while the exam dedup logic uses "_2". Add a
                # numeric alias so both conventions resolve to the same entry.
                _alt_m = re.match(r"^(.+?)_alt(\d*)$", _k)
                if _alt_m:
                    _base, _n = _alt_m.group(1), _alt_m.group(2)
                    _idx = (int(_n) + 1) if _n else 2
                    scheme_map[f"{_base}_{_idx}"] = _sq
            _merge_scheme(raw_questions, scheme_map)

    finally:
        # Delete uploaded Gemini files (auto-expire after 48 h anyway)
        for label, f in uploaded_files.items():
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass
        # Delete temp split PDF (always, even if upload or inference failed)
        if split_pdf_path is not None:
            try:
                split_pdf_path.unlink()
            except OSError:
                pass

    layout = ExamLayout(
        rows=max(1, int(raw_layout.get("rows") or 1)),
        cols=max(1, int(raw_layout.get("cols") or 1)),
    )

    import logging as _logging
    valid_nodes = []
    for node in raw_questions:
        if not isinstance(node, dict) or "number" not in node:
            _logging.warning("ai_scaffold: skipping question node missing 'number' key: %r", node)
            continue
        valid_nodes.append(node)

    questions = [_json_to_question(node, layout) for node in valid_nodes]
    _fix_zero_mark_leaves(questions)
    return questions, layout


def _fix_zero_mark_leaves(questions: list) -> None:
    """Upgrade any leaf question with marks=0 but a marking criterion to marks=1.

    Gemini sometimes returns marks=0 for sub-questions whose mark allocation is
    not explicitly bracketed in the PDF. When a marking criterion exists the
    question is worth at least 1 mark.
    """
    import logging as _log
    for q in questions:
        if q.subquestions:
            _fix_zero_mark_leaves(q.subquestions)
        elif q.marks == 0 and q.marking_criteria:
            _log.warning(
                "ai_scaffold: %s has marks=0 but a marking criterion — upgraded to 1", q.number
            )
            q.marks = 1

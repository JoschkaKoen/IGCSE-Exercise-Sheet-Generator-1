"""Data helpers, XML parsing, artifact helpers, and merge utilities for the scaffold pipeline.

No Gemini API calls — pure data transformation. Fully isolated from the API layer.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from xscore.shared.models import BBox, ExamLayout, McAnswerOption, Question
from xscore.shared.terminal_ui import warn_line


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _norm(n: str) -> str:
    """Normalise a question number for matching: "(9)(a)(i)" → "9ai", "9A" → "9a"."""
    return re.sub(r"[()\s]", "", str(n)).lower()


def _merge_scheme(questions: list[dict], scheme_map: dict[str, dict]) -> None:
    """Recursively annotate *questions* in-place with the parsed mark scheme.

    Type-driven schema:

    - **MCQ** — ``correct_answer`` (letter) + ``explanation`` (rationale bullets).
    - **Non-MCQ** — ``mark_scheme_answer`` (single block: the entire printed
      mark-scheme cell with per-criterion mark counts dropped).

    Reads the new-shape intermediate dict produced by
    ``_merge_scheme_results``: ``correct_answer``, ``explanation``,
    ``mark_scheme_answer`` are direct keys. Falls back to the legacy
    ``mark_scheme: [{mark, criterion}]`` list when present (in-flight runs
    on the old AI output shape).

    Legacy fields ``marking_criteria`` and ``reasoning`` are populated
    alongside during the transition so consumers that haven't migrated still
    work; both are removed in the cleanup step once all consumers are on the
    new fields.
    """
    for node in questions:
        key = _norm(node.get("number", ""))
        entry = scheme_map.get(key)
        if entry:
            ca = entry.get("correct_answer")
            is_mcq = node.get("question_type") == "multiple_choice"

            # Prefer direct new-shape fields; fall back to building from the
            # legacy ``mark_scheme: [...]`` list if neither is present.
            new_msa = entry.get("mark_scheme_answer")
            new_exp = entry.get("explanation")

            criteria_with_prefix: list[str] = []
            criteria_no_prefix: list[str] = []
            for m in (entry.get("mark_scheme") or []):
                criterion = m.get("criterion", "").lstrip("\t ")
                if not criterion:
                    continue
                mark_label = m.get("mark") or ""
                prefix = "" if is_mcq else (f"[{mark_label}] " if mark_label else "")
                criteria_with_prefix.append(f"{prefix}{criterion}")
                criteria_no_prefix.append(criterion)
            joined_with = "\n".join(criteria_with_prefix) or None
            joined_no = "\n".join(criteria_no_prefix) or None

            if is_mcq:
                explanation = new_exp or joined_no
                node["correct_answer"] = ca
                node["explanation"] = explanation
                node["mark_scheme_answer"] = None
                # Legacy fields, populated alongside for transitional consumers.
                node["reasoning"] = explanation
                node["marking_criteria"] = None
            else:
                if new_msa:
                    msa = new_msa
                else:
                    ca_str = str(ca).strip() if ca and str(ca).strip() else ""
                    if ca_str and joined_no:
                        msa = ca_str + "\n" + joined_no
                    else:
                        msa = ca_str or joined_no
                node["mark_scheme_answer"] = msa
                node["explanation"] = None
                # Legacy fields, populated alongside for transitional consumers.
                node["correct_answer"] = ca
                node["marking_criteria"] = joined_with
                node["reasoning"] = None
        else:
            node.setdefault("correct_answer", None)
            node.setdefault("mark_scheme_answer", None)
            node.setdefault("explanation", None)
            node.setdefault("marking_criteria", None)
            node.setdefault("reasoning", None)
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
        mark_scheme_answer=node.get("mark_scheme_answer"),
        explanation=node.get("explanation"),
        marking_criteria=node.get("marking_criteria"),
        reasoning=node.get("reasoning"),
    )


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------

def _serialize_exam_xml(questions: list[dict], layout: dict) -> str:
    """Serialise post-remap question dicts to <exam rows cols> XML string."""
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
    return ET.tostring(root, encoding="unicode")


def _save_exam_questions_xml(artifact_dir: Path, raw_questions: list[dict], layout: dict) -> None:
    from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown
    from xscore.shared.exam_paths import artifact_exam_questions_xml_path
    xml_path = artifact_exam_questions_xml_path(artifact_dir)
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(_serialize_exam_xml(raw_questions, layout), encoding="utf-8")
    write_raw_exam_markdown(artifact_dir, raw_questions)


# ---------------------------------------------------------------------------
# XML parsing helpers
# ---------------------------------------------------------------------------

def _preprocess_xml(raw: str) -> str:
    """Strip markdown fences and fix unescaped & before XML parsing."""
    from xscore.shared.response_parsing import strip_code_fences
    return re.sub(r"&(?![a-zA-Z#]\w*;)", "&amp;", strip_code_fences(raw))


def _parse_exam_xml(raw: str) -> tuple[list[dict], dict]:
    """Parse Gemini exam XML → (questions_list, layout_dict).
    Raises ET.ParseError / RuntimeError if malformed.
    """
    root = ET.fromstring(_preprocess_xml(raw))
    layout = {"rows": int(root.get("rows", 1)), "cols": int(root.get("cols", 1))}

    def _parse_q(el: ET.Element) -> dict:
        text_el = el.find("text")
        qtype = el.get("type", "short_answer")
        marks = int(el.get("marks", 0))
        if qtype == "multiple_choice" and marks == 0:
            from xscore.scaffold.formats.base import _mcq_default_points  # noqa: PLC0415
            marks = _mcq_default_points()
        return {
            "number":        el.get("number", ""),
            "question_type": qtype,
            "page":          int(el.get("page", 1)),
            "subpage_row":   int(el.get("subpage_row", 1)),
            "subpage_col":   int(el.get("subpage_col", 1)),
            "marks":         marks,
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
            "x0": 0.0, "y0": float(g_el.get("y0", 0)),
            "x1": 1.0, "y1": float(g_el.get("y1", 1)),
        }
    except (ValueError, TypeError):
        return None


def _parse_scheme_xml(raw: str) -> dict:
    """Parse Gemini mark scheme XML → scheme dict. Raises ``RuntimeError`` on parse error."""
    try:
        root = ET.fromstring(_preprocess_xml(raw))
    except ET.ParseError as exc:
        raise RuntimeError(f"Mark scheme XML parse error: {exc}") from exc
    questions = []
    for q_el in root.findall("question"):
        graphics_list = []
        for g_el in q_el.findall("graphic"):
            g = _parse_graphic(g_el)
            if g:
                graphics_list.append(g)
        questions.append({
            "number":         q_el.get("number", ""),
            "correct_answer": q_el.get("correct_answer") or None,
            "mark_scheme": [
                {"mark": c.get("mark", ""), "criterion": (c.text or "").strip()}
                for c in q_el.findall("criterion")
            ],
            "graphics": graphics_list,
        })
    return {"questions": questions}


def _merge_scheme_results(page_results: list[dict]) -> dict:
    """Merge per-page (or per-group) parse-scheme results into one dict.

    The new schema is type-driven (see ``parse_scheme_response``):

    - **MCQ**       → ``{number, question_type, correct_answer, explanation, graphics}``.
    - **Non-MCQ**   → ``{number, question_type, mark_scheme_answer, graphics}``.

    With the page-grouping logic from parse_mark_scheme, each question's content lands
    in exactly one group result, so the merge is simple: first non-null wins
    for content fields, graphics concatenate. Legacy ``mark_scheme: [...]``
    inputs (mid-transition) are also tolerated and pass through.
    """
    merged: dict[str, dict] = {}
    for result in page_results:
        for q in result.get("questions", []):
            num = q["number"]
            if num not in merged:
                merged[num] = {
                    "number":             num,
                    "question_type":      q.get("question_type"),
                    "correct_answer":     None,
                    "explanation":        None,
                    "mark_scheme_answer": None,
                    "mark_scheme":        [],   # legacy shape passthrough
                    "graphics":           [],
                }
            if merged[num].get("question_type") in (None, "") and q.get("question_type"):
                merged[num]["question_type"] = q["question_type"]
            for key in ("correct_answer", "explanation", "mark_scheme_answer"):
                if not merged[num].get(key) and q.get(key):
                    merged[num][key] = q[key]
            merged[num]["mark_scheme"].extend(q.get("mark_scheme") or [])
            merged[num]["graphics"].extend(q.get("graphics") or [])
    return {"questions": list(merged.values())}


def _extract_scheme_graphics(
    questions: list[dict],
    scheme_pdf: "Path",
    out_dir: "Path",
    dpi: int = 150,
    margin: float = 0.0,
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
                mx = margin * w
                my = margin * h
                clip = fitz.Rect(
                    max(0, g["x0"] * w - mx),
                    max(0, g["y0"] * h - my),
                    min(w, g["x1"] * w + mx),
                    min(h, g["y1"] * h + my),
                )
                if clip.is_empty or clip.is_infinite:
                    continue
                pix = page.get_pixmap(dpi=dpi, clip=clip)
                pix.save(str(out_dir / f"{g['page']}_{safe_num}_{idx}.png"))
                # Vector PDF crop alongside the PNG — graphicx picks .pdf
                # over .png automatically (xelatex extension default), so
                # the print path renders vectors instead of the 300-DPI
                # raster. PNG above remains both the AI input and the
                # silent fallback if this PDF write fails.
                out_pdf_path = out_dir / f"{g['page']}_{safe_num}_{idx}.pdf"
                try:
                    with fitz.open() as out_pdf:
                        # Destination rect equals clip dims — keep_proportion
                        # default (True) renders 1:1, no scaling, vectors
                        # preserved.
                        new_page = out_pdf.new_page(width=clip.width, height=clip.height)
                        new_page.show_pdf_page(new_page.rect, doc, page_idx, clip=clip)
                        out_pdf.save(str(out_pdf_path),
                                     garbage=4, deflate=True, clean=True)
                except Exception as exc:  # noqa: BLE001
                    warn_line(f"Mark scheme: vector PDF crop failed for "
                              f"{out_pdf_path.name} ({exc.__class__.__name__}) — "
                              f"falling back to PNG")

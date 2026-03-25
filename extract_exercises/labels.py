# -*- coding: utf-8 -*-
"""Exam / paper labels derived from filenames and job lists."""

import re
from pathlib import Path

from .config import PAGE_HEADER_BY_EXAM

# Cambridge-style PDF stem: ``…_s25_qp_12``, ``…_m25_ci_52``, ``…_w24_ms_21``.
# Session letter: s = June, m = March, w = Oct/Nov — library order is M → W → S.
_LIB_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"_([smw])(\d{2})_qp_(\d+)"), "qp"),
    (re.compile(r"_([smw])(\d{2})_ms_(\d+)"), "ms"),
    (re.compile(r"_([smw])(\d{2})_ci_(\d+)"), "ci"),
)
_SESSION_ORDER = {"m": 0, "w": 1, "s": 2}
_COMPONENT_ORDER = {"qp": 0, "ms": 1, "ci": 2}

# Short labels for session letter (library section headers).
_SESSION_LABEL = {"m": "M", "w": "W", "s": "S"}
_SESSION_TITLE = {
    "m": "March session",
    "w": "October / November session",
    "s": "June session",
}

# Descriptive IGCSE Mathematics filenames (e.g. ``0580 Mathematics June 2023 Question Paper  21.pdf``).
_MONTH_TO_SESSION = {"march": "m", "june": "s", "november": "w"}
_MATH_COMPONENT_ORDER = {"qp": 0, "ms": 1, "ci": 2, "gt": 3, "er": 4, "_": 9}
_MATH_DESCRIPTIVE = re.compile(
    r"mathematics\s+(march|june|november)\s+(\d{4})\s+(.+?)\s+(\d+)\s*$",
    re.IGNORECASE | re.DOTALL,
)
# ``… June 2024 Grade Thresholds`` / ``… November 2023 Examiner Report`` (no paper number).
_MATH_GT_ER = re.compile(
    r"mathematics\s+(march|june|november)\s+(\d{4})\s+(grade thresholds|examiner report)\s*$",
    re.IGNORECASE,
)


def _parse_math_descriptive_stem(stem: str) -> dict[str, str | int] | None:
    """Parse ``0580 Mathematics … June 2023 Question Paper  21``-style stems."""
    m = _MATH_DESCRIPTIVE.search(stem)
    if m:
        month_w, year_s, comp_raw, paper_s = m.group(1), m.group(2), m.group(3), m.group(4)
        letter = _MONTH_TO_SESSION.get(month_w.lower())
        if not letter:
            return None
        year = int(year_s, 10)
        paper = int(paper_s, 10)
        comp_norm = re.sub(r"\s+", " ", comp_raw.strip().lower())
        if "question paper" in comp_norm:
            kind = "qp"
        elif "mark scheme" in comp_norm:
            kind = "ms"
        elif "grade threshold" in comp_norm:
            kind = "gt"
        elif "examiner report" in comp_norm:
            kind = "er"
        else:
            kind = "_"
        yy_s = year_s[-2:]
        session_code = f"{letter}{yy_s}"
        return {
            "letter": letter,
            "year": year,
            "paper": paper,
            "kind": kind,
            "session_code": session_code,
        }
    m2 = _MATH_GT_ER.search(stem)
    if not m2:
        return None
    month_w, year_s, tail = m2.group(1), m2.group(2), m2.group(3).lower()
    letter = _MONTH_TO_SESSION.get(month_w.lower())
    if not letter:
        return None
    year = int(year_s, 10)
    kind = "gt" if "threshold" in tail else "er"
    yy_s = year_s[-2:]
    session_code = f"{letter}{yy_s}"
    return {
        "letter": letter,
        "year": year,
        "paper": 0,
        "kind": kind,
        "session_code": session_code,
    }


def library_pdf_group_meta(filename: str) -> dict[str, str]:
    """
    Fields for UI grouping: calendar year, session letter, component kind.
    Unknown filenames → ``group_year`` ``_other`` so they sort last.
    """
    stem = Path(filename).stem
    math = _parse_math_descriptive_stem(stem)
    if math:
        return {
            "group_year": str(math["year"]),
            "group_session": str(math["letter"]),
            "paper_kind": str(math["kind"]),
            "session_heading": _SESSION_LABEL[str(math["letter"])],
            "session_title": _SESSION_TITLE[str(math["letter"])],
        }
    stem_lower = stem.lower()
    for rx, kind in _LIB_NAME_PATTERNS:
        m = rx.search(stem_lower)
        if m:
            letter, yy_s, _paper_s = m.group(1), m.group(2), m.group(3)
            yy = int(yy_s, 10)
            year = 2000 + yy if yy < 70 else 1900 + yy
            return {
                "group_year": str(year),
                "group_session": letter,
                "paper_kind": kind,
                "session_heading": _SESSION_LABEL[letter],
                "session_title": _SESSION_TITLE[letter],
            }
    return {
        "group_year": "_other",
        "group_session": "_",
        "paper_kind": "_",
        "session_heading": "",
        "session_title": "",
    }


def library_pdf_sort_key(filename: str) -> tuple:
    """
    Sort key for exam PDF filenames:

    1. **Year** (newest first — ``s25`` before ``s24``).
    2. **Session** M → W → S (March, Oct/Nov, June).
    3. **Paper** number (11, 12, 21, … numerically).
    4. **Component** qp → ms → ci for the same paper.

    Names that do not match ``_*[smw]NN_(qp|ms|ci)_*`` sort last, A–Z.
    """
    stem_raw = Path(filename).stem
    stem = stem_raw.lower()
    math = _parse_math_descriptive_stem(stem_raw)
    if math:
        sess = _SESSION_ORDER.get(str(math["letter"]), 99)
        comp = _MATH_COMPONENT_ORDER.get(str(math["kind"]), 9)
        return (
            0,
            -int(math["year"]),
            sess,
            int(math["paper"]),
            comp,
            stem,
        )
    for rx, kind in _LIB_NAME_PATTERNS:
        m = rx.search(stem)
        if m:
            letter, yy_s, paper_s = m.group(1), m.group(2), m.group(3)
            yy = int(yy_s, 10)
            year = 2000 + yy if yy < 70 else 1900 + yy
            sess = _SESSION_ORDER.get(letter, 99)
            paper = int(paper_s, 10)
            comp = _COMPONENT_ORDER.get(kind, 9)
            # (0,) = structured; -year → newest calendar year first
            return (0, -year, sess, paper, comp, stem)
    return (1, stem)


def library_pdf_display_name(filename: str) -> str:
    """
    Human-readable label for the library UI only (disk filenames stay unchanged).

    Examples: ``0625_s23_qp_13.pdf`` → ``s23 Questions 13``;
    ``0478_w24_ms_21.pdf`` → ``w24 Answers 21``.
    """
    stem_raw = Path(filename).stem
    stem = stem_raw.lower()
    math = _parse_math_descriptive_stem(stem_raw)
    if math:
        sc = str(math["session_code"])
        k = str(math["kind"])
        pn = str(math["paper"])
        if k == "qp":
            return f"{sc} Questions {pn}"
        if k == "ms":
            return f"{sc} Answers {pn}"
        if k == "gt":
            return f"{sc} Grade thresholds"
        if k == "er":
            return f"{sc} Examiner report"
        return filename
    for rx, kind in _LIB_NAME_PATTERNS:
        m = rx.search(stem)
        if m:
            letter, yy_s, paper_s = m.group(1), m.group(2), m.group(3)
            session = f"{letter}{yy_s}"
            if kind == "qp":
                return f"{session} Questions {paper_s}"
            if kind == "ms":
                return f"{session} Answers {paper_s}"
            if kind == "ci":
                return f"{session} Instructions {paper_s}"
    return filename


def exam_label_from_filename(filename: str) -> str | None:
    """Return compact label like 'w24 21' from a PDF name, or None if pattern unknown."""
    stem_raw = Path(filename).stem
    stem = stem_raw.lower()
    math = _parse_math_descriptive_stem(stem_raw)
    if math and str(math["kind"]) == "qp":
        return f"{math['session_code']} {math['paper']}"
    for pattern in (
        r"_([smw]\d{2})_qp_(\d+)",
        r"_([smw]\d{2})_ms_(\d+)",
        r"_([smw]\d{2})_ci_(\d+)",
    ):
        m = re.search(pattern, stem)
        if m:
            return f"{m.group(1)} {m.group(2)}"
    return None


def build_exam_header_label_from_paths(paths: list[str | None]) -> str:
    """Comma-separated labels for distinct exams (e.g. 'w24 21, s23 42')."""
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if not p:
            continue
        lab = exam_label_from_filename(Path(p).name)
        if lab and lab not in seen:
            seen.add(lab)
            out.append(lab)
    if out:
        return ", ".join(out)
    return "Extracted exercises"


def build_exam_header_label(question_paper_path: str, mark_scheme_path: str | None) -> str:
    return build_exam_header_label_from_paths([question_paper_path, mark_scheme_path])


def paper_label_from_qp_path(qp_path: str) -> str:
    """Short paper id from the question-paper filename only (e.g. ``w24 21``)."""
    lab = exam_label_from_filename(Path(qp_path).name)
    if lab:
        return lab
    stem = Path(qp_path).stem
    if stem:
        return stem
    name = Path(qp_path).name
    if name:
        return name
    return "Extracted exercises"


def page_header_label(jobs: list[dict], exam_key: str | None) -> str:
    """
    Single line repeated at the top of every output page.

    When ``exam_key`` maps to a subject title, that title is used and the session/paper id
    (e.g. ``s25 21``) is shown in the body via markers / sub-labels — not in this string.

    Legacy / unknown exam: one paper → filename-based paper code in the header; several papers
    → a generic label (paper codes still appear above each block when multiple jobs are used).
    """
    if exam_key and exam_key in PAGE_HEADER_BY_EXAM:
        return PAGE_HEADER_BY_EXAM[exam_key]
    if len(jobs) == 1:
        return paper_label_from_qp_path(jobs[0]["input_pdf"])
    return "Extracted exercises"

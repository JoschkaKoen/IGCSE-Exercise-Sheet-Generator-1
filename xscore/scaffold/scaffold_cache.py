"""On-disk cache for the parsed :class:`ExamScaffold`.

Formats:

- ``report.yaml`` — primary format (matches the rest of the pipeline's YAML artifacts).
- ``report.xml`` — legacy format, still readable for resume from older runs.
- ``scaffold.md`` and a short markdown — produced by ``write_scaffold_markdown``
  for human inspection.

Legacy JSON caches (``scaffold_cache.json`` at various historical locations)
are still readable for backwards compatibility; :func:`_save_cache` migrates
them — and any sibling legacy ``report.xml`` — to YAML on the next save.

Note: this module is the **disk-cache** serializer for an ``ExamScaffold``. The
``xscore/scaffold/formats/`` package implements ``ScaffoldFormat`` classes used
during AI response parsing — a different concern entirely. Two layers, not
redundant.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

from xscore.shared.exam_paths import (
    artifact_scaffold_json_path,
    artifact_scaffold_xml_path,
    artifact_scaffold_yaml_path,
)
from xscore.shared.models import (
    BBox,
    ExamImage,
    ExamLayout,
    ExamScaffold,
    McAnswerOption,
    Question,
    WritingArea,
    gradable_questions,
)
from xscore.scaffold.scaffold_markdown import (
    write_scaffold_markdown,
    write_short_scaffold_markdown,
)


SCHEMA_VERSION = 19


# ---------------------------------------------------------------------------
# JSON (de)serialization
# ---------------------------------------------------------------------------

# Keep cache JSON readable: 1 fractional digit is sufficient for PDF-point coordinates.
_JSON_COORD_DECIMALS = 1


def _round_coord(v: float) -> float:
    return round(float(v), _JSON_COORD_DECIMALS)


def _bbox_to_dict(b: BBox) -> dict:
    return {
        "x0": _round_coord(b.x0),
        "y0": _round_coord(b.y0),
        "x1": _round_coord(b.x1),
        "y1": _round_coord(b.y1),
        "page": b.page,
    }


def _bbox_from_dict(d: dict) -> BBox:
    return BBox(
        float(d["x0"]),
        float(d["y0"]),
        float(d["x1"]),
        float(d["y1"]),
        int(d["page"]),
    )


def _img_to_dict(im: ExamImage) -> dict:
    return {"bbox": _bbox_to_dict(im.bbox), "path": im.path}


def _img_from_dict(d: dict) -> ExamImage:
    return ExamImage(bbox=_bbox_from_dict(d["bbox"]), path=d["path"])


def _wa_to_dict(w: WritingArea) -> dict:
    return {"bbox": _bbox_to_dict(w.bbox), "kind": w.kind}


def _wa_from_dict(d: dict) -> WritingArea:
    return WritingArea(bbox=_bbox_from_dict(d["bbox"]), kind=d["kind"])


def question_to_dict(q: Question) -> dict[str, Any]:
    """Serialize for cache JSON; omit nulls and empty collections (sparse)."""
    opts_dicts = [{"letter": o.letter, "text": o.text} for o in q.answer_options]
    d: dict[str, Any] = {
        "number": q.number,
        "question_type": q.question_type,
        "text": q.text,
        "marks": q.marks,
        "page": q.page,
        "subpage_row": q.subpage_row,
        "subpage_col": q.subpage_col,
    }
    if q.bbox.x0 or q.bbox.y0 or q.bbox.x1 or q.bbox.y1:
        d["bbox"] = _bbox_to_dict(q.bbox)
    if opts_dicts:
        d["answer_options"] = opts_dicts
    if q.equation_blank_bboxes:
        d["equation_blank_bboxes"] = [_bbox_to_dict(b) for b in q.equation_blank_bboxes]
    if q.images:
        d["images"] = [_img_to_dict(i) for i in q.images]
    if q.writing_areas:
        d["writing_areas"] = [_wa_to_dict(w) for w in q.writing_areas]
    if q.subquestions:
        d["subquestions"] = [question_to_dict(s) for s in q.subquestions]
    if q.correct_answer is not None and str(q.correct_answer).strip():
        d["correct_answer"] = q.correct_answer
    if q.question_type != "multiple_choice" and q.marking_criteria is not None and str(q.marking_criteria).strip():
        d["marking_criteria"] = q.marking_criteria
    if q.reasoning is not None and str(q.reasoning).strip():
        d["reasoning"] = q.reasoning
    if q.answer_images:
        d["answer_images"] = [_img_to_dict(i) for i in q.answer_images]
    return d


def question_from_dict(d: dict) -> Question:
    # Migrate v1 cache (AI scaffold)
    text = d.get("text")
    if text is None:
        text = d.get("content_summary", "")
    bbox_d = d.get("bbox")
    if not bbox_d:
        bbox_d = {"x0": 0.0, "y0": 0.0, "x1": 0.0, "y1": 0.0, "page": 1}
    ao = [
        McAnswerOption(letter=str(x["letter"]), text=str(x.get("text") or ""))
        for x in (d.get("answer_options") or [])
        if isinstance(x, dict) and x.get("letter")
    ]
    ca = d.get("correct_answer")
    if ca is None or (isinstance(ca, str) and not str(ca).strip()):
        # Migrate older caches that stored answer_key_text instead of correct_answer
        leg = d.get("answer_key_text")
        if leg and str(leg).strip():
            ca = str(leg).strip()
    page = int(d.get("page") or d.get("bbox", {}).get("page", 0))
    return Question(
        number=str(d["number"]),
        question_type=d.get("question_type", "short_answer"),
        text=text,
        marks=int(d.get("marks", 1)),
        bbox=_bbox_from_dict(bbox_d),
        page=page,
        subpage_row=int(d.get("subpage_row", 1)),
        subpage_col=int(d.get("subpage_col", 1)),
        equation_blank_bboxes=[_bbox_from_dict(x) for x in d.get("equation_blank_bboxes") or []],
        images=[_img_from_dict(x) for x in d.get("images") or []],
        writing_areas=[_wa_from_dict(x) for x in d.get("writing_areas") or []],
        subquestions=[question_from_dict(s) for s in d.get("subquestions") or []],
        correct_answer=ca,
        marking_criteria=d.get("marking_criteria"),
        reasoning=d.get("reasoning"),
        answer_images=[_img_from_dict(x) for x in d.get("answer_images") or []],
        answer_options=ao,
    )


# ---------------------------------------------------------------------------
# Cache paths and validity
# ---------------------------------------------------------------------------

def _legacy_cache_path(folder: Path) -> Path:
    """Pre-layout: cache lived at the exam folder root."""
    return folder / "scaffold_cache.json"


def _legacy_scaffold_subdir_cache(folder: Path) -> Path:
    return folder / "scaffolds" / "scaffold_cache.json"


def _legacy_flat_artifact_scaffold_cache_path(artifact_dir: Path) -> Path:
    """Older runs stored the cache as ``scaffold_cache.json`` in the run folder."""
    return artifact_dir / "scaffold_cache.json"


def _legacy_artifact_scaffold_subdir_cache_path(artifact_dir: Path) -> Path:
    """Older layout: cache lived under ``scaffolds/`` inside *artifact_dir*."""
    return artifact_dir / "scaffolds" / "scaffold_cache.json"


def _effective_cache_path(folder: Path, artifact_dir: Path) -> Path | None:
    for p in (
        artifact_scaffold_yaml_path(artifact_dir),   # primary
        artifact_scaffold_xml_path(artifact_dir),    # legacy
        artifact_scaffold_json_path(artifact_dir),
        _legacy_flat_artifact_scaffold_cache_path(artifact_dir),
        _legacy_artifact_scaffold_subdir_cache_path(artifact_dir),
        _legacy_scaffold_subdir_cache(folder),
        _legacy_cache_path(folder),
    ):
        if p.is_file():
            return p
    return None


def _cache_path_under_exam_folder(path: Path, exam_folder: Path) -> bool:
    try:
        path.resolve().relative_to(exam_folder.resolve())
        return True
    except ValueError:
        return False


def _migrate_scaffold_cache_to_artifact(
    exam_folder: Path, artifact_dir: Path, scaffold: ExamScaffold
) -> None:
    """Copy scaffold JSON + images into *artifact_dir* and remove legacy copies in *exam_folder*."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _save_cache(artifact_dir, scaffold)
    src_img = exam_folder / "scaffold_images"
    dst_img = artifact_dir / "scaffold_images"
    if src_img.is_dir():
        if dst_img.exists():
            shutil.rmtree(dst_img)
        shutil.copytree(src_img, dst_img)
    _clear_legacy_scaffold_outputs(exam_folder)


def _clear_legacy_scaffold_outputs(exam_folder: Path) -> None:
    for p in (_legacy_scaffold_subdir_cache(exam_folder), _legacy_cache_path(exam_folder)):
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    leg_img = exam_folder / "scaffold_images"
    if leg_img.is_dir():
        shutil.rmtree(leg_img, ignore_errors=True)
    leg_sd = exam_folder / "scaffolds"
    if leg_sd.is_dir():
        try:
            if not any(leg_sd.iterdir()):
                leg_sd.rmdir()
        except OSError:
            pass


def _load_cache(folder: Path, artifact_dir: Path) -> ExamScaffold:
    path = _effective_cache_path(folder, artifact_dir)
    if path is None:
        raise FileNotFoundError(f"No scaffold cache for {folder}")
    if path.suffix == ".yaml":
        return _load_cache_yaml(path)
    if path.suffix == ".xml":
        return _load_cache_xml(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            "scaffold cache schema_version mismatch — rebuild required "
            f"(got {data.get('schema_version')!r}, need {SCHEMA_VERSION})"
        )
    questions = [question_from_dict(q) for q in data["questions"]]
    total = int(data.get("total_marks", 0))
    if not total and questions:
        total = sum(q.marks for q in gradable_questions(questions))
    layout_d = data.get("layout") or {}
    return ExamScaffold(
        questions=questions,
        total_marks=total,
        page_count=int(data.get("page_count", 0)),
        raw_description=data.get("raw_description", ""),
        layout=ExamLayout(
            rows=int(layout_d.get("rows", 1)),
            cols=int(layout_d.get("cols", 1)),
        ),
    )


# ---------------------------------------------------------------------------
# XML (de)serialization
# ---------------------------------------------------------------------------

def _criterion_str_to_elements(criteria_str: str) -> list[ET.Element]:
    """Convert a LaTeX-formatted marking criteria block → single <criterion mark=""> element."""
    text = criteria_str.strip()
    if not text:
        return []
    el = ET.Element("criterion")
    el.set("mark", "")
    el.text = text
    return [el]


def _question_to_xml_element(q: Question) -> ET.Element:
    el = ET.Element("question")
    el.set("number", q.number)
    el.set("type", q.question_type)
    el.set("page", str(q.page or (q.bbox.page if q.bbox else 1)))
    el.set("subpage_row", str(q.subpage_row))
    el.set("subpage_col", str(q.subpage_col))
    el.set("marks", str(q.marks))
    if q.correct_answer is not None and str(q.correct_answer).strip():
        el.set("correct_answer", str(q.correct_answer))
    text_el = ET.SubElement(el, "text")
    text_el.text = q.text or ""
    for opt in (q.answer_options or []):
        opt_el = ET.SubElement(el, "option")
        opt_el.set("letter", opt.letter)
        opt_el.text = opt.text
    if q.question_type != "multiple_choice" and q.marking_criteria and str(q.marking_criteria).strip():
        for crit_el in _criterion_str_to_elements(str(q.marking_criteria)):
            el.append(crit_el)
    if q.reasoning and str(q.reasoning).strip():
        reasoning_el = ET.SubElement(el, "reasoning")
        reasoning_el.text = str(q.reasoning)
    for sub in (q.subquestions or []):
        el.append(_question_to_xml_element(sub))
    return el


def _compute_pdf_sha256(path: Path) -> str:
    """SHA-256 hex digest of a file's bytes; empty string on read error."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _read_cached_source_hashes(cache_path: Path) -> dict[str, str]:
    """Return ``{filename: sha256}`` recorded with the cache, empty on miss.

    YAML caches store hashes under a top-level ``sources`` list. Legacy XML
    caches store them as ``<basename>_sha256`` attributes on the root element.
    Legacy JSON caches return ``{}`` so callers fall back to the older
    mtime-based validity check.
    """
    if cache_path.suffix == ".yaml":
        try:
            with open(cache_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError):
            return {}
        out: dict[str, str] = {}
        for entry in (data.get("sources") or []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("file")
            sha = entry.get("sha256")
            if name and sha:
                out[str(name)] = str(sha)
        return out
    if cache_path.suffix != ".xml":
        return {}
    try:
        root = ET.parse(cache_path).getroot()
    except (ET.ParseError, OSError):
        return {}
    out = {}
    suffix = "_sha256"
    for k, v in root.attrib.items():
        if k.endswith(suffix) and v:
            name = k[: -len(suffix)]
            if name:
                out[name] = v
    return out


def _scaffold_to_xml(
    scaffold: ExamScaffold,
    students: list[str] | None = None,
    source_hashes: dict[str, str] | None = None,
) -> str:
    """Serialise ExamScaffold to an XML string.

    *source_hashes* maps source-PDF basename → SHA-256 hex digest; each entry
    is written as a ``<basename>_sha256`` attribute on the root so the next
    cache-validity check can compare current file content against the
    snapshot taken at save time. mtime is too fragile (cp -p / touch).
    """
    root = ET.Element("scaffold")
    root.set("schema_version", str(SCHEMA_VERSION))
    root.set("total_marks", str(scaffold.total_marks))
    root.set("page_count", str(scaffold.page_count))
    root.set("rows", str(scaffold.layout.rows))
    root.set("cols", str(scaffold.layout.cols))
    for name, h in (source_hashes or {}).items():
        if h:
            root.set(f"{name}_sha256", h)
    if students:
        studs_el = ET.SubElement(root, "students")
        for s in students:
            s_el = ET.SubElement(studs_el, "student")
            s_el.text = s
    for q in scaffold.questions:
        root.append(_question_to_xml_element(q))
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def _question_from_xml_element(el: ET.Element) -> Question:
    page = int(el.get("page", 1))
    text_el = el.find("text")
    text = (text_el.text or "").strip() if text_el is not None else ""
    answer_options = [
        McAnswerOption(letter=o.get("letter", ""), text=(o.text or "").strip())
        for o in el.findall("option")
    ]
    criterion_parts = []
    for c in el.findall("criterion"):
        mark = c.get("mark", "")
        ctext = (c.text or "").strip()
        if ctext:
            criterion_parts.append(f"[{mark}] {ctext}" if mark else ctext)
    marking_criteria: str | None = "\n".join(criterion_parts) or None
    reasoning_el = el.find("reasoning")
    reasoning: str | None = (
        (reasoning_el.text or "").strip() or None
        if reasoning_el is not None else None
    )
    subquestions = [_question_from_xml_element(sub) for sub in el.findall("question")]
    return Question(
        number=el.get("number", ""),
        question_type=el.get("type", "short_answer"),
        text=text,
        marks=int(el.get("marks", 0)),
        bbox=BBox(0.0, 0.0, 0.0, 0.0, page),
        page=page,
        subpage_row=int(el.get("subpage_row", 1)),
        subpage_col=int(el.get("subpage_col", 1)),
        answer_options=answer_options,
        subquestions=subquestions,
        correct_answer=el.get("correct_answer") or None,
        marking_criteria=marking_criteria,
        reasoning=reasoning,
    )


def _load_cache_xml(path: Path) -> ExamScaffold:
    tree = ET.parse(path)
    root = tree.getroot()
    if root.get("schema_version") != str(SCHEMA_VERSION):
        raise ValueError(
            f"scaffold XML schema_version mismatch — rebuild required "
            f"(got {root.get('schema_version')!r}, need {str(SCHEMA_VERSION)!r})"
        )
    questions = [_question_from_xml_element(el) for el in root.findall("question")]
    total = int(root.get("total_marks", 0))
    if not total and questions:
        total = sum(q.marks for q in gradable_questions(questions))
    return ExamScaffold(
        questions=questions,
        total_marks=total,
        page_count=int(root.get("page_count", 0)),
        layout=ExamLayout(
            rows=int(root.get("rows", 1)),
            cols=int(root.get("cols", 1)),
        ),
    )


# ---------------------------------------------------------------------------
# YAML (de)serialization — primary format
# ---------------------------------------------------------------------------

def _question_to_yaml_dict(q: Question) -> dict:
    """Mirror the XML schema in dict form, suitable for ``yaml.safe_dump``.

    Keeps the same field set as ``_question_to_xml_element`` for round-trip
    equivalence between formats. Sparse: omits empty/null fields.
    """
    d: dict[str, Any] = {
        "number": q.number,
        "type": q.question_type,
        "page": q.page or (q.bbox.page if q.bbox else 1),
        "subpage_row": q.subpage_row,
        "subpage_col": q.subpage_col,
        "marks": q.marks,
    }
    if q.correct_answer is not None and str(q.correct_answer).strip():
        d["correct_answer"] = str(q.correct_answer)
    d["text"] = q.text or ""
    if q.answer_options:
        d["options"] = [{"letter": o.letter, "text": o.text} for o in q.answer_options]
    if q.question_type != "multiple_choice" and q.marking_criteria and str(q.marking_criteria).strip():
        d["marking_criteria"] = str(q.marking_criteria)
    if q.reasoning and str(q.reasoning).strip():
        d["reasoning"] = str(q.reasoning)
    if q.subquestions:
        d["subquestions"] = [_question_to_yaml_dict(s) for s in q.subquestions]
    return d


def _question_from_yaml_dict(d: dict) -> Question:
    page = int(d.get("page") or 1)
    answer_options = [
        McAnswerOption(letter=str(o.get("letter", "")), text=str(o.get("text") or ""))
        for o in (d.get("options") or [])
        if isinstance(o, dict) and o.get("letter")
    ]
    marking_criteria_raw = d.get("marking_criteria")
    marking_criteria: str | None = (
        str(marking_criteria_raw).strip() or None if marking_criteria_raw is not None else None
    )
    reasoning_raw = d.get("reasoning")
    reasoning: str | None = (
        str(reasoning_raw).strip() or None if reasoning_raw is not None else None
    )
    subquestions = [_question_from_yaml_dict(s) for s in (d.get("subquestions") or []) if isinstance(s, dict)]
    return Question(
        number=str(d.get("number", "")),
        question_type=d.get("type", "short_answer"),
        text=str(d.get("text") or ""),
        marks=int(d.get("marks", 0)),
        bbox=BBox(0.0, 0.0, 0.0, 0.0, page),
        page=page,
        subpage_row=int(d.get("subpage_row", 1)),
        subpage_col=int(d.get("subpage_col", 1)),
        answer_options=answer_options,
        subquestions=subquestions,
        correct_answer=(d.get("correct_answer") or None),
        marking_criteria=marking_criteria,
        reasoning=reasoning,
    )


def _scaffold_to_yaml(
    scaffold: ExamScaffold,
    students: list[str] | None = None,
    source_hashes: dict[str, str] | None = None,
) -> str:
    """Serialise ExamScaffold to a YAML string.

    Filenames in *source_hashes* live as YAML string values under
    ``sources[].file`` — never as keys — so they tolerate spaces, leading
    digits, dots, and any other characters that are illegal in XML attribute
    names. This is the structural fix for the malformed-attribute bug in the
    legacy XML cache writer.
    """
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "total_marks": scaffold.total_marks,
        "page_count": scaffold.page_count,
        "layout": {"rows": scaffold.layout.rows, "cols": scaffold.layout.cols},
    }
    if source_hashes:
        payload["sources"] = [
            {"file": name, "sha256": h}
            for name, h in source_hashes.items()
            if h
        ]
    if students:
        payload["students"] = list(students)
    payload["questions"] = [_question_to_yaml_dict(q) for q in scaffold.questions]
    return yaml.safe_dump(
        payload,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=10**9,
    )


def _load_cache_yaml(path: Path) -> ExamScaffold:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"scaffold YAML cache malformed: {path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"scaffold YAML schema_version mismatch — rebuild required "
            f"(got {data.get('schema_version')!r}, need {SCHEMA_VERSION!r})"
        )
    questions = [
        _question_from_yaml_dict(q)
        for q in (data.get("questions") or [])
        if isinstance(q, dict)
    ]
    total = int(data.get("total_marks", 0))
    if not total and questions:
        total = sum(q.marks for q in gradable_questions(questions))
    layout_d = data.get("layout") or {}
    return ExamScaffold(
        questions=questions,
        total_marks=total,
        page_count=int(data.get("page_count", 0)),
        layout=ExamLayout(
            rows=int(layout_d.get("rows", 1)),
            cols=int(layout_d.get("cols", 1)),
        ),
    )


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def _scaffold_to_payload(scaffold: ExamScaffold, students: list[str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "layout": {"rows": scaffold.layout.rows, "cols": scaffold.layout.cols},
        "students": students or [],
        "questions": [question_to_dict(q) for q in scaffold.questions],
        "total_marks": scaffold.total_marks,
        "page_count": scaffold.page_count,
        "raw_description": scaffold.raw_description,
    }


def _save_cache(
    artifact_dir: Path,
    scaffold: ExamScaffold,
    students: list[str] | None = None,
    *,
    source_hashes: dict[str, str] | None = None,
) -> None:
    out = artifact_scaffold_yaml_path(artifact_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_scaffold_to_yaml(scaffold, students, source_hashes), encoding="utf-8")
    # Clean up any sibling legacy report.xml from a pre-migration run so the
    # cache directory has a single canonical file.
    old_xml = artifact_scaffold_xml_path(artifact_dir)
    if old_xml.is_file():
        try:
            old_xml.unlink()
        except OSError:
            pass
    payload = _scaffold_to_payload(scaffold, students)
    write_scaffold_markdown(artifact_dir, payload)
    write_short_scaffold_markdown(artifact_dir, payload)
    for old_name in (artifact_dir / "6_scaffold.json", artifact_dir / "5_scaffold.json", artifact_dir / "1_scaffold.json"):
        if old_name.is_file():
            try:
                old_name.unlink()
            except OSError:
                pass
    flat_old = _legacy_flat_artifact_scaffold_cache_path(artifact_dir)
    if flat_old.is_file():
        try:
            flat_old.unlink()
        except OSError:
            pass
    leg = _legacy_artifact_scaffold_subdir_cache_path(artifact_dir)
    if leg.is_file():
        try:
            leg.unlink()
        except OSError:
            pass
        try:
            sd = artifact_dir / "scaffolds"
            if sd.is_dir() and not any(sd.iterdir()):
                sd.rmdir()
        except OSError:
            pass

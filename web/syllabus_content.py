"""Syllabus subtopic content extractor + loader for the Learn page.

One-shot CLI (`python -m web.syllabus_content`) per-page-parallel-extracts
each subtopic's learning-objective content from the syllabus PDF and writes
it as markdown to ``syllabi/content/<subject_key>/<number>.md``. The web
route loads those files at request time via :func:`load_content`.

Per-page extraction (rather than whole-PDF) keeps Gemini's visual attention
focused on a single table layout, which is critical for correctly preserving
the Core/Supplement row alignment in IGCSE syllabi.

Format-aware dispatch:

- **IGCSE** syllabi use two-column tables (``Core`` / ``Supplement``,
  ``Subject content`` / ``Notes and examples``, etc.). Some subtopics have
  N.M.K sub-sections (Physics 1.5.1 / 1.5.2 / 1.5.3) — each is its own
  small table, all rendered as H3 sub-blocks under the parent N.M.
- **A-Level** syllabi use a single-column numbered list of learning
  outcomes per subtopic — no two-column structure.

Gemini imports are deferred to the extraction helpers and the ``__main__``
block, so importing this module from the web route does not pull in
``google-genai``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal

from eXercise.config import EXAM_ROOT_BY_KEY, SYLLABI_DIR

from web.syllabus_topics import (
    current_syllabus_pdf,
    load_topics,
    syllabus_level,
)

CONTENT_DIR = SYLLABI_DIR / "content"

# Accept N (topic-only, IGCSE CS topics with no subtopics), N.M (depth-2),
# and N.M.K (depth-3 sub-sections) from Gemini. Optional Core/Extended
# letter prefix (Math: ``C1.1``).
_NUMBER_RE = re.compile(r"^[A-Z]*\d+(?:\.\d+){0,2}$")
_DEPTH2_OF = re.compile(r"^([A-Z]*\d+\.\d+)(?:\.\d+)?$")
_DEPTH3_RE = re.compile(r"^[A-Z]*\d+\.\d+\.\d+$")


# ── Loader (web route reads these — no in-process cache) ─────────────────
def load_content(subject_key: str, subtopic_number: str) -> str | None:
    """Return the markdown content for one subtopic, or ``None`` if missing."""
    path = CONTENT_DIR / subject_key / f"{subtopic_number}.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# ── Markdown writers ─────────────────────────────────────────────────────
def _cell(text: Any) -> str:
    """Escape a cell value for use inside a markdown pipe table."""
    s = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return s.replace("|", "\\|")


def _render_table(left_header: str, right_header: str, rows: list[dict]) -> list[str]:
    """Render a single Core/Supplement table as markdown pipe lines.

    Returns the list of lines (no trailing newline). Empty rows are dropped.
    The caller appends a blank line after if needed.
    """
    lh = _cell(left_header) or "Core"
    rh = _cell(right_header) or "Supplement"
    out = [f"| {lh} | {rh} |", "| --- | --- |"]
    for row in rows:
        left = _cell(row.get("left", ""))
        right = _cell(row.get("right", ""))
        if not left and not right:
            continue
        out.append(f"| {left} | {right} |")
    return out


def to_markdown_igcse_flat(
    number: str,
    title: str,
    left_header: str,
    right_header: str,
    rows: list[dict],
) -> str:
    """IGCSE subtopic without sub-sections: H1 + single table."""
    lines = [f"# {number} {title}", ""]
    lines += _render_table(left_header, right_header, rows)
    return "\n".join(lines) + "\n"


def to_markdown_igcse_sections(
    number: str,
    title: str,
    sections: list[dict],
) -> str:
    """IGCSE subtopic with N.M.K sub-sections: H1 + H3 per sub-section."""
    lines = [f"# {number} {title}", ""]
    for sec in sections:
        sec_num = str(sec.get("number") or "").strip()
        sec_title = str(sec.get("section_title") or "").strip()
        heading = " ".join(p for p in [sec_num, sec_title] if p)
        lines.append(f"### {heading}".rstrip())
        lines.append("")
        lines += _render_table(
            sec.get("left_header", ""),
            sec.get("right_header", ""),
            sec.get("rows") or [],
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_markdown_a_level(number: str, title: str, items: list[str]) -> str:
    """A-Level subtopic: H1 + numbered ordered list."""
    lines = [f"# {number} {title}", ""]
    for i, item in enumerate(items, 1):
        text = str(item or "").replace("\r", " ").replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"{i}. {text}")
    return "\n".join(lines) + "\n"


# ── Pre-pass: find "Subject content" page range ──────────────────────────
_PRE_PASS_PROMPT = """\
You are given a Cambridge syllabus PDF. Return the inclusive 1-indexed
page range of the section titled \"Subject content\" (typically section 3).
The start page is where the \"Subject content\" section heading appears.
The end page is the last page of subject content, just before any later
sections such as \"Other information\", \"Mathematical requirements\",
appendices, glossary, or assessment objectives.

If unsure, prefer to over-include (slightly larger range) rather than
truncate.

Return JSON only, matching:

{\"start\": <int>, \"end\": <int>}
"""


def _find_content_page_range(
    client: Any,
    pdf_path: Path,
    *,
    model_name: str,
    thinking_tokens: int | None,
) -> tuple[int, int]:
    from google.genai import types as gai_types

    from eXercise.ai_client import (
        build_gemini_thinking_config,
        gemini_pdf_part,
        split_gemini_response,
        strip_json_fences,
    )
    from eXercise.api_retry import retry_api_call

    gen_config_kwargs: dict[str, Any] = {
        "system_instruction": _PRE_PASS_PROMPT,
        "max_output_tokens": 256,
        "response_mime_type": "application/json",
    }
    if thinking_tokens is not None:
        gen_config_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
    gen_config = gai_types.GenerateContentConfig(**gen_config_kwargs)

    response = retry_api_call(
        lambda: client.models.generate_content(
            model=model_name,
            contents=[gemini_pdf_part(client, pdf_path, label=f"page-range {pdf_path.stem}")],
            config=gen_config,
        ),
        label=f"content page range — {pdf_path.name}",
    )
    raw, _ = split_gemini_response(response)
    data = json.loads(strip_json_fences(raw))
    start = int(data["start"])
    end = int(data["end"])
    if start < 1 or end < start:
        raise RuntimeError(f"{pdf_path.name}: invalid page range {start}–{end}")
    return start, end


# ── Per-page prompts (one per format) ────────────────────────────────────
_IGCSE_PAGE_PROMPT = """\
You are given ONE page of a Cambridge IGCSE syllabus PDF. Identify every
two-column subtopic-content table visible on this page and extract its
rows.

NUMBERING — the table heading carries one of three forms:

- N.M (e.g. \"1.2\" or \"C1.3\" for Mathematics) — a normal subtopic.
- N.M.K (e.g. \"1.5.1\") — a sub-section inside a parent subtopic (the
  page may show \"1.5.1 Effects of forces\", \"1.5.2 Turning effect of
  forces\", each with its own small table).
- N (e.g. \"9\") — a topic-level table for syllabi where a topic has no
  N.M subdivisions (Cambridge IGCSE Computer Science topics 7–10 do this).

For each table set:
- `number`: the exact heading number (\"1.2\", \"1.5.1\", or \"9\").
- `section_title`: the subtopic/sub-section/topic title text that appears
  next to the number (e.g. \"Effects of forces\" for 1.5.1, \"Motion\"
  for 1.2, \"Databases\" for 9).
- `left_header` and `right_header`: read the column headers verbatim
  from the page. If the LEFT column has NO explicit header text (only
  the peach-banded subtopic title in that area, as Cambridge IGCSE
  Mathematics does), use the literal string \"Subject content\" as
  `left_header`. Common variants: \"Core\" / \"Supplement\";
  \"Candidates should be able to:\" / \"Notes and guidance\"; \"Subject
  content\" / \"Notes and examples\".
- `rows`: the table body as a list of `{left, right}` pairs.

CRITICAL — preserve visual row alignment.

- When the page shows a left-column item and a right-column item on the
  same horizontal row, they MUST share an output row.
- When a left-column item has no right counterpart on the same row,
  leave the right cell empty (\"\").
- When a right-column item has no left counterpart on the same row,
  leave the left cell empty.
- The right column often qualifies or extends the specific left item it
  shares a row with — preserving this pairing is essential.

If a table starts on this page and is cut off at the bottom, OR continues
from a previous page (header repeats with \"continued\"), return only the
rows visible on THIS page — do NOT invent continuation rows.

Each cell must be a single line: if a syllabus item wraps across multiple
lines in the PDF, join with spaces. Preserve item numbering verbatim
(e.g. \"3 Recall and use the equation ...\"). Preserve equations as
LaTeX (`$v=s/t$` inline, `$$ ... $$` display).

Return JSON only, matching:

{\"subtopics\": [
  {\"number\": \"1.2\", \"section_title\": \"Motion\",
   \"left_header\": \"Core\", \"right_header\": \"Supplement\",
   \"rows\": [{\"left\": \"...\", \"right\": \"...\"}]}
]}

If no subtopic table is visible on this page (page shows only an intro
paragraph, a section heading, or is between subtopics), return
{\"subtopics\": []}.
"""


_A_LEVEL_PAGE_PROMPT = """\
You are given ONE page of a Cambridge A-Level (or AS & A-Level) syllabus
PDF. Identify every subtopic-content section visible on this page and
extract its numbered learning outcomes.

Cambridge A-Level subject content uses a SINGLE-COLUMN format: a
peach-banded subtopic heading like \"1.2 SI units\" is followed by a
\"Candidates should be able to:\" or \"Learning outcomes\" header and a
numbered list of outcomes (1. , 2. , 3. , …). There is NO two-column
Core/Supplement table.

For each subtopic section visible on this page:
- `number`: the heading number, e.g. \"1.2\" or \"1.5\".
- `section_title`: the subtopic title text next to the number
  (e.g. \"SI units\").
- `items`: an array of strings, one per numbered learning outcome.
  Preserve the verbatim outcome text but drop the leading \"1. \", \"2. \"
  numbering (the markdown writer adds it back). Equations stay as LaTeX
  (`$v = s/t$` inline, `$$ ... $$` display).

If an outcome wraps across multiple lines, join with spaces. If a
subtopic's list starts on this page and continues on the next, return
only the outcomes visible on THIS page — do NOT invent continuations.

Ignore section dividers like \"Physical chemistry\", \"Inorganic
chemistry\" — these are not subtopics, just labels.

Return JSON only, matching:

{\"subtopics\": [
  {\"number\": \"1.2\", \"section_title\": \"SI units\",
   \"items\": [\"recall the following SI base quantities ...\",
              \"express derived units ...\"]}
]}

If no subtopic content is visible on this page, return
{\"subtopics\": []}.
"""


def _extract_page(
    client: Any,
    page_pdf: Path,
    page_num: int,
    level: Literal["igcse", "a_level"],
    *,
    model_name: str,
    thinking_tokens: int | None,
    max_tokens: int | None,
) -> list[dict]:
    """Per-page extraction. Dispatches prompt + parsing on level.

    Returns a list of subtopic dicts whose shape depends on level:

    - IGCSE: ``[{number, section_title, left_header, right_header, rows}, …]``
    - A-Level: ``[{number, section_title, items}, …]``
    """
    from google.genai import types as gai_types

    from eXercise.ai_client import (
        build_gemini_thinking_config,
        gemini_pdf_part,
        split_gemini_response,
        strip_json_fences,
    )
    from eXercise.api_retry import retry_api_call

    prompt = _A_LEVEL_PAGE_PROMPT if level == "a_level" else _IGCSE_PAGE_PROMPT
    gen_config_kwargs: dict[str, Any] = {
        "system_instruction": prompt,
        "max_output_tokens": max_tokens or 16384,
        "response_mime_type": "application/json",
    }
    if thinking_tokens is not None:
        gen_config_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
    gen_config = gai_types.GenerateContentConfig(**gen_config_kwargs)

    response = retry_api_call(
        lambda: client.models.generate_content(
            model=model_name,
            contents=[gemini_pdf_part(client, page_pdf, label=f"p{page_num}")],
            config=gen_config,
        ),
        label=f"content p{page_num} ({level})",
    )
    raw, _thinking = split_gemini_response(response)
    data = json.loads(strip_json_fences(raw))

    out: list[dict] = []
    for sub in data.get("subtopics") or []:
        if not isinstance(sub, dict):
            continue
        num = str(sub.get("number", "")).strip()
        if not _NUMBER_RE.match(num):
            continue
        if level == "a_level":
            items_in = sub.get("items") or []
            if not isinstance(items_in, list):
                continue
            items_out = [
                str(it or "").strip()
                for it in items_in
                if str(it or "").strip()
            ]
            out.append({
                "number": num,
                "section_title": str(sub.get("section_title", "") or "").strip(),
                "items": items_out,
            })
        else:
            rows_in = sub.get("rows") or []
            if not isinstance(rows_in, list):
                continue
            rows_out: list[dict] = []
            for row in rows_in:
                if not isinstance(row, dict):
                    continue
                rows_out.append({
                    "left": str(row.get("left", "") or "").strip(),
                    "right": str(row.get("right", "") or "").strip(),
                })
            out.append({
                "number": num,
                "section_title": str(sub.get("section_title", "") or "").strip(),
                "left_header": str(sub.get("left_header", "") or "").strip(),
                "right_header": str(sub.get("right_header", "") or "").strip(),
                "rows": rows_out,
            })
    return out


# ── PDF page splitter (lazy fitz import) ─────────────────────────────────
def _split_pages(pdf_path: Path, start: int, end: int, out_dir: Path) -> dict[int, Path]:
    """Write each page in ``[start, end]`` as a one-page PDF in ``out_dir``."""
    import fitz  # PyMuPDF

    src = fitz.open(pdf_path)
    try:
        if end > len(src):
            end = len(src)
        page_paths: dict[int, Path] = {}
        for page_num in range(start, end + 1):
            out = out_dir / f"page_{page_num:03d}.pdf"
            single = fitz.open()
            try:
                single.insert_pdf(src, from_page=page_num - 1, to_page=page_num - 1)
                single.save(str(out))
            finally:
                single.close()
            page_paths[page_num] = out
        return page_paths
    finally:
        src.close()


# ── Merge across pages ───────────────────────────────────────────────────
def _merge_pages_igcse(per_page: list[tuple[int, list[dict]]]) -> dict[str, dict]:
    """Merge IGCSE per-page results into a dict keyed by output-file number.

    Output dict shape:

        {
          "1.5": {"kind": "sections", "sections": [{number, section_title,
                  left_header, right_header, rows}, …]},
          "1.2": {"kind": "flat", "section_title", "left_header",
                  "right_header", "rows"},
          "9":   {"kind": "topic", "section_title", "left_header",
                  "right_header", "rows"},
        }

    Keys are determined by Gemini's number field:
      - N.M.K  → grouped under N.M, kind="sections"
      - N.M    → kind="flat" (unless an N.M.K entry already promoted it to
                 sections)
      - N      → kind="topic"
    """
    merged: dict[str, dict] = {}
    for _page_num, subs in sorted(per_page, key=lambda x: x[0]):
        for sub in subs:
            num = sub["number"]
            is_depth3 = bool(_DEPTH3_RE.match(num))
            m = _DEPTH2_OF.match(num)
            depth2_key = m.group(1) if m else None
            is_topic_only = depth2_key is None  # number has no dot (e.g. "9")

            if is_topic_only:
                entry = merged.setdefault(num, {
                    "kind": "topic",
                    "section_title": sub.get("section_title", ""),
                    "left_header": sub.get("left_header", ""),
                    "right_header": sub.get("right_header", ""),
                    "rows": [],
                })
                entry["rows"].extend(sub.get("rows") or [])
                if not entry["left_header"] and sub.get("left_header"):
                    entry["left_header"] = sub["left_header"]
                if not entry["right_header"] and sub.get("right_header"):
                    entry["right_header"] = sub["right_header"]
                if not entry["section_title"] and sub.get("section_title"):
                    entry["section_title"] = sub["section_title"]
                continue

            if is_depth3:
                # Promote (or initialize) entry to "sections" kind.
                entry = merged.get(depth2_key)
                if entry is None or entry["kind"] != "sections":
                    # If we previously saw N.M as flat, fold those rows into
                    # a synthetic first section so we don't lose them. This
                    # is rare — usually Gemini emits either flat or sections
                    # consistently for a given parent.
                    if entry is not None and entry["kind"] == "flat" and entry.get("rows"):
                        synth_section = {
                            "number": depth2_key,
                            "section_title": entry.get("section_title", ""),
                            "left_header": entry.get("left_header", ""),
                            "right_header": entry.get("right_header", ""),
                            "rows": entry["rows"],
                        }
                        merged[depth2_key] = {"kind": "sections", "sections": [synth_section]}
                    else:
                        merged[depth2_key] = {"kind": "sections", "sections": []}
                    entry = merged[depth2_key]
                # Find existing section by number to extend; else append.
                existing = next(
                    (s for s in entry["sections"] if s["number"] == num),
                    None,
                )
                if existing is None:
                    entry["sections"].append({
                        "number": num,
                        "section_title": sub.get("section_title", ""),
                        "left_header": sub.get("left_header", ""),
                        "right_header": sub.get("right_header", ""),
                        "rows": list(sub.get("rows") or []),
                    })
                else:
                    existing["rows"].extend(sub.get("rows") or [])
                    if not existing["left_header"] and sub.get("left_header"):
                        existing["left_header"] = sub["left_header"]
                    if not existing["right_header"] and sub.get("right_header"):
                        existing["right_header"] = sub["right_header"]
                    if not existing["section_title"] and sub.get("section_title"):
                        existing["section_title"] = sub["section_title"]
                continue

            # Depth-2 N.M entry.
            entry = merged.get(depth2_key)
            if entry is None:
                merged[depth2_key] = {
                    "kind": "flat",
                    "section_title": sub.get("section_title", ""),
                    "left_header": sub.get("left_header", ""),
                    "right_header": sub.get("right_header", ""),
                    "rows": list(sub.get("rows") or []),
                }
            elif entry["kind"] == "flat":
                entry["rows"].extend(sub.get("rows") or [])
                if not entry["left_header"] and sub.get("left_header"):
                    entry["left_header"] = sub["left_header"]
                if not entry["right_header"] and sub.get("right_header"):
                    entry["right_header"] = sub["right_header"]
                if not entry["section_title"] and sub.get("section_title"):
                    entry["section_title"] = sub["section_title"]
            else:
                # Existing entry is "sections" (depth-3 already seen). Fold
                # this depth-2 entry's rows in as a synthetic section so we
                # don't lose them.
                entry["sections"].append({
                    "number": depth2_key,
                    "section_title": sub.get("section_title", ""),
                    "left_header": sub.get("left_header", ""),
                    "right_header": sub.get("right_header", ""),
                    "rows": list(sub.get("rows") or []),
                })

    # Sort sections within each "sections" entry by their N.M.K number.
    for entry in merged.values():
        if entry.get("kind") == "sections":
            entry["sections"].sort(key=lambda s: s["number"])
    return merged


def _merge_pages_a_level(per_page: list[tuple[int, list[dict]]]) -> dict[str, dict]:
    """Merge A-Level per-page results into ``{number: {kind, section_title, items}}``."""
    merged: dict[str, dict] = {}
    for _page_num, subs in sorted(per_page, key=lambda x: x[0]):
        for sub in subs:
            num = sub["number"]
            m = _DEPTH2_OF.match(num)
            if not m:
                # Skip topic-only (N) entries for A-Level — A-Level subjects
                # don't have topic-without-subtopics structure.
                continue
            key = m.group(1)
            entry = merged.setdefault(key, {
                "kind": "list",
                "section_title": sub.get("section_title", ""),
                "items": [],
            })
            entry["items"].extend(sub.get("items") or [])
            if not entry["section_title"] and sub.get("section_title"):
                entry["section_title"] = sub["section_title"]
    return merged


# ── Orchestrator ─────────────────────────────────────────────────────────
def extract_content(pdf_path: Path, level: Literal["igcse", "a_level"]) -> dict[str, dict]:
    """Pre-pass for page range, per-page parallel extraction, format-aware merge.

    Returns ``{number: payload}`` where payload shape depends on the kind
    (see :func:`_merge_pages_igcse` and :func:`_merge_pages_a_level`).
    """
    from eXercise.ai_client import make_gemini_native_client, parse_model_spec

    raw_spec = (
        os.environ.get("SYLLABUS_EXTRACT_MODEL", "").strip()
        or "gemini-3.5-flash, off"
    )
    model_name, thinking_tokens, max_tokens = parse_model_spec(raw_spec)
    if not model_name.startswith("gemini"):
        raise RuntimeError(
            f"SYLLABUS_EXTRACT_MODEL must be a Gemini model (got {model_name!r})."
        )

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set.")

    print(f"  pre-pass: finding subject-content page range ({level}) …", file=sys.stderr)
    start, end = _find_content_page_range(
        client, pdf_path,
        model_name=model_name, thinking_tokens=thinking_tokens,
    )
    print(f"  pages {start}–{end} ({end - start + 1} pages)", file=sys.stderr)

    workers = max(1, int(os.environ.get("SYLLABUS_EXTRACT_WORKERS", "16") or "16"))

    with tempfile.TemporaryDirectory(prefix="syllabus_pages_") as tmp:
        tmp_dir = Path(tmp)
        page_paths = _split_pages(pdf_path, start, end, tmp_dir)

        per_page: list[tuple[int, list[dict]]] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(
                    _extract_page,
                    client, page_path, page_num, level,
                    model_name=model_name,
                    thinking_tokens=thinking_tokens,
                    max_tokens=max_tokens,
                ): page_num
                for page_num, page_path in page_paths.items()
            }
            for fut in as_completed(futs):
                page_num = futs[fut]
                try:
                    subs = fut.result()
                    per_page.append((page_num, subs))
                except Exception as exc:
                    print(f"  ! page {page_num} failed: {exc}", file=sys.stderr)
                    per_page.append((page_num, []))

    if level == "a_level":
        return _merge_pages_a_level(per_page)
    return _merge_pages_igcse(per_page)


# ── Output writer + CLI ──────────────────────────────────────────────────
def _topic_title(topics_data: dict | None, number: str) -> str | None:
    """Find the title for a topic or subtopic by number from the topics YAML."""
    if not topics_data:
        return None
    for t in topics_data.get("topics") or []:
        if str(t.get("number")) == number:
            return t.get("title")
        for s in t.get("subtopics") or []:
            if str(s.get("number")) == number:
                return s.get("title")
    return None


def _payload_to_markdown(number: str, title: str, payload: dict) -> str | None:
    """Render a merged payload to its final markdown string.

    Returns ``None`` if the payload has no content (caller should skip writing
    the .md file rather than create one with an empty body).
    """
    kind = payload.get("kind")
    if kind == "flat":
        rows = payload.get("rows") or []
        if not any(r.get("left") or r.get("right") for r in rows):
            return None
        return to_markdown_igcse_flat(
            number, title,
            payload.get("left_header", ""),
            payload.get("right_header", ""),
            rows,
        )
    if kind == "topic":
        rows = payload.get("rows") or []
        if not any(r.get("left") or r.get("right") for r in rows):
            return None
        return to_markdown_igcse_flat(
            number, title,
            payload.get("left_header", ""),
            payload.get("right_header", ""),
            rows,
        )
    if kind == "sections":
        sections = payload.get("sections") or []
        if not any(
            any(r.get("left") or r.get("right") for r in (s.get("rows") or []))
            for s in sections
        ):
            return None
        return to_markdown_igcse_sections(number, title, sections)
    if kind == "list":
        items = [it for it in (payload.get("items") or []) if it]
        if not items:
            return None
        return to_markdown_a_level(number, title, items)
    return None


def _write_md(subject_key: str, number: str, md: str) -> Path:
    out_dir = CONTENT_DIR / subject_key
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{number}.md"
    out_path.write_text(md, encoding="utf-8")
    return out_path


def _expected_numbers(topics_data: dict) -> list[str]:
    """List of numbers that should produce a .md file.

    - Topics with subtopics → each subtopic's number.
    - Topics WITHOUT subtopics → the topic's own number (CS 9, 10).
    """
    out: list[str] = []
    for t in topics_data.get("topics") or []:
        subs = t.get("subtopics") or []
        if subs:
            out.extend(str(s["number"]) for s in subs if s.get("number"))
        else:
            num = t.get("number")
            if num is not None:
                out.append(str(num))
    return out


def _missing_numbers(subject_key: str, expected: list[str]) -> list[str]:
    subj_dir = CONTENT_DIR / subject_key
    if not subj_dir.exists():
        return list(expected)
    return [n for n in expected if not (subj_dir / f"{n}.md").exists()]


def _run_one(subject_key: str, *, force: bool, dry_run: bool) -> tuple[str, str]:
    topics_data = load_topics(subject_key)
    if topics_data is None:
        return ("error", f"{subject_key}: no topics YAML (run web.syllabus_topics first)")
    expected = _expected_numbers(topics_data)
    if not expected:
        return ("error", f"{subject_key}: topics YAML has no subtopics/topics to extract")

    missing = list(expected) if force else _missing_numbers(subject_key, expected)
    if not missing and not dry_run:
        return ("skip", f"{subject_key}: all {len(expected)} files present (use --force to re-extract)")

    pdf = current_syllabus_pdf(subject_key)
    if pdf is None:
        return ("error", f"{subject_key}: no syllabus PDF found in {SYLLABI_DIR}")

    level = syllabus_level(subject_key)
    target = list(missing) if missing else list(expected)
    print(
        f"{subject_key} ({level}): extracting from {pdf.name} "
        f"({len(target)}/{len(expected)} needed)",
        file=sys.stderr,
    )

    try:
        all_content = extract_content(pdf, level)
    except Exception as exc:
        return ("error", f"{subject_key}: extraction failed — {exc}")

    written: list[str] = []
    still_missing: list[str] = []
    skipped_empty: list[str] = []
    for num in target:
        title = _topic_title(topics_data, num) or num
        payload = all_content.get(num)
        if not payload:
            still_missing.append(num)
            continue
        md = _payload_to_markdown(num, title, payload)
        if md is None:
            skipped_empty.append(num)
            continue
        if dry_run:
            print(f"### {subject_key}/{num}.md", flush=True)
            print(md, flush=True)
            written.append(num)
        else:
            _write_md(subject_key, num, md)
            written.append(num)

    extras = sorted(set(all_content) - set(expected))
    msg_parts = [
        f"{subject_key}: extracted {len(written)} file"
        + ("s" if len(written) != 1 else "")
        + (" (dry-run)" if dry_run else ""),
    ]
    if still_missing:
        msg_parts.append(f"missing: {', '.join(still_missing)}")
    if skipped_empty:
        msg_parts.append(f"empty (no rows/items, skipped): {', '.join(skipped_empty)}")
    if extras:
        msg_parts.append(f"unexpected numbers in response (ignored): {', '.join(extras)}")
    status = "ok" if not still_missing else "warn"
    return (status, " — ".join(msg_parts))


def main() -> int:
    from eXercise.env_load import load_project_env
    load_project_env()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", help="Only process this subject_key (e.g. physics)")
    parser.add_argument("--force", action="store_true", help="Re-extract every subtopic (overwrites existing .md)")
    parser.add_argument("--dry-run", action="store_true", help="Print markdown to stdout, don't write files")
    args = parser.parse_args()

    if args.subject and args.subject not in EXAM_ROOT_BY_KEY:
        print(f"Unknown subject_key: {args.subject!r}", file=sys.stderr)
        print(f"Available: {', '.join(EXAM_ROOT_BY_KEY)}", file=sys.stderr)
        return 2

    subjects = [args.subject] if args.subject else list(EXAM_ROOT_BY_KEY.keys())
    exit_code = 0
    for key in subjects:
        status, message = _run_one(key, force=args.force, dry_run=args.dry_run)
        print(message, file=sys.stderr)
        if status == "error":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

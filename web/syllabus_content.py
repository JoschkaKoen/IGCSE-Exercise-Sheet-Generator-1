"""Syllabus subtopic content extractor + loader for the Learn page.

One-shot CLI (`python -m web.syllabus_content`) per-page-parallel-extracts
each subtopic's two-column learning-objectives table from the syllabus
PDF and writes it as a markdown pipe table to
``syllabi/content/<subject_key>/<subtopic_number>.md``. The web route
loads those files at request time via :func:`load_content`.

Per-page extraction (rather than whole-PDF) keeps Gemini's visual
attention focused on a single table layout, which is critical for
correctly preserving the Core/Supplement row alignment. Subtopics that
span multiple pages are reassembled by :func:`merge_pages` — rows from
later pages are appended to the same subtopic entry in page order.

Gemini imports are deferred to the extraction helpers and the
``__main__`` block, so importing this module from the web route does
not pull in ``google-genai``.
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
from typing import Any

from eXercise.config import EXAM_ROOT_BY_KEY, SYLLABI_DIR

from web.syllabus_topics import current_syllabus_pdf, load_topics

CONTENT_DIR = SYLLABI_DIR / "content"
# Accept depth-2 (1.2, C1.3) and depth-3 (1.5.1) subtopic numbers from Gemini.
# Depth-3 entries get collapsed under their parent N.M in :func:`_merge_pages`
# so e.g. Physics 1.5.1 / 1.5.2 / 1.5.3 rows all flow into the "1.5" subtopic.
_SUBTOPIC_NUM_RE = re.compile(r"^[A-Z]*\d+(?:\.\d+){1,2}$")
_DEPTH2_NUM_RE = re.compile(r"^([A-Z]*\d+\.\d+)(?:\.\d+)?$")


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


# ── Markdown conversion ──────────────────────────────────────────────────
def to_markdown(left_header: str, right_header: str, rows: list[dict]) -> str:
    """Render one subtopic's rows as a markdown pipe table.

    - Escapes ``|`` → ``\\|`` so pipes inside cell text don't break the table.
    - Joins embedded newlines with spaces (pipe tables don't allow literal
      newlines in cells).
    - Drops rows where both cells are empty after stripping.
    """
    def cell(text: Any) -> str:
        s = str(text or "").replace("\r", " ").replace("\n", " ").strip()
        return s.replace("|", "\\|")

    lh = cell(left_header) or "Core"
    rh = cell(right_header) or "Supplement"
    lines = [f"| {lh} | {rh} |", "| --- | --- |"]
    for row in rows:
        left = cell(row.get("left", ""))
        right = cell(row.get("right", ""))
        if not left and not right:
            continue
        lines.append(f"| {left} | {right} |")
    return "\n".join(lines) + "\n"


# ── Pre-pass prompt: find "Subject content" page range ───────────────────
_PRE_PASS_PROMPT = """\
You are given a Cambridge syllabus PDF. Return the inclusive 1-indexed
page range of the section titled \"Subject content\" (typically section
3). The start page is the page where the \"Subject content\" section
heading appears; the end page is the last page of subject content,
just before any later sections such as \"Other information\",
\"Mathematical requirements\", appendices, glossary, or assessment
objectives.

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
    raw, _thinking = split_gemini_response(response)
    data = json.loads(strip_json_fences(raw))
    start = int(data["start"])
    end = int(data["end"])
    if start < 1 or end < start:
        raise RuntimeError(f"{pdf_path.name}: invalid page range {start}–{end}")
    return start, end


# ── Per-page prompt and extraction ───────────────────────────────────────
_PER_PAGE_PROMPT = """\
You are given ONE page of a Cambridge syllabus PDF. Identify every
subtopic-content table visible on this page and extract its rows.

A subtopic-content table is headed by a number like N.M (e.g. \"1.2\")
or [A-Z]N.M (e.g. \"C1.3\" for Mathematics) and has TWO columns. Read
the column headers verbatim from the page — they may be \"Core\" /
\"Supplement\" (sciences), \"Subject content\" / \"Notes and examples\"
(Mathematics), or other variants. The subtopic title text (e.g.
\"Motion\" for 1.2) is the heading ABOVE the table, NOT a row of the
table — do not include it as a row.

Some subtopics are split into sub-sections numbered N.M.K (e.g.
\"1.5.1 Effects of forces\", \"1.5.2 Turning effect of forces\"), each
with its own two-column table. Treat each sub-section as a SEPARATE
subtopic entry, with its full N.M.K number in the `number` field.
Downstream code merges them under the parent N.M.

CRITICAL — preserve visual row alignment.

- When the page shows a left-column item and a right-column item on
  the same horizontal row, they MUST share an output row.
- When a left-column item has no right counterpart on the same row,
  leave the right cell empty (\"\").
- When a right-column item has no left counterpart on the same row,
  leave the left cell empty.
- The right column often qualifies or extends the specific left item
  it shares a row with — preserving this pairing is essential.

If a table starts on this page and is cut off at the bottom, OR
continues from a previous page (header repeats with \"continued\"),
return only the rows visible on THIS page — do NOT invent
continuation rows.

Each cell must be a single line: if a syllabus item wraps across
multiple lines in the PDF, join with spaces. Preserve item numbering
verbatim (e.g. \"3 Recall and use the equation ...\"). Preserve
equations as LaTeX (`$v=s/t$` inline, `$$ ... $$` display) so KaTeX
can render them downstream.

Return JSON only, matching:

{\"subtopics\": [
  {\"number\": \"1.2\", \"left_header\": \"Core\", \"right_header\": \"Supplement\",
   \"rows\": [{\"left\": \"...\", \"right\": \"...\"}]}
]}

If no subtopic table is visible on this page (e.g. the page shows
only a section heading, intro paragraphs, or is between subtopics),
return {\"subtopics\": []}.
"""


def _extract_page_subtopics(
    client: Any,
    page_pdf: Path,
    page_num: int,
    *,
    model_name: str,
    thinking_tokens: int | None,
    max_tokens: int | None,
) -> list[dict]:
    from google.genai import types as gai_types

    from eXercise.ai_client import (
        build_gemini_thinking_config,
        gemini_pdf_part,
        split_gemini_response,
        strip_json_fences,
    )
    from eXercise.api_retry import retry_api_call

    gen_config_kwargs: dict[str, Any] = {
        "system_instruction": _PER_PAGE_PROMPT,
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
        label=f"content p{page_num}",
    )
    raw, _thinking = split_gemini_response(response)
    data = json.loads(strip_json_fences(raw))

    out: list[dict] = []
    for sub in data.get("subtopics") or []:
        if not isinstance(sub, dict):
            continue
        num = str(sub.get("number", "")).strip()
        if not _SUBTOPIC_NUM_RE.match(num):
            continue
        rows: list[dict] = []
        for row in sub.get("rows") or []:
            if not isinstance(row, dict):
                continue
            rows.append({
                "left": str(row.get("left", "") or "").strip(),
                "right": str(row.get("right", "") or "").strip(),
            })
        out.append({
            "number": num,
            "left_header": str(sub.get("left_header", "") or "").strip(),
            "right_header": str(sub.get("right_header", "") or "").strip(),
            "rows": rows,
        })
    return out


# ── PDF page splitter (lazy fitz import) ─────────────────────────────────
def _split_pages(pdf_path: Path, start: int, end: int, out_dir: Path) -> dict[int, Path]:
    """Write each page in ``[start, end]`` as a one-page PDF in ``out_dir``.

    Returns ``{page_num: path}``. ``end`` is clamped to the PDF's actual
    page count.
    """
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
def _merge_pages(per_page: list[tuple[int, list[dict]]]) -> dict[str, dict]:
    """Group rows by depth-2 subtopic number, preserving page order.

    Depth-3 numbers (N.M.K — e.g. Physics 1.5.1) are collapsed under
    their parent N.M; the K-ordered rows flow into the same merged list
    so 1.5.1 / 1.5.2 / 1.5.3 all appear under "1.5" in sequence.
    """
    merged: dict[str, dict] = {}
    for _page_num, subs in sorted(per_page, key=lambda x: x[0]):
        for sub in subs:
            full_num = sub["number"]
            m = _DEPTH2_NUM_RE.match(full_num)
            if not m:
                continue
            num = m.group(1)  # collapse N.M.K → N.M
            entry = merged.setdefault(num, {
                "left_header": "",
                "right_header": "",
                "rows": [],
            })
            # First non-empty header wins (later pages / sub-sections
            # may repeat or omit headers).
            if not entry["left_header"] and sub.get("left_header"):
                entry["left_header"] = sub["left_header"]
            if not entry["right_header"] and sub.get("right_header"):
                entry["right_header"] = sub["right_header"]
            entry["rows"].extend(sub.get("rows") or [])
    return merged


# ── Orchestrator ─────────────────────────────────────────────────────────
def extract_content(pdf_path: Path) -> dict[str, dict]:
    """Pre-pass for page range, per-page parallel extraction, merge.

    Returns ``{subtopic_number: {"left_header": ..., "right_header": ...,
    "rows": [...]}}``. Pages that fail are logged and contribute no rows;
    other pages continue.
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

    print("  pre-pass: finding subject-content page range …", file=sys.stderr)
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
                    _extract_page_subtopics,
                    client, page_path, page_num,
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

    return _merge_pages(per_page)


# ── Output writer + CLI ──────────────────────────────────────────────────
def _write_subtopic(subject_key: str, number: str, content: dict) -> Path:
    out_dir = CONTENT_DIR / subject_key
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{number}.md"
    md = to_markdown(content["left_header"], content["right_header"], content["rows"])
    out_path.write_text(md, encoding="utf-8")
    return out_path


def _expected_subtopics(subject_key: str) -> list[str] | None:
    topics_data = load_topics(subject_key)
    if not topics_data:
        return None
    return [
        s["number"]
        for t in topics_data.get("topics", [])
        for s in t.get("subtopics", [])
        if s.get("number")
    ]


def _missing_subtopics(subject_key: str, expected: list[str]) -> list[str]:
    subj_dir = CONTENT_DIR / subject_key
    if not subj_dir.exists():
        return list(expected)
    return [n for n in expected if not (subj_dir / f"{n}.md").exists()]


def _run_one(subject_key: str, *, force: bool, dry_run: bool) -> tuple[str, str]:
    expected = _expected_subtopics(subject_key)
    if expected is None:
        return ("error", f"{subject_key}: no topics YAML (run web.syllabus_topics first)")

    missing = list(expected) if force else _missing_subtopics(subject_key, expected)
    if not missing and not dry_run:
        return ("skip", f"{subject_key}: all {len(expected)} subtopic files present (use --force to re-extract)")

    pdf = current_syllabus_pdf(subject_key)
    if pdf is None:
        return ("error", f"{subject_key}: no syllabus PDF found in {SYLLABI_DIR}")

    target = list(missing) if missing else list(expected)
    print(
        f"{subject_key}: extracting from {pdf.name} "
        f"({len(target)}/{len(expected)} subtopics needed)",
        file=sys.stderr,
    )

    try:
        all_content = extract_content(pdf)
    except Exception as exc:
        return ("error", f"{subject_key}: extraction failed — {exc}")

    written: list[str] = []
    still_missing: list[str] = []
    for num in target:
        entry = all_content.get(num)
        if not entry or not entry["rows"]:
            still_missing.append(num)
            continue
        if dry_run:
            md = to_markdown(entry["left_header"], entry["right_header"], entry["rows"])
            print(f"### {subject_key}/{num}.md", flush=True)
            print(md, flush=True)
            written.append(num)
        else:
            _write_subtopic(subject_key, num, entry)
            written.append(num)

    extra_in_response = sorted(set(all_content) - set(expected))
    msg_parts = [
        f"{subject_key}: extracted {len(written)} subtopic"
        + ("s" if len(written) != 1 else "")
        + (" (dry-run)" if dry_run else ""),
    ]
    if still_missing:
        msg_parts.append(f"missing: {', '.join(still_missing)}")
    if extra_in_response:
        msg_parts.append(f"unexpected numbers in response (ignored): {', '.join(extra_in_response)}")
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

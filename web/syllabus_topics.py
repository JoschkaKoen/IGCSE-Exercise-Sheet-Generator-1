"""Syllabus topic extractor + loader for the Learn page.

One-shot CLI (`python -m web.syllabus_topics`) reads each subject's syllabus PDF,
asks Gemini (native PDF input) for the two-level topic hierarchy under section
"3 Subject content", and writes the result to ``syllabi/topics/<subject_key>.yaml``.
The web route loads those YAML files at request time via :func:`load_topics`.

Gemini imports are deferred to :func:`extract_topics` and the ``__main__``
block, so importing this module from the route handler does not pull in
``google-genai``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from eXercise.config import EXAM_ROOT_BY_KEY, SYLLABI_DIR, SYLLABUS_CODE_BY_KEY

TOPICS_DIR = SYLLABI_DIR / "topics"
_YEAR_RE = re.compile(r"(20\d{2})(?:-(20\d{2}))?")


# ── Year-range helpers (also imported by web/service.py) ─────────────────────
def parse_year_range(p: Path) -> tuple[int, int] | None:
    """Return ``(start, end)`` years from a syllabus filename like
    ``0625 Physics 2026-2028 Syllabus Document.pdf``. Single-year filenames
    return ``(year, year)``. Returns ``None`` if no year token is present.
    """
    m = _YEAR_RE.search(p.stem)
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2) or m.group(1))
    return (start, end)


def current_syllabus_pdf(subject_key: str) -> Path | None:
    """Pick the syllabus PDF whose year range covers the current year.

    Globs ``<code> *Syllabus Document.pdf`` — explicitly excludes
    ``*Syllabus Update.pdf`` (which is a diff, not a full syllabus). Falls
    back to the candidate whose midpoint is closest to the current year if
    none cover it.
    """
    code = SYLLABUS_CODE_BY_KEY.get(subject_key)
    if not code or not SYLLABI_DIR.is_dir():
        return None
    pdfs = list(SYLLABI_DIR.glob(f"{code} *Syllabus Document.pdf"))
    if not pdfs:
        return None
    current_year = _dt.date.today().year
    covering = [p for p in pdfs if (r := parse_year_range(p)) and r[0] <= current_year <= r[1]]
    if covering:
        return covering[0]

    def distance(p: Path) -> int:
        r = parse_year_range(p)
        if not r:
            return 10_000
        midpoint = (r[0] + r[1]) / 2
        return abs(int(midpoint) - current_year)

    return min(pdfs, key=distance)


# ── Loader (web route reads these — no in-process cache) ─────────────────────
def load_topics(subject_key: str) -> dict[str, Any] | None:
    """Return the parsed ``syllabi/topics/<subject_key>.yaml``, or ``None``."""
    path = TOPICS_DIR / f"{subject_key}.yaml"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("topics"), list):
        return None
    return data


# ── Extraction prompt (sent as Gemini system instruction) ────────────────────
_PROMPT = """\
You are given a Cambridge syllabus PDF. Locate the section titled \"Subject content\"
(typically section 3). Extract a two-level hierarchy:

- Depth 1 (main topics): usually numbered 1, 2, 3, … If a syllabus splits this
  section by paper or part (e.g. \"Paper 1 Theory\" / \"Paper 3 Advanced\") and
  entries are not numbered, use sequential numbers \"1\", \"2\", … and put the
  original heading in the title field.
- Depth 2 (subtopics): the direct children at depth two, usually N.1, N.2, …
  If the numbering restarts inside a paper section, preserve the numbers as
  written in the PDF.

Return JSON only, matching this schema:

{\"topics\": [{\"number\": \"1\", \"title\": \"...\",
  \"subtopics\": [{\"number\": \"1.1\", \"title\": \"...\"}]}]}

Titles must be verbatim from the syllabus. Stop at depth two — do not include
N.N.N items or \"Core / Supplement\" learning objectives.
"""


# ── Extractor (Gemini imports deferred so the web server stays light) ────────
def extract_topics(pdf_path: Path) -> dict[str, Any]:
    """Call Gemini with the syllabus PDF and return ``{"topics": [...]}``.

    Raises ``RuntimeError`` if the API key is missing, the model returns
    non-JSON, or validation fails (missing fields, wrong number format,
    empty subtopics list).
    """
    import os

    from google.genai import types as gai_types

    from eXercise.ai_client import (
        build_gemini_thinking_config,
        gemini_pdf_part,
        make_gemini_native_client,
        parse_model_spec,
        split_gemini_response,
        strip_json_fences,
    )
    from eXercise.api_retry import retry_api_call

    raw_spec = (
        os.environ.get("SYLLABUS_EXTRACT_MODEL", "").strip()
        or "gemini-3.5-flash, off"
    )
    model_name, thinking_tokens, max_tokens = parse_model_spec(raw_spec)
    if not model_name.startswith("gemini"):
        raise RuntimeError(
            f"SYLLABUS_EXTRACT_MODEL must be a Gemini model (got {model_name!r}); "
            "the extractor relies on native PDF input."
        )

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) not set; cannot extract syllabus topics."
        )

    gen_config_kwargs: dict[str, Any] = {
        "system_instruction": _PROMPT,
        "max_output_tokens": max_tokens or 32768,
        "response_mime_type": "application/json",
    }
    if thinking_tokens is not None:
        gen_config_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
    gen_config = gai_types.GenerateContentConfig(**gen_config_kwargs)

    response = retry_api_call(
        lambda: client.models.generate_content(
            model=model_name,
            contents=[gemini_pdf_part(client, pdf_path, label=f"syllabus {pdf_path.stem}")],
            config=gen_config,
        ),
        label=f"syllabus topics — {pdf_path.name}",
    )
    raw, _thinking = split_gemini_response(response)
    try:
        data = json.loads(strip_json_fences(raw))
    except json.JSONDecodeError as exc:
        snippet = raw[:1000].replace("\n", " ")
        raise RuntimeError(
            f"Gemini returned non-JSON for {pdf_path.name}: {exc}\n--- first 1KB ---\n{snippet}"
        ) from exc

    return _normalize(data, pdf_path.name)


_TOPIC_NUM_RE = re.compile(r"^[A-Z]*\d+$")
_SUBTOPIC_NUM_RE = re.compile(r"^[A-Z]*\d+\.\d+$")
# Subtopic numbers may carry a Core/Extended prefix (Math: C1.1, E1.1) — the
# regex accepts both bare ``1.1`` and prefixed ``C1.1`` forms. Mirrored by the
# FastAPI path-param validator in web/routes/learn.py.


def _normalize(data: Any, source: str) -> dict[str, Any]:
    """Trim Gemini output to a strict two-level shape.

    Drops any subtopic whose number is deeper than ``N.M`` (Gemini occasionally
    slips ``N.M.K`` items into the subtopics list when the syllabus has a third
    level — e.g. Physics 1.5.1). Raises on shape errors that can't be repaired
    (non-dict topics, empty result, missing titles).
    """
    if not isinstance(data, dict) or not isinstance(data.get("topics"), list) or not data["topics"]:
        raise RuntimeError(f"{source}: expected dict with non-empty 'topics' list")
    topics_out: list[dict[str, Any]] = []
    for i, t in enumerate(data["topics"]):
        if not isinstance(t, dict):
            raise RuntimeError(f"{source}: topic[{i}] not a dict: {t!r}")
        num = str(t.get("number", "")).strip()
        title = str(t.get("title", "")).strip()
        if not _TOPIC_NUM_RE.match(num):
            raise RuntimeError(
                f"{source}: topic[{i}] number {num!r} must be a single integer"
            )
        if not title:
            raise RuntimeError(f"{source}: topic[{i}] ({num}) missing title")
        subs_in = t.get("subtopics") or []
        if not isinstance(subs_in, list):
            raise RuntimeError(f"{source}: topic[{i}] ({num}) subtopics not a list")
        subs_out: list[dict[str, str]] = []
        for s in subs_in:
            if not isinstance(s, dict):
                continue
            snum = str(s.get("number", "")).strip()
            stitle = str(s.get("title", "")).strip()
            if not _SUBTOPIC_NUM_RE.match(snum) or not stitle:
                continue  # drop deeper N.M.K entries silently
            subs_out.append({"number": snum, "title": stitle})
        if not subs_out:
            raise RuntimeError(
                f"{source}: topic[{i}] ({num}) has no depth-2 subtopics after filtering"
            )
        topics_out.append({"number": num, "title": title, "subtopics": subs_out})
    return {"topics": topics_out}


# ── YAML writer ──────────────────────────────────────────────────────────────
def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            data, f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )


def _build_record(subject_key: str, pdf_path: Path, topics: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject_key": subject_key,
        "syllabus_pdf": pdf_path.name,
        "topics": topics["topics"],
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _run_one(subject_key: str, *, force: bool, dry_run: bool) -> tuple[str, str]:
    """Process one subject. Returns ``(status, message)`` where status is one of
    ``"ok" | "skip" | "error"``."""
    out_path = TOPICS_DIR / f"{subject_key}.yaml"
    if out_path.exists() and not force and not dry_run:
        return ("skip", f"{subject_key}: {out_path.name} exists (use --force to overwrite)")
    pdf = current_syllabus_pdf(subject_key)
    if pdf is None:
        return ("error", f"{subject_key}: no syllabus PDF found in {SYLLABI_DIR}")
    try:
        data = extract_topics(pdf)
    except Exception as exc:
        return ("error", f"{subject_key}: extraction failed — {exc}")
    record = _build_record(subject_key, pdf, data)
    if dry_run:
        print(f"# {subject_key} ({pdf.name})", flush=True)
        yaml.safe_dump(
            record, sys.stdout,
            default_flow_style=False, allow_unicode=True, sort_keys=False,
        )
        return ("ok", f"{subject_key}: {len(data['topics'])} topics (dry-run)")
    _write_yaml(out_path, record)
    return ("ok", f"{subject_key}: wrote {out_path}, {len(data['topics'])} topics")


def main() -> int:
    from eXercise.env_load import load_project_env
    load_project_env()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", help="Only process this subject_key (e.g. physics)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing YAML files")
    parser.add_argument("--dry-run", action="store_true", help="Print YAML to stdout, don't write")
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

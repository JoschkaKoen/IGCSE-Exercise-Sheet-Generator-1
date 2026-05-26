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
from typing import Any, Literal

import yaml

from eXercise.config import EXAM_ROOT_BY_KEY, SYLLABI_DIR, SYLLABUS_CODE_BY_KEY

TOPICS_DIR = SYLLABI_DIR / "topics"
_YEAR_RE = re.compile(r"(20\d{2})(?:-(20\d{2}))?")


def syllabus_level(subject_key: str) -> Literal["igcse", "a_level"]:
    """Map subject_key to its Cambridge level. ``a_level_*`` → A-Level, else IGCSE."""
    return "a_level" if subject_key.startswith("a_level_") else "igcse"


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
    # Recursive glob — PDFs may live in syllabi/igcse/ or syllabi/a_level/
    # subfolders. Cambridge codes don't overlap across levels.
    pdfs = list(SYLLABI_DIR.glob(f"**/{code} *Syllabus Document.pdf"))
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


# ── Extraction prompts (sent as Gemini system instruction) ──────────────────
# Two variants: IGCSE syllabi use peach-banded sub-headings (e.g. \"1.1 Number
# systems\") to introduce N.M subtopics, each with its own two-column table.
# A-Level syllabi use the same peach-banded N.M format but sometimes group
# topics under section dividers (\"Physical chemistry\", etc.) that look like
# additional headings but are NOT topics. Both prompts go to the same model
# (SYLLABUS_EXTRACT_MODEL, gemini-3.5-flash) with native PDF input.

_IGCSE_PROMPT = """\
You are given a Cambridge IGCSE syllabus PDF. Locate the \"Subject content\"
section (usually section 3) and extract its topic hierarchy.

DEPTH 1 — MAIN TOPICS
Topics are numbered 1, 2, 3, … and have a large bold heading like
\"1 Motion, forces and energy\" with no peach/cream background.

DEPTH 2 — SUBTOPICS
Subtopics are introduced by their own peach/cream-banded sub-heading row,
formatted like \"1.1 Physical quantities and measurement techniques\".
Only count an N.M entry as a subtopic when this peach-banded sub-heading
exists for it. Numbers like 1, 2, 3, … that appear as items INSIDE a
table are NOT subtopics; they are learning-outcome items belonging to
the table.

IMPORTANT: if a topic's content is a single table with no peach-banded
N.M sub-headings (Cambridge IGCSE Computer Science topics 7–10 do this),
return an empty subtopics list for that topic: \"subtopics\": []. Do NOT
fabricate N.M entries from item numbers inside the topic-level table.

Stop at depth two. Numbers like 1.5.1, 2.1.1 are sub-sections of a
subtopic, not new subtopics — do not emit them.

Return JSON only, matching:

{\"topics\": [{\"number\": \"1\", \"title\": \"...\",
  \"subtopics\": [{\"number\": \"1.1\", \"title\": \"...\"}]}]}

Titles verbatim from the syllabus. Topics with no subtopics use
\"subtopics\": [].
"""

_A_LEVEL_PROMPT = """\
You are given a Cambridge A-Level (or AS & A-Level) syllabus PDF. Locate
the \"Subject content\" section (usually section 3) and extract its topic
hierarchy.

DEPTH 1 — MAIN TOPICS
Topics are numbered 1, 2, 3, … with a large bold heading like
\"1 Atomic structure\".

DEPTH 2 — SUBTOPICS
Subtopics are introduced by their own peach/cream-banded sub-heading
row, formatted like \"1.1 Particles in the atom and atomic radius\".

SECTION DIVIDERS ARE NOT TOPICS
Some A-Level syllabi group topics under section dividers — e.g.
\"Physical chemistry\", \"Inorganic chemistry\", \"Organic chemistry\" in
Chemistry, or paper headings like \"AS Level subject content\" /
\"A Level subject content\". These dividers do NOT have their own
number — they are pink/red text above the numbered topics. They are
labels, not topics. IGNORE them entirely; do not emit them as topics
and do not let their presence renumber the topics that follow.

Stop at depth two. Numbers like 1.5.1 (if any) are sub-sections of a
subtopic — do not emit them.

Return JSON only, matching:

{\"topics\": [{\"number\": \"1\", \"title\": \"...\",
  \"subtopics\": [{\"number\": \"1.1\", \"title\": \"...\"}]}]}

Titles verbatim from the syllabus. Topics with no subtopics use
\"subtopics\": [].
"""


# ── Extractor (Gemini imports deferred so the web server stays light) ────────
def extract_topics(pdf_path: Path, level: Literal["igcse", "a_level"]) -> dict[str, Any]:
    """Call Gemini with the syllabus PDF and return ``{"topics": [...]}``.

    Dispatches on ``level``: IGCSE syllabi use the IGCSE-specific prompt that
    explicitly forbids inventing subtopic numbers from item numbers inside a
    topic-level table; A-Level syllabi use a prompt that explicitly ignores
    section dividers ("Physical chemistry", etc.) so they don't get
    misread as topics.

    Raises ``RuntimeError`` if the API key is missing, the model returns
    non-JSON, or validation fails.
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

    prompt = _A_LEVEL_PROMPT if level == "a_level" else _IGCSE_PROMPT
    gen_config_kwargs: dict[str, Any] = {
        "system_instruction": prompt,
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
        label=f"syllabus topics ({level}) — {pdf_path.name}",
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

    - Drops any subtopic whose number is deeper than ``N.M`` (Gemini
      occasionally slips ``N.M.K`` items into the subtopics list when
      the syllabus has a third level — e.g. Physics 1.5.1).
    - **Empty subtopics list is now legal** — IGCSE Computer Science
      topics 7–10 have no N.M subdivisions; we no longer fabricate them.
    - Raises on shape errors (non-dict topics, missing titles).
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
    level = syllabus_level(subject_key)
    try:
        data = extract_topics(pdf, level)
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

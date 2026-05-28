# -*- coding: utf-8 -*-
"""Match extracted exam questions to syllabus subtopics / subsubtopics.

One-shot CLI: for a given subject, walks every paper indexed under
``output/eXam/bank/<subject>/``, sends each leaf question to
``gemini-3.5-flash`` together with the subject's full syllabus catalogue
(subtopic titles + any ``### N.M.K`` subsubtopic headers from
``syllabi/content/<subject>/<n.m>.md``), and writes a
``subtopic_matches.yaml`` sidecar next to each paper's
``exam_questions.yaml``.

The catalogue and instruction text are sent as a fixed *system* message on
every call so Gemini's automatic implicit prefix caching (≥1024 tokens,
byte-identical) discounts them across the per-paper fan-out. The only
per-call variation is a short *user* message naming the question.

Usage::

    python -m web.subtopic_matcher --subject igcse_physics [--paper STEM]
                                   [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from eXam.bank import bank_dir_for
from eXercise.ai_client import build_completion_kwargs, make_ai_client
from eXercise.api_retry import retry_api_call
from xscore.shared.qnum_utils import norm_qnum

from . import extracted_questions
from .syllabus_topics import load_topics

# ── Constants ────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_ROOT = REPO_ROOT / "syllabi" / "content"

SUBSUBTOPIC_HEADER_RE = re.compile(r"^### (\d+\.\d+\.\d+) (.+)$", re.MULTILINE)

INSTRUCTIONS = """\
You are classifying Cambridge exam questions against a syllabus catalogue.

Cambridge exam questions are drawn directly from the syllabus — every question
tests content that appears somewhere in the catalogue below. Your job is to
identify which subtopic(s) contain that content.

For each question I send, return the catalogue code(s) that cover the knowledge
the student must use to answer it. Codes are either subtopic numbers like "1.5"
(the `## N.M Title` headings below) or subsubtopic numbers like "1.5.2" (the
`### N.M.K Title` sub-headings inside a subtopic's body). Use the most specific
code that fits — if the question maps to a subsubtopic, return that subsubtopic
and not its parent subtopic.

Return ONLY a JSON object: {"matches": ["<code>", "<code>", ...]}.
- Every code must appear verbatim in the catalogue, either as a `## N.M` heading
  or as a `### N.M.K` sub-heading. Do NOT invent codes from the numbered list
  items inside a subtopic's table or learning-outcomes body — those numbers
  (1, 2, 3, …) are item indices, not catalogue codes.
- Return 1 code for a focused question, up to 3 codes only if the question
  genuinely spans multiple distinct subtopics.
- Return [] only when the question text is too garbled or figure-dependent to
  classify at all. A diagram-based question whose stem still says what's being
  measured (e.g. "distance-time graph") IS classifiable — match it to the
  relevant subtopic on the stem alone."""

# ── Catalogue assembly ───────────────────────────────────────────────────


_H1_LINE_RE = re.compile(r"^# [^\n]*\n+", re.MULTILINE)


def _load_subtopic_body(md_path: Path) -> str:
    """Return the .md body with its leading ``# N.M Title`` H1 stripped.

    We re-emit the heading in our own ``## N.M Title`` form for catalogue
    consistency, so dropping the file's own H1 avoids duplicating it.
    """
    if not md_path.is_file():
        return ""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _H1_LINE_RE.sub("", text, count=1).strip()


def build_catalogue(subject: str) -> tuple[str, set[str], int]:
    """Assemble the (catalogue_text, valid_codes, subsubtopic_count) for *subject*.

    For each subtopic from ``topics.yaml`` we emit a ``## N.M Title`` heading
    followed by the verbatim body of ``syllabi/content/<subject>/<n.m>.md``
    (with that file's own H1 stripped). Including the body — Core/Supplement
    table rows for IGCSE, numbered learning outcomes for A-Level — gives the
    model the actual learning objectives to classify against AND keeps the
    prefix comfortably above Gemini's implicit-cache floor (~10k tokens per
    subject vs. ~700 with headers alone).

    ``valid_codes`` is the set of every N.M and N.M.K code referenced anywhere
    in the catalogue, used to drop hallucinated codes from model output.
    ``subsubtopic_count`` is reported by the CLI so the operator can confirm
    whether subsubtopic granularity is active for this subject.
    """
    topics_data = load_topics(subject)
    if not topics_data:
        raise RuntimeError(
            f"No syllabi/topics/{subject}.yaml — run "
            "`python -m web.syllabus_topics` first."
        )

    sections: list[str] = []
    valid_codes: set[str] = set()
    subsubtopic_count = 0
    content_dir = CONTENT_ROOT / subject

    for topic in topics_data.get("topics") or []:
        for sub in topic.get("subtopics") or []:
            num = str(sub.get("number") or "").strip()
            title = str(sub.get("title") or "").strip()
            if not num or not title:
                continue
            valid_codes.add(num)

            md_path = content_dir / f"{num}.md"
            body = _load_subtopic_body(md_path)
            # Subsubtopic codes live as `### N.M.K` headers inside the body.
            for sub_num, _t in SUBSUBTOPIC_HEADER_RE.findall(body):
                valid_codes.add(sub_num)
                subsubtopic_count += 1

            block = f"## {num} {title}"
            if body:
                block = f"{block}\n\n{body}"
            sections.append(block)

    return "\n\n".join(sections), valid_codes, subsubtopic_count


def _format_valid_codes_block(valid_codes: set[str]) -> str:
    """Emit an explicit 'these are the only legal codes' enumeration.

    Without this, models will often hallucinate plausible-looking codes
    (e.g. `1.3.1` for a subject whose catalogue has no subsubtopics at all)
    by extrapolating from the numbered items inside a subtopic body. Listing
    the codes upfront removes the ambiguity.
    """
    def _sort_key(code: str) -> tuple:
        return tuple(int(p) if p.isdigit() else (0, p) for p in code.split("."))

    subtopics = sorted((c for c in valid_codes if c.count(".") == 1), key=_sort_key)
    subsubtopics = sorted((c for c in valid_codes if c.count(".") >= 2), key=_sort_key)

    lines = [f"Valid subtopic codes ({len(subtopics)}): {', '.join(subtopics)}"]
    if subsubtopics:
        lines.append(
            f"Valid subsubtopic codes ({len(subsubtopics)}): {', '.join(subsubtopics)}"
        )
    else:
        lines.append(
            "Valid subsubtopic codes: (none — this subject has no subsubtopic "
            "headers; only N.M codes are valid for it)"
        )
    lines.append(
        "Any code outside these lists is INVALID. Do not invent N.M.K codes "
        "from numbered items inside a subtopic body — those are item indices, "
        "not catalogue codes."
    )
    return "\n".join(lines)


def build_system_prefix(
    subject_display: str,
    paper_stem: str,
    catalogue: str,
    valid_codes: set[str],
) -> str:
    """Compose the system message sent on every per-question call.

    The string must be byte-identical across every call in a paper for the
    provider's implicit prefix cache to discount it.
    """
    valid_block = _format_valid_codes_block(valid_codes)
    paper_block = f"Subject: {subject_display}\nPaper: {paper_stem}"
    return (
        f"{INSTRUCTIONS}\n\n---\n\n"
        f"{catalogue}\n\n---\n\n"
        f"{valid_block}\n\n---\n\n"
        f"{paper_block}"
    )


# ── Question traversal ──────────────────────────────────────────────────


def iter_leaves(questions: list[dict], parent_text: str = "") -> Iterator[tuple[dict, str]]:
    """Yield (leaf_question, accumulated_parent_text) for every leaf in *questions*.

    A leaf is a question node with no ``subquestions``. ``parent_text`` is the
    concatenation of stem texts from every ancestor, so a child like
    "Calculate the moment" carries the parent context "A 2.0 N weight is hung …".
    Leaves whose text is the sentinel ``"STUB ERROR"`` are still yielded — the
    caller decides whether to skip them.
    """
    for q in questions:
        subs = q.get("subquestions") or []
        if subs:
            own = (q.get("text") or "").strip()
            new_parent = (parent_text + ("\n\n" + own if own else "")).strip()
            yield from iter_leaves(subs, new_parent)
        else:
            yield q, parent_text


def _format_options(q: dict) -> str:
    opts = q.get("answer_options") or []
    if not opts:
        return "(none — not multiple choice)"
    lines = []
    for opt in opts:
        letter = str(opt.get("letter") or "").strip()
        text = str(opt.get("text") or "").strip()
        lines.append(f"{letter}) {text}")
    return "\n".join(lines)


def build_user_message(q: dict, parent_text: str) -> str:
    qnum = str(q.get("number") or "").strip() or "?"
    marks = q.get("marks")
    marks_str = str(marks) if isinstance(marks, int) else "?"
    qtype = str(q.get("question_type") or "?").strip()
    text = (q.get("text") or "").strip() or "(none)"
    ctx = parent_text.strip() or "(none)"
    options = _format_options(q)
    return (
        f"Question: {qnum}\n"
        f"Marks: {marks_str}\n"
        f"Type: {qtype}\n\n"
        f"Context (parent stem):\n{ctx}\n\n"
        f"Question text:\n{text}\n\n"
        f"Answer options:\n{options}"
    )


# ── API call ────────────────────────────────────────────────────────────


def _parse_matches(raw_content: str, valid_codes: set[str]) -> tuple[list[str], list[str]]:
    """Parse Gemini's JSON output → (kept_codes, dropped_codes)."""
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError:
        return [], []
    matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(matches, list):
        return [], []
    kept: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for m in matches:
        code = str(m).strip()
        if not code:
            continue
        if code in seen:
            continue
        seen.add(code)
        if code in valid_codes:
            kept.append(code)
        else:
            dropped.append(code)
    return kept, dropped


def call_match(
    client: Any,
    model: str,
    provider: str,
    thinking_tokens: int | None,
    system_prefix: str,
    user_message: str,
    max_tokens: int,
    label: str,
) -> tuple[str, int]:
    """Single classification call. Returns (raw_content, cached_input_tokens).

    Uses :func:`build_completion_kwargs` to thread the provider-correct
    thinking / max_tokens kwargs (e.g. Qwen needs ``extra_body={"enable_thinking":
    False}`` for the non-streaming JSON path to work).

    ``cached_input_tokens`` is read from
    ``usage.prompt_tokens_details.cached_tokens`` when present; ``-1`` if the
    field isn't surfaced (Gemini OpenAI-compat omits it currently).
    """
    _use_stream, kw = build_completion_kwargs(provider, thinking_tokens, max_tokens)

    def _do_call():
        return client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prefix},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            **kw,
        )

    resp = retry_api_call(_do_call, label=label)
    content = (resp.choices[0].message.content or "").strip()
    cached = -1
    usage = getattr(resp, "usage", None)
    if usage is not None:
        details = getattr(usage, "prompt_tokens_details", None)
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)
        elif details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)
        # details is None → field absent on this provider; leave cached = -1
        # so the log shows "?" rather than misreporting "miss".
    return content, cached


# ── Sidecar I/O ─────────────────────────────────────────────────────────


def _sidecar_path(subject: str, paper_stem: str) -> Path:
    return bank_dir_for(subject, Path(paper_stem)) / "subtopic_matches.yaml"


def _load_sidecar(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    raw = data.get("matches") or {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v if x]
    return out


def _write_sidecar(
    path: Path,
    *,
    subject: str,
    paper_stem: str,
    model: str,
    matches: dict[str, list[str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "subject_key": subject,
        "paper_stem": paper_stem,
        "model": model,
        "matched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matches": {k: matches[k] for k in sorted(matches)},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True, default_flow_style=None)
    os.replace(tmp, path)


# ── Per-paper runner ────────────────────────────────────────────────────


def _display_name(subject: str) -> str:
    return extracted_questions._display_name(subject)


def process_paper(
    *,
    subject: str,
    paper_stem: str,
    catalogue: str,
    valid_codes: set[str],
    client: Any,
    model: str,
    provider: str,
    thinking_tokens: int | None,
    max_tokens: int,
    workers: int,
    force: bool,
    dry_run: bool,
) -> None:
    data = extracted_questions.load_paper(subject, paper_stem)
    if data is None:
        print(f"  ! {paper_stem}: no exam_questions.yaml", file=sys.stderr)
        return
    questions = data.get("questions") or []

    leaves: list[tuple[dict, str]] = list(iter_leaves(questions))
    # Skip STUB ERROR leaves — no text the model can match against.
    leaves = [(q, p) for q, p in leaves if (q.get("text") or "").strip() != "STUB ERROR"]

    sidecar = _sidecar_path(subject, paper_stem)
    matches: dict[str, list[str]] = {} if force else _load_sidecar(sidecar)

    todo: list[tuple[str, dict, str]] = []
    for q, parent_text in leaves:
        key = norm_qnum(str(q.get("number") or ""))
        if not key:
            continue
        if not force and key in matches:
            continue
        todo.append((key, q, parent_text))

    system_prefix = build_system_prefix(
        _display_name(subject), paper_stem, catalogue, valid_codes,
    )

    header = f"{subject} / {paper_stem}: {len(leaves)} leaf questions ({len(todo)} to match)"
    print(header, file=sys.stderr)

    if dry_run:
        print("(dry-run — no API calls, no file write)", file=sys.stderr)
        for key, q, parent_text in todo[:3]:
            print(f"--- system prefix ({len(system_prefix)} chars) ---", file=sys.stderr)
            if key == todo[0][0]:
                print(system_prefix, file=sys.stderr)
            print(f"--- user message for q='{key}' ---", file=sys.stderr)
            print(build_user_message(q, parent_text), file=sys.stderr)
            print("", file=sys.stderr)
        return

    if not todo:
        print("  (nothing to do)", file=sys.stderr)
        return

    lock = threading.Lock()
    flush_interval = 32
    completed_since_flush = 0
    done = 0

    def _one(key: str, q: dict, parent_text: str) -> tuple[str, list[str], list[str], int, float]:
        user_msg = build_user_message(q, parent_text)
        t0 = time.monotonic()
        raw, cached = call_match(
            client, model, provider, thinking_tokens,
            system_prefix, user_msg,
            max_tokens=max_tokens,
            label=f"subtopic-match {subject}/{paper_stem} q={key}",
        )
        kept, dropped = _parse_matches(raw, valid_codes)
        return key, kept, dropped, cached, time.monotonic() - t0

    def _log(prefix: str, key: str, kept: list[str], dropped: list[str], cached: int, ttf: float) -> None:
        cache_str = (
            "?" if cached < 0
            else "hit" if cached > 0
            else "miss"
        )
        warn = f"  (dropped {dropped})" if dropped else ""
        print(
            f"{prefix} q='{key}' → {kept}  (cache: {cache_str}, ttf={ttf:.2f}s){warn}",
            file=sys.stderr,
        )

    # Warm call (serial) so Gemini registers the prefix before the fan-out.
    warm_key, warm_q, warm_parent = todo[0]
    try:
        key, kept, dropped, cached, ttf = _one(warm_key, warm_q, warm_parent)
        matches[key] = kept
        _log("[warm]", key, kept, dropped, cached, ttf)
        done += 1
        completed_since_flush += 1
    except Exception as exc:
        print(f"[warm] q='{warm_key}' FAILED: {exc}", file=sys.stderr)
        return  # If the very first call fails, don't fan out — likely auth / quota.

    # Fan-out for the rest.
    remaining = todo[1:]
    if remaining:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(_one, key, q, parent_text): key
                for key, q, parent_text in remaining
            }
            for fut in as_completed(futs):
                fut_key = futs[fut]
                try:
                    key, kept, dropped, cached, ttf = fut.result()
                except Exception as exc:
                    print(f"[?/{len(remaining)}] q='{fut_key}' FAILED: {exc}", file=sys.stderr)
                    continue
                with lock:
                    matches[key] = kept
                    done += 1
                    completed_since_flush += 1
                    flush_now = completed_since_flush >= flush_interval
                    if flush_now:
                        completed_since_flush = 0
                _log(f"[{done}/{len(todo)}]", key, kept, dropped, cached, ttf)
                if flush_now:
                    with lock:
                        _write_sidecar(
                            sidecar,
                            subject=subject, paper_stem=paper_stem,
                            model=model, matches=matches,
                        )

    _write_sidecar(
        sidecar,
        subject=subject, paper_stem=paper_stem,
        model=model, matches=matches,
    )
    print(f"flushed {len(matches)} matches → {sidecar}", file=sys.stderr)


# ── CLI ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    from eXercise.env_load import load_project_env
    load_project_env()

    parser = argparse.ArgumentParser(
        prog="python -m web.subtopic_matcher",
        description=(
            "Classify extracted exam questions against the subject's syllabus "
            "subtopics / subsubtopics using gemini-3.5-flash."
        ),
    )
    parser.add_argument(
        "--subject", required=True,
        help="Subject slug (e.g. igcse_physics, a_level_chemistry).",
    )
    parser.add_argument(
        "--paper", default=None,
        help="Optional paper-stem to restrict the run (default: every indexed paper).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-run every question even if its key is already in the sidecar.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the catalogue + a few example user messages; no API calls, no file write.",
    )
    args = parser.parse_args(argv)

    subject = args.subject
    valid_subjects = {s["slug"] for s in extracted_questions.list_subjects()}
    if subject not in valid_subjects:
        print(
            f"error: '{subject}' is not an indexed subject. "
            f"Indexed: {sorted(valid_subjects)}",
            file=sys.stderr,
        )
        return 2

    catalogue, valid_codes, subsubtopic_count = build_catalogue(subject)
    if subsubtopic_count == 0:
        print(
            f"{subject}: matching at subtopic granularity (no ### headers found)",
            file=sys.stderr,
        )
    else:
        print(
            f"{subject}: catalogue has {len(valid_codes)} codes "
            f"({subsubtopic_count} subsubtopics)",
            file=sys.stderr,
        )

    if args.paper:
        papers = [args.paper]
    else:
        papers = extracted_questions.list_papers(subject)
    if not papers:
        print(f"  ! no indexed papers under output/eXam/bank/{subject}/", file=sys.stderr)
        return 1

    if args.dry_run:
        # Skip client setup — no API calls.
        client = None
        model = "(dry-run)"
        provider = "unknown"
        thinking_tokens: int | None = 0
        max_tokens = 256
        workers = 0
    else:
        bundle = make_ai_client(
            model_env="LEARN_SUBTOPIC_MATCH_MODEL",
            default_model="qwen3.6-plus, 0, 1024",
            deterministic=True,
        )
        if bundle is None:
            print(
                "error: provider API key not set (DASHSCOPE_API_KEY for Qwen, "
                "GEMINI_API_KEY for Gemini, etc.) — check .env.",
                file=sys.stderr,
            )
            return 1
        client, model, provider, thinking_tokens, model_max = bundle
        max_tokens = model_max or 1024
        workers = max(1, int(os.environ.get("LEARN_SUBTOPIC_MATCH_WORKERS", "8") or "8"))
        print(
            f"model={model} provider={provider} thinking={thinking_tokens} "
            f"max_tokens={max_tokens} workers={workers}",
            file=sys.stderr,
        )

    for paper_stem in papers:
        try:
            process_paper(
                subject=subject,
                paper_stem=paper_stem,
                catalogue=catalogue,
                valid_codes=valid_codes,
                client=client,
                model=model,
                provider=provider,
                thinking_tokens=thinking_tokens,
                max_tokens=max_tokens,
                workers=workers,
                force=args.force,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            print(f"  ! {paper_stem}: {exc}", file=sys.stderr)
            continue

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

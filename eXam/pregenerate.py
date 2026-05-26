"""Helper pregeneration: text-only via Qwen / multimodal via Gemini.

Dispatch rule: ``Question.images`` non-empty → Gemini + PDF snippet attached;
otherwise → per-kind text model (Qwen).

Helpers are cached in ``question_helpers (question_id, kind)``. Re-running
with an existing row is a no-op unless caller passes ``force=True``.
"""

from __future__ import annotations

import datetime as _dt
import os
import time
import traceback
from pathlib import Path

from eXam.bank import bank_dir_for
from eXam.db import connect
from eXam.prompts.loader import load_prompt
from eXam.runtime import (
    mark_scheme_entry,
    parse_question_id,
    pdf_path_for,
    question_metadata,
)

KINDS = ("hint", "solution", "example", "kb")


def helper_path(question_id: str, kind: str) -> Path:
    """`<bank>/<subject>/<paper_stem>/<qnum>/helpers/<kind>.md`."""
    subject, paper_stem, qnum = parse_question_id(question_id)
    return bank_dir_for(subject, Path(paper_stem)) / qnum / "helpers" / f"{kind}.md"


def read_cached(question_id: str, kind: str) -> str | None:
    """Return cached helper content if the file exists, else None."""
    p = helper_path(question_id, kind)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")

_MODEL_ENV_BY_KIND = {
    "hint": "EXAM_HINT_MODEL",
    "solution": "EXAM_SOLUTION_MODEL",
    "example": "EXAM_EXAMPLE_MODEL",
    "kb": "EXAM_KB_MODEL",
}

_PROMPT_BY_KIND = {
    "hint": "hint",
    "solution": "solution",
    "example": "example_question",
    "kb": "kb_topic",
}


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _scheme_text(qid: str) -> str:
    """Best-effort plain-text rendering of the question's mark scheme entry."""
    entry = mark_scheme_entry(qid)
    if not entry:
        return "(no mark scheme available)"
    if entry.get("correct_answer"):
        ans = entry["correct_answer"]
        expl = entry.get("explanation") or ""
        if expl:
            return f"Correct answer: {ans}\n\n{expl}"
        return f"Correct answer: {ans}"
    return str(entry.get("mark_scheme_answer") or "(scheme empty)")


def _build_prompt(kind: str, meta: dict) -> tuple[str, str]:
    """Return ``(system, user)`` for the helper kind."""
    prompt_name = _PROMPT_BY_KIND[kind]
    subs = {
        "subject": meta["subject"],
        "question_text": meta["text"] or "(text extraction missing)",
        "mark_scheme_text": _scheme_text(meta["question_id"]),
    }
    _, system = load_prompt(prompt_name, section="system", **subs)
    _, user = load_prompt(prompt_name, section="user", **subs)
    return system, user


def _call_text_qwen(kind: str, system: str, user: str) -> tuple[str, str]:
    """Return ``(content, model_id)`` via the OpenAI-compatible client."""
    from eXercise.ai_client import build_completion_kwargs, make_ai_client

    result = make_ai_client(model_env=_MODEL_ENV_BY_KIND[kind])
    if result is None:
        raise RuntimeError(
            f"No API key set for {_MODEL_ENV_BY_KIND[kind]} — set QWEN_API_KEY (or equivalent) in .env"
        )
    client, model, provider, thinking, max_tokens = result
    _, thinking_kw = build_completion_kwargs(provider, thinking, max_tokens)
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    completion = client.chat.completions.create(
        model=model, messages=msgs, **thinking_kw,
    )
    content = (completion.choices[0].message.content or "").strip()
    return content, model


def _call_pdf_gemini(system: str, user: str, pdf_path) -> tuple[str, str]:
    """Return ``(content, model_id)`` via the native Gemini client with PDF attached."""
    from eXercise.ai_client import (
        build_gemini_thinking_config,
        gemini_pdf_part,
        make_gemini_native_client,
        parse_model_spec,
    )
    from google.genai import types as gai_types

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) not set — required for EXAM_PDF_MODEL"
        )
    raw = os.environ.get("EXAM_PDF_MODEL", "gemini-3.5-flash, 1024, 8192")
    model_name, thinking, max_tokens = parse_model_spec(raw)
    cfg_kwargs = {
        "system_instruction": system,
        "max_output_tokens": max_tokens or 4096,
    }
    if thinking is not None:
        cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking)
    cfg = gai_types.GenerateContentConfig(**cfg_kwargs)
    contents = [
        gemini_pdf_part(client, pdf_path, label="question"),
        user,
    ]
    response = client.models.generate_content(
        model=model_name, contents=contents, config=cfg,
    )
    return (response.text or "").strip(), model_name


def pregenerate_for_question(
    rec: dict,
    subject: str,
    kind: str,
    *,
    force: bool = False,
    test_id: str | None = None,
) -> str:
    """Generate (and cache to disk) one helper. Returns the content text.

    *test_id* is optional context for the ai_calls log; pass it from
    build-time callers and leave it ``None`` for on-demand `/api/helper`
    requests that aren't tied to a single test.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    qid = rec["question_id"]
    path = helper_path(qid, kind)
    if not force and path.exists():
        return path.read_text(encoding="utf-8")

    meta = question_metadata(qid)
    if meta is None:
        raise RuntimeError(f"no metadata for {qid}")
    # Keep subject from the test (in case it differs from the slug embedded in qid).
    meta["subject"] = subject

    system, user = _build_prompt(kind, meta)
    pdf = pdf_path_for(qid) if meta.get("has_images") else None

    from eXam.cost_tracker import track

    started = time.monotonic()
    with track(f"pregen_{kind}", test_id=test_id, question_id=qid):
        if pdf is not None and pdf.exists():
            content, model_id = _call_pdf_gemini(system, user, pdf)
        else:
            content, model_id = _call_text_qwen(kind, system, user)
    elapsed = time.monotonic() - started
    print(f"[pregen] {kind} for {qid} ({model_id}, {elapsed:.1f}s) → {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def pregenerate_for_test(test_id: str, *, force: bool = False) -> dict:
    """Generate all four helpers for every question in the test. Best-effort.

    Passes ``test_id`` to each :func:`pregenerate_for_question` call so the
    resulting AI usage rows are attributed to this test in the cost log.
    """
    import json as _json
    with connect() as conn:
        row = conn.execute(
            "SELECT subject, question_ids FROM tests WHERE id=?", (test_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError(f"no such test: {test_id}")
    subject = row["subject"]
    qids = _json.loads(row["question_ids"])
    results: dict[str, dict[str, str]] = {}
    failures: list[tuple[str, str, str]] = []
    for qid in qids:
        rec = {"question_id": qid}
        results.setdefault(qid, {})
        for kind in KINDS:
            try:
                pregenerate_for_question(rec, subject, kind, force=force, test_id=test_id)
                results[qid][kind] = "ok"
            except Exception as e:  # noqa: BLE001
                failures.append((qid, kind, repr(e)))
                results[qid][kind] = "fail"
                tb = traceback.format_exc()
                print(f"[pregen] failed {kind} for {qid}: {tb}")
    return {"results": results, "failures": failures}


def _cli() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(prog="eXam.pregenerate")
    p.add_argument("--test-id", required=True)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    from eXercise.env_load import load_project_env
    load_project_env()
    out = pregenerate_for_test(args.test_id, force=args.force)
    print(f"failures: {len(out['failures'])}")
    return 0 if not out["failures"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli())

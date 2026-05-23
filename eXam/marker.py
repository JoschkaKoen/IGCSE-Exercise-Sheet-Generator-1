"""eXam marker — three paths: MCQ deterministic, numeric final-answer, free response.

Text-only by default. The figure-aware multimodal path (Gemini + PDF snippet)
activates when the question's ``images`` field is non-empty.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from eXam.bank import bank_dir_for
from eXam.prompts.loader import load_prompt
from eXam.runtime import mark_scheme_entry, parse_question_id, pdf_path_for, question_metadata


def _final_answer_cache_path(qid: str) -> Path:
    subject, paper_stem, qnum = parse_question_id(qid)
    return bank_dir_for(subject, Path(paper_stem)) / qnum / "final_answer.json"


def _mark_mcq(meta: dict, submitted: str) -> dict:
    entry = mark_scheme_entry(meta["question_id"]) or {}
    expected = (entry.get("correct_answer") or "").strip().upper()
    if not expected:
        return {
            "assigned_marks": 0.0,
            "max_marks": float(meta.get("marks") or 1),
            "reasoning": "Mark scheme missing the correct answer letter.",
        }
    got_letters = re.findall(r"[A-Z]", submitted.upper())
    got = got_letters[0] if got_letters else ""
    marks = float(meta.get("marks") or 1)
    if got == expected:
        return {
            "assigned_marks": marks,
            "max_marks": marks,
            "reasoning": f"Correct ({expected}).",
        }
    return {
        "assigned_marks": 0.0,
        "max_marks": marks,
        "reasoning": (
            f"Incorrect. You selected {got or '(none)'}; "
            "see the solution after you answer correctly."
        ),
    }


_NUM_UNIT_RE = re.compile(
    r"""
    ^\s*
    (?P<value>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
    \s*
    (?P<unit>[^\s].*?)?
    \s*$
    """,
    re.VERBOSE,
)


def _parse_value_unit(text: str) -> tuple[float | None, str]:
    m = _NUM_UNIT_RE.match(text)
    if not m:
        return None, ""
    try:
        value = float(m.group("value"))
    except (TypeError, ValueError):
        return None, ""
    unit = (m.group("unit") or "").strip()
    return value, unit


def _units_match(a: str, b: str) -> bool:
    if not a or not b:
        return True  # missing on either side → don't penalise
    norm = lambda s: re.sub(r"\s+", "", s).replace("·", "").replace("^", "").lower()
    return norm(a) == norm(b)


def _call_text_qwen(model_env: str, system: str, user: str) -> str:
    from eXercise.ai_client import build_completion_kwargs, make_ai_client

    result = make_ai_client(model_env=model_env)
    if result is None:
        raise RuntimeError(f"No API key set for {model_env}")
    client, model, provider, thinking, max_tokens = result
    _, thinking_kw = build_completion_kwargs(provider, thinking, max_tokens)
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **thinking_kw,
    )
    return (completion.choices[0].message.content or "").strip()


def _call_pdf_gemini(system: str, user: str, pdf_path: Path) -> str:
    import os

    from eXercise.ai_client import (
        build_gemini_thinking_config,
        gemini_pdf_part,
        make_gemini_native_client,
        parse_model_spec,
    )
    from google.genai import types as gai_types

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not set")
    raw = os.environ.get("EXAM_PDF_MODEL", "gemini-3-flash-preview, 1024, 8192")
    model_name, thinking, max_tokens = parse_model_spec(raw)
    cfg_kwargs = {"system_instruction": system, "max_output_tokens": max_tokens or 4096}
    if thinking is not None:
        cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking)
    cfg = gai_types.GenerateContentConfig(**cfg_kwargs)
    contents = [gemini_pdf_part(client, pdf_path, label="question"), user]
    response = client.models.generate_content(
        model=model_name, contents=contents, config=cfg,
    )
    return (response.text or "").strip()


def _strip_json_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # Remove ```json ... ``` fences if the model added them.
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s


def _parse_json(s: str) -> dict:
    s = _strip_json_fences(s)
    return json.loads(s)


def _extract_final_answer_spec(meta: dict, scheme_text: str) -> dict:
    """Lazy AI extraction of {value, unit, tolerance_rel, max_marks} from scheme."""
    cache = _final_answer_cache_path(meta["question_id"])
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    _, system = load_prompt(
        "mark_numeric_final", section="system",
        subject=meta["subject"],
        question_text=meta["text"],
        mark_scheme_text=scheme_text,
    )
    _, user = load_prompt(
        "mark_numeric_final", section="user",
        subject=meta["subject"],
        question_text=meta["text"],
        mark_scheme_text=scheme_text,
    )
    raw = _call_text_qwen("EXAM_PHYSICS_MARK_MODEL", system, user)
    spec = _parse_json(raw)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(spec), encoding="utf-8")
    return spec


def _mark_numeric_final(meta: dict, submitted: str) -> dict:
    scheme_entry = mark_scheme_entry(meta["question_id"]) or {}
    scheme_text = str(scheme_entry.get("mark_scheme_answer") or "")
    if not scheme_text.strip():
        # Fall back to free-response if scheme is empty.
        return _mark_free_response(meta, submitted)
    try:
        spec = _extract_final_answer_spec(meta, scheme_text)
    except Exception as e:  # noqa: BLE001
        # If the AI extraction fails, fall back to free-response.
        return _mark_free_response(meta, submitted, fallback_reason=str(e))

    target_value = float(spec.get("value", 0) or 0)
    target_unit = str(spec.get("unit", "") or "")
    tol_rel = float(spec.get("tolerance_rel", 0.05) or 0.05)
    max_marks = float(spec.get("max_marks", meta.get("marks") or 1))

    value, unit = _parse_value_unit(submitted)
    if value is None:
        # Student submitted prose; the extractor may have hallucinated a number
        # from mark counts or part labels. Don't hard-zero a reasonable sentence
        # — defer to the AI free-response marker.
        return _mark_free_response(meta, submitted)
    if target_value == 0:
        # Scheme is qualitative; defer to free response.
        return _mark_free_response(meta, submitted)
    abs_diff = abs(value - target_value)
    abs_tol = tol_rel * abs(target_value)
    value_ok = abs_diff <= abs_tol
    unit_ok = _units_match(unit, target_unit)
    if value_ok and unit_ok:
        return {
            "assigned_marks": max_marks,
            "max_marks": max_marks,
            "reasoning": f"Correct ({value} {unit or target_unit}).",
        }
    if value_ok and not unit_ok:
        partial = max_marks / 2 if max_marks > 1 else 0.0
        return {
            "assigned_marks": partial,
            "max_marks": max_marks,
            "reasoning": (
                f"Value is right but the unit should be {target_unit!r} (you wrote {unit!r})."
                if partial > 0 else
                f"Value is right but the unit should be {target_unit!r}; no partial credit on a 1-mark item."
            ),
        }
    return {
        "assigned_marks": 0.0,
        "max_marks": max_marks,
        "reasoning": (
            f"Expected approximately {target_value} {target_unit} "
            f"(within ±{tol_rel*100:.0f}%)."
        ),
    }


def _mark_free_response(meta: dict, submitted: str, *, fallback_reason: str = "") -> dict:
    entry = mark_scheme_entry(meta["question_id"]) or {}
    scheme_text = str(entry.get("mark_scheme_answer") or entry.get("explanation") or "")
    subs = {
        "subject": meta["subject"],
        "question_text": meta["text"],
        "mark_scheme_text": scheme_text or "(scheme empty)",
        "student_answer": submitted,
    }
    _, system = load_prompt("mark_free_response", section="system", **subs)
    _, user = load_prompt("mark_free_response", section="user", **subs)

    # Figure-aware dispatch.
    if meta.get("has_images"):
        pdf = pdf_path_for(meta["question_id"])
        try:
            raw = _call_pdf_gemini(system, user, pdf)
        except Exception as e:  # noqa: BLE001
            # Last-resort fallback to text-only Qwen.
            raw = _call_text_qwen("EXAM_MARK_MODEL", system, user)
    else:
        raw = _call_text_qwen("EXAM_MARK_MODEL", system, user)

    try:
        data = _parse_json(raw)
    except json.JSONDecodeError:
        return {
            "assigned_marks": 0.0,
            "max_marks": float(meta.get("marks") or 1),
            "reasoning": "Marker returned an unparseable response; ask your teacher to re-mark.",
        }
    return {
        "assigned_marks": float(data.get("assigned_marks", 0) or 0),
        "max_marks": float(data.get("max_marks", meta.get("marks") or 1)),
        "reasoning": str(data.get("reasoning", "")).strip()[:500],
    }


def _mark_one_leaf_meta(top_meta: dict, leaf: dict) -> dict:
    """Build a leaf-scoped meta dict for the existing private mark functions.

    The synthetic ``question_id`` exists only so ``mark_scheme_entry`` picks
    the right leaf row from ``mark_scheme.yaml`` — it's never persisted.
    ``has_images`` carries the top-level value (the snippet PDF shows every
    figure regardless of which leaf is being graded).
    """
    subject = top_meta["subject"]
    paper_stem = top_meta["paper_stem"]
    return {
        **top_meta,
        "question_id": f"{subject}::{paper_stem}::{leaf['number']}",
        "number": leaf["number"],
        "question_type": leaf["question_type"],
        "marks": leaf["marks"],
        "text": leaf["text"],
        "options": leaf["options"],
        "has_images": leaf["has_images"],
    }


def _mark_leaf_dispatch(
    leaf_meta: dict,
    answer: str,
    *,
    test_id: str | None,
    student_id: int,
) -> dict:
    """Apply MCQ / numeric / free-response marking to one leaf."""
    qtype = leaf_meta.get("question_type")
    if qtype == "multiple_choice":
        return _mark_mcq(leaf_meta, answer)
    is_numeric = qtype in ("calculation", "long_answer")
    op = "mark_numeric" if is_numeric else "mark_free"
    from eXam.cost_tracker import track

    with track(
        op,
        test_id=test_id,
        student_id=student_id,
        question_id=leaf_meta["question_id"],
    ):
        if is_numeric:
            return _mark_numeric_final(leaf_meta, answer)
        return _mark_free_response(leaf_meta, answer)


def _mark_legacy_single_string(
    meta: dict,
    submitted: str,
    *,
    test_id: str | None,
    student_id: int,
) -> dict:
    """Legacy path for callers that still send a single string for the whole
    top-level question (class mode). Behavior is identical to the pre-leaf
    implementation — kept until class mode is migrated."""
    qtype = meta.get("question_type")
    if qtype == "multiple_choice":
        return _mark_mcq(meta, submitted)
    is_numeric = qtype in ("calculation", "long_answer")
    op = "mark_numeric" if is_numeric else "mark_free"
    from eXam.cost_tracker import track

    with track(op, test_id=test_id, student_id=student_id, question_id=meta["question_id"]):
        if is_numeric:
            return _mark_numeric_final(meta, submitted)
        return _mark_free_response(meta, submitted)


def _mark_per_leaf(
    meta: dict,
    submitted: dict[str, str],
    *,
    test_id: str | None,
    student_id: int,
) -> dict:
    """Mark each leaf independently and aggregate the totals."""
    leaves = meta.get("leaves") or []
    per_leaf: list[tuple[dict, dict]] = []
    for leaf in leaves:
        answer = submitted.get(leaf["number"], "")
        leaf_meta = _mark_one_leaf_meta(meta, leaf)
        verdict = _mark_leaf_dispatch(
            leaf_meta, answer, test_id=test_id, student_id=student_id,
        )
        per_leaf.append((leaf, verdict))
    total_assigned = sum(float(v["assigned_marks"]) for _, v in per_leaf)
    total_max = sum(float(v["max_marks"]) for _, v in per_leaf)
    if len(per_leaf) == 1:
        reasoning = per_leaf[0][1].get("reasoning", "")
    else:
        reasoning = "\n".join(
            f"{leaf['number_suffix']} {v.get('reasoning', '')}".strip()
            for leaf, v in per_leaf
        )
    return {
        "assigned_marks": total_assigned,
        "max_marks": total_max,
        "reasoning": reasoning,
    }


def mark(
    student_id: int,
    question_id: str,
    submitted: dict[str, str] | str,
    *,
    test_id: str | None = None,
) -> dict:
    """Top-level marking entry point. Returns {assigned_marks, max_marks, reasoning}.

    *submitted* accepts both shapes:
    - ``dict[str, str]`` (current practice page) — keyed by leaf number;
      marker iterates ``meta["leaves"]`` and grades each independently.
    - ``str`` (legacy class mode) — single answer for the whole top-level
      question; preserved verbatim until class mode is migrated.

    *test_id* threads through to ``track()`` so AI calls land in the
    ``ai_calls`` cost log. MCQ is deterministic and skips the tracker.
    """
    meta = question_metadata(question_id)
    if meta is None:
        return {
            "assigned_marks": 0.0,
            "max_marks": 0.0,
            "reasoning": "Question metadata missing.",
        }
    if isinstance(submitted, str):
        return _mark_legacy_single_string(
            meta, submitted, test_id=test_id, student_id=student_id,
        )
    return _mark_per_leaf(
        meta, submitted, test_id=test_id, student_id=student_id,
    )

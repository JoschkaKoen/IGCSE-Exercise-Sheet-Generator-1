"""Post-processing of the AI marking response.

Four helpers, called after :func:`xscore.marking.mark_page._mark_page` gets a
parseable response back from the model:

- :func:`_apply_marking_response` — parse the YAML, walk the blueprint, fill
  entries by ``_bq_key`` positional match; handles MCQ corrections,
  unfilled/unmatched bookkeeping, and the three tier-fallbacks (truncation
  repair, list-at-root, 1×1 flat-key) inline.
- :func:`_finalize_marking` — final validation pass: MCQ deterministic
  recompute, blank-answer default text, unmarked-question warnings, mark
  clamp to ``[0, max_marks]``.
- :func:`_normalize_mc_answer` — coerce any MCQ answer string into the
  canonical three-value enum (``A``/``B``/.../``not clear``/``no answer``).
- :func:`_fix_mc_marks` — recompute MCQ ``assigned_marks`` deterministically
  from the (normalised) student answer + ``correct_answer``.

Extracted from ``mark_page.py`` as part of the file-split into prompts /
call+retry / postprocess.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from xscore.marking.formats.base import FormatParseError, MarkingFormat
from xscore.marking.mark_page_prompts import _bq_key
from xscore.shared.terminal_ui import info_line


def _apply_marking_response(
    raw: str,
    blueprint: dict,
    fmt: "MarkingFormat",
) -> tuple[dict, list[str], list[str], list[dict]]:
    """Parse a raw marking response and apply it to *blueprint*.

    Pure-ish: parses *raw*, walks *blueprint*, fills entries by ``_bq_key``
    positional match, returns ``(result, unfilled, unmatched, mcq_corrections)``.
    Does NOT warn, does NOT MCQ-fix, does NOT clamp — those are caller
    responsibilities so that retry logic can run before final validation.
    Raises :class:`FormatParseError` if *raw* is unparseable.

    *mcq_corrections* is a list of ``{"number", "from", "to"}`` dicts, one per
    MCQ question where the AI emitted a ``corrected_student_answer`` letter
    different from the extracted ``student_answer``. Marks/explanation for
    MCQs are then deterministically recomputed by ``_fix_mc_marks`` against
    the (possibly corrected) letter.

    Idempotent on repeated calls against partially-filled blueprints — the
    inner walk only fills bp entries whose ``assigned_marks is None``, so a
    second invocation against a slim blueprint of previously-unfilled entries
    is a clean retry-merge.
    """
    try:
        parsed_questions = fmt.parse_response(raw)
    except FormatParseError:
        # Tier-1 fallback: truncation repair. Walks back from the end of the
        # response one line at a time, retrying yaml.safe_load until a valid
        # prefix appears. Recovers the maximum useful data when the stream
        # was cut mid-block-scalar (most often inside a `problem:` field).
        from xscore.shared.response_parsing import repair_truncated_marking_response
        parsed_questions = None
        repaired = repair_truncated_marking_response(raw)
        if repaired != raw:
            try:
                parsed_questions = fmt.parse_response(repaired)
                info_line(
                    f"Marking {blueprint.get('student_name', '?')} p{blueprint.get('page')}: "
                    f"truncation-repair recovered "
                    f"{len(parsed_questions)} of "
                    f"{len(blueprint.get('questions') or [])} question(s)"
                )
            except FormatParseError:
                parsed_questions = None
        if parsed_questions is None:
            # Tier-2 fallback (existing): model dropped the `questions:`
            # wrapper and emitted a list of question dicts at document root.
            # Each entry self-identifies via `number`, so this is positionally
            # unambiguous regardless of blueprint size — no 1×1 gating needed
            # (unlike parse_flat_fallback).
            list_fallback = fmt.parse_list_fallback(raw)
            if list_fallback is None:
                raise
            parsed_questions = list_fallback
            info_line(
                f"Marking {blueprint.get('student_name', '?')} p{blueprint.get('page')}: "
                f"list-at-root fallback rescued response (AI dropped `questions:` wrapper)"
            )
    # Fallback: model dropped the `questions:` wrapper and emitted the four
    # fill fields at document root. Safe only when the blueprint has exactly
    # one question — flat-keyed shape is otherwise positionally ambiguous.
    if not parsed_questions and len(blueprint.get("questions") or []) == 1:
        fallback_fields = fmt.parse_flat_fallback(raw)
        if fallback_fields is not None:
            bq0 = blueprint["questions"][0]
            parsed_questions = [{
                "number":      str(bq0.get("number", "")),
                "subpage_row": int(bq0.get("subpage_row", 1)),
                "subpage_col": int(bq0.get("subpage_col", 1)),
                **fallback_fields,
            }]
            info_line(
                f"Marking {blueprint.get('student_name', '?')} p{blueprint.get('page')}: "
                f"1×1 single-question fallback rescued response (AI dropped `questions:` wrapper)"
            )
    result = blueprint.copy()
    fill_groups: dict[tuple, list] = defaultdict(list)
    for q in parsed_questions:
        fill_groups[_bq_key(q)].append(q)

    fill_group_idx: dict[tuple, int] = defaultdict(int)
    unfilled: list[str] = []
    mcq_corrections: list[dict] = []
    for bq in result.get("questions", []):
        key = _bq_key(bq)
        idx = fill_group_idx[key]
        fill_group_idx[key] += 1
        # Skip bp entries that were already filled by an earlier pass — only
        # fill ones still pending. Lets the same function run idempotently as
        # a retry-merge against a slim blueprint of just-unfilled entries.
        if bq.get("assigned_marks") is not None:
            continue
        group = fill_groups.get(key, [])
        is_mcq = (bq.get("question_type") or "") == "multiple_choice"

        if idx >= len(group):
            # AI emitted nothing for this slot. MCQs are exempt from the
            # completeness retry — _fix_mc_marks computes their marks
            # deterministically from student_answer regardless of whether the
            # AI's response carried an entry, so a missing MCQ entry is a
            # missed correction opportunity, not a reason to retry.
            if not is_mcq:
                unfilled.append(bq.get("number"))
            continue

        fq = group[idx]

        if is_mcq:
            # Apply correction (regardless of whether AI emitted assigned_marks).
            corrected_raw = (fq.get("corrected_student_answer") or "").strip()
            if corrected_raw:
                new_value = _normalize_mc_answer(corrected_raw)
                original_value = _normalize_mc_answer(bq.get("student_answer"))
                if new_value != original_value and new_value not in ("no answer", "not clear"):
                    # Defense-in-depth guard: drop AI corrections that downgrade
                    # to no_answer/not_clear. Observed pattern across runs
                    # 2026-05-05_20-54-28 and 2026-05-05_20-33-40: ~all such
                    # downgrades were wrong (28 of 44 corrections were wrong
                    # downgrades per user review). Letter swaps and rescues
                    # (no_answer/not_clear → letter) still flow through. The
                    # AI's confidence/problem flags below still surface to
                    # human reviewers regardless of whether the answer changed.
                    mcq_corrections.append({
                        "number": bq.get("number"),
                        "from": original_value,
                        "to": new_value,
                    })
                    bq["student_answer"] = new_value
            # MCQs don't need assigned_marks/explanation from the AI; the
            # downstream _fix_mc_marks computes them. Side-channel signals
            # (confidence, problem) flow through to the per-page YAML.
            if "confidence" in fq:
                bq["confidence"] = fq["confidence"]
            if "problem" in fq:
                bq["problem"] = fq["problem"]
            continue

        if fq.get("assigned_marks") is None:
            # AI emitted this slot but with an unparseable / empty mark.
            # Treat as if the AI hadn't emitted it: leave bq unfilled so
            # the completeness retry re-asks. The fq slot is consumed
            # (idx already advanced) — intentional, otherwise a later bp
            # entry sharing the same key would be paired with the wrong
            # fq on the positional walk.
            unfilled.append(bq.get("number"))
            continue
        # Guarded: pre-fill from create_report (extract_student_answers) takes
        # precedence over the AI's re-emission in the marking response.
        # In presupplied mode the AI is told NOT to emit student_answer at
        # all — fall back to "" so the missing key doesn't crash the merge.
        if not bq.get("student_answer"):
            bq["student_answer"] = fq.get("student_answer", "")
        bq["assigned_marks"] = fq['assigned_marks']
        bq["explanation"] = fq['explanation']
        # Side-channel signals — copied from the AI response when
        # present. Read only by review_queue's confidence audit.
        if "confidence" in fq:
            bq["confidence"] = fq["confidence"]
        if "problem" in fq:
            bq["problem"] = fq["problem"]

    unmatched: list[str] = []
    for key, grp in fill_groups.items():
        excess = len(grp) - fill_group_idx.get(key, 0)
        for fq in grp[fill_group_idx.get(key, 0):fill_group_idx.get(key, 0) + max(0, excess)]:
            unmatched.append(fq.get("number") or str(key))

    return result, unfilled, unmatched, mcq_corrections


def _finalize_marking(result: dict, warn: Callable[[str], None]) -> None:
    """Run the final validation pass on a fully-merged marking result.

    Steps: MCQ deterministic recompute, blank-answer default text, unmarked-
    question surfacing, range clamp on ``assigned_marks``. Mutates *result* in
    place. Fires a warn for unmarked questions (AI failed to produce a mark
    after the completeness retry) and for out-of-range marks.
    """
    _fix_mc_marks(result)
    questions = result.get("questions", [])

    # Withdrawn questions (max_marks=0): force-zero, blank explanation/problem,
    # and skip the downstream blank-answer override and unmarked/clamp warnings.
    withdrawn_ids: set[int] = set()
    for bq in questions:
        if bq.get("max_marks") == 0:
            bq["assigned_marks"] = 0
            bq["explanation"] = ""
            bq["problem"] = ""
            withdrawn_ids.add(id(bq))

    for bq in questions:
        if id(bq) in withdrawn_ids:
            continue
        if not (bq.get("student_answer") or "").strip() and bq.get("assigned_marks") in (None, 0):
            bq["explanation"] = "Blank answer."
    for bq in questions:
        if id(bq) in withdrawn_ids:
            continue
        max_m = bq.get("max_marks")
        if max_m is None:
            continue
        m = bq.get("assigned_marks")
        if m is None:
            # AI never produced a mark for this question (and the completeness
            # retry didn't recover it). Default to 0 so totals are computable,
            # but tag the explanation so per-question reports flag it for
            # manual review rather than presenting a silent 0/max grade.
            warn(
                f"Marking {result.get('student_name', '?')}: "
                f"Q{bq.get('number')} unmarked after retry — "
                f"defaulted to 0 (manual review required)"
            )
            bq["assigned_marks"] = 0
            if (bq.get("student_answer") or "").strip():
                bq["explanation"] = (
                    "AI marking failed — defaulted to 0; manual review required."
                )
            # else: leave the "Blank answer." explanation set in the first loop
            continue
        if not isinstance(m, int) or m < 0 or m > int(max_m):
            warn(
                f"Marking {result.get('student_name', '?')}: "
                f"Q{bq.get('number')} assigned_marks={m} out of range "
                f"[0, {max_m}] — clamping"
            )
            try:
                m_int = int(m)
            except (TypeError, ValueError):
                m_int = 0
            bq["assigned_marks"] = max(0, min(m_int, int(max_m)))


def _normalize_mc_answer(s: str | None) -> str:
    """Coerce any MCQ answer string into the canonical three-value enum.

    Returns one of:
      - a single uppercase letter (A, B, C, …) — when the input begins with a letter
      - "not clear" — for "not clear", "unclear", or the legacy "?" sentinel
      - "no answer" — for "no answer", empty/whitespace, or any non-alphabetic input

    Used by both `_apply_marking_response` (when validating
    `corrected_student_answer`) and `_fix_mc_marks` (when finalising
    `student_answer`) so the two functions agree on what each input maps to.
    """
    s = (s or "").strip()
    sl = s.lower()
    if sl in ("not clear", "unclear", "?"):
        return "not clear"
    if sl in ("no answer", ""):
        return "no answer"
    if s[:1].isalpha():
        return s[0].upper()
    return "no answer"


def _fix_mc_marks(result: dict) -> None:
    """Normalise student_answer and recompute assigned_marks for MCQ questions in-place.

    The AI does not award MCQ marks; this function does it deterministically by
    comparing the (already-normalised) student_answer against correct_answer.

    student_answer is coerced into the three-value enum via _normalize_mc_answer:
    a single uppercase letter, "not clear", or "no answer". Marks: max_marks for
    a correct letter; 0 for any other case ("not clear", "no answer", or a wrong
    letter). Explanation: "Correct.", "Incorrect.", "Unclear answer — flagged
    for review.", or "No answer." respectively.

    Keyed by question_text (not number) because duplicate question numbers
    (e.g. two Q38s on the same page) share the same stripped number after
    _2 is removed from blueprints.
    """
    mc_correct: dict[str, str] = {
        (q.get("question_text") or "").strip(): str(q.get("correct_answer") or "").strip().upper()
        for q in result.get("questions", [])
        if q.get("question_type") == "multiple_choice" and q.get("correct_answer")
    }
    if not mc_correct:
        return
    for q in result.get("questions", []):
        qt = (q.get("question_text") or "").strip()
        if qt not in mc_correct:
            continue
        student_ans = _normalize_mc_answer(q.get("student_answer"))
        q["student_answer"] = student_ans
        max_m = int(q.get("max_marks") or 1)

        if student_ans == "not clear":
            q["assigned_marks"] = 0
            q["explanation"] = "Unclear answer — flagged for review."
        elif student_ans == "no answer":
            q["assigned_marks"] = 0
            q["explanation"] = "No answer."
        else:
            correct = student_ans == mc_correct[qt]
            q["assigned_marks"] = max_m if correct else 0
            q["explanation"] = "Correct." if correct else "Incorrect."

"""Step 16: per-student page-order check — heuristic over step 14's handwriting.json.

For every scan page, step 14's vision call already detected the printed page
number and whether the page is a cover. This step joins that data with the
per-student page_numbers from step 15 and verifies that each student's
sequence of detected page numbers matches what the empty-exam layout
expects. No OCR, no LLM call.

The dispatcher in ``xscore/steps/geometry.py`` is the single policy layer:
this module returns ``(PageOrderStatus, message)`` and never calls SystemExit
or prints. INCONCLUSIVE covers every path that today silently fails open
(missing handwriting.json, parse error, model returned None for too many
pages to draw a conclusion).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xscore.shared.models import PageAssignment


class PageOrderStatus(Enum):
    PASSED = "PASSED"
    MISMATCH_FOUND = "MISMATCH_FOUND"
    INCONCLUSIVE = "INCONCLUSIVE"


_OCR_CONF_THRESHOLD = 0.8
_OCR_LOW_KEPT_RATIO = 0.3
_OCR_BAD_PAGE_FRACTION = 0.5


# ─────────── OCR + text extraction ───────────────────────────────────────────

def _exam_page_texts(exam_pdf: Path) -> list[str]:
    import fitz
    with fitz.open(str(exam_pdf)) as doc:
        return [doc[i].get_text().strip() for i in range(doc.page_count)]


def _scan_page_texts(
    scan_pdf: Path,
    page_nums: list[int],  # 1-based
    dpi: int = 150,
) -> list[tuple[str, int, int]]:
    """OCR each page; return (joined_text, kept_words, total_words) per page.

    ``kept_words`` is the count after the confidence filter
    (``> _OCR_CONF_THRESHOLD``); ``total_words`` is the unfiltered count.
    """
    import fitz
    from xscore.preprocessing.assign_pages_to_students import _get_ocr

    def _ocr_one(p: int) -> tuple[str, int, int]:
        with fitz.open(str(scan_pdf)) as doc:
            pix = doc[p - 1].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        png_bytes = pix.tobytes("png")
        pix = None  # release ~2 MB pixmap before the OCR call
        ocr_out, _ = _get_ocr()(png_bytes)
        png_bytes = None
        items = list(ocr_out or [])
        total = len(items)
        kept = [t for _, t, c in items if float(c) > _OCR_CONF_THRESHOLD]
        return "\n".join(kept), len(kept), total

    # RapidOCR's ONNX inference uses all cores for one call, but Python-side
    # preprocessing between detection/classification/recognition models is
    # single-threaded — so parallel callers help fill those gaps up to ~cpu_count.
    # Beyond that it's pure memory bloat. Each worker holds ~20 MB in flight,
    # so cpu_count workers ≈ a few hundred MB peak — cheap on modern machines.
    # PAGE_ORDER_OCR_WORKERS overrides for tuning.
    default_ocr_workers = os.cpu_count() or 8
    workers = min(
        len(page_nums),
        int(os.environ.get("PAGE_ORDER_OCR_WORKERS", str(default_ocr_workers))),
    ) or 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_ocr_one, page_nums))


def _format_text_artifact(sections: list[tuple[str, str]]) -> str:
    parts = []
    for label, text in sections:
        parts.append(f"=== {label} ===\n{text or '(no text)'}")
    return "\n\n".join(parts)


# ─────────── Prompt construction ─────────────────────────────────────────────

def _build_per_student_prompt(exam_texts: list[str], student_data: dict) -> str:
    from xscore.prompts.loader import load_prompt

    exam_lines: list[str] = []
    for i, text in enumerate(exam_texts, 1):
        exam_lines += [f"Page {i}:", text or "(no printed text)", ""]
    exam_pages_block = "\n".join(exam_lines)

    student_lines: list[str] = []
    for pos, (scan_p, text) in enumerate(
        zip(student_data["scan_pages"], student_data["texts"]), 1
    ):
        student_lines += [f"  Position {pos} (scan page {scan_p}):", f"  {text or '(no text)'}", ""]
    student_pages_block = "\n".join(student_lines)

    _, body = load_prompt(
        "page_order_check",
        n_exam_pages=len(exam_texts),
        exam_pages_block=exam_pages_block,
        student_name=student_data["name"],
        student_pages_block=student_pages_block,
    )
    return body.rstrip("\n")


# ─────────── Model client (built once, shared by per-student calls) ──────────

class _ClientState:
    """Holds whichever model client is appropriate for ``model_id``.

    Built once before the parallel per-student loop so we don't re-init the
    client (and re-validate creds) per student × per retry attempt.
    """

    def __init__(self, gai: Any, oa: Any, provider: str | None) -> None:
        self.gai = gai
        self.oa = oa
        self.provider = provider


def _build_client_state(model_id: str) -> _ClientState | str:
    """Return ``_ClientState`` on success, or a human-readable error message string."""
    if model_id.startswith("gemini"):
        from eXercise.ai_client import make_gemini_native_client
        gai = make_gemini_native_client()
        if gai is None:
            return "GEMINI_API_KEY not set"
        return _ClientState(gai=gai, oa=None, provider="gemini")
    from eXercise.ai_client import make_ai_client
    result = make_ai_client(model_env="", default_model=model_id)
    if result is None:
        return f"PAGE_ORDER_CHECK_MODEL={model_id} requires API key for its provider"
    oa, _, provider, _, _ = result
    return _ClientState(gai=None, oa=oa, provider=provider)


def _call_gemini(state: _ClientState, prompt: str, model_id: str, thinking: int | None, max_tok: int | None) -> tuple[str, str]:
    from eXercise.ai_client import build_gemini_thinking_config
    from google.genai import types as gai_types
    cfg_kwargs: dict = {
        "max_output_tokens": max_tok or 2048,
        "response_mime_type": "application/json",
    }
    if thinking is not None:
        cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking)
    resp = state.gai.models.generate_content(
        model=model_id,
        contents=[gai_types.Part.from_text(text=prompt)],
        config=gai_types.GenerateContentConfig(**cfg_kwargs),
    )
    from eXercise.ai_client import split_gemini_response
    return split_gemini_response(resp)


def _call_openai_compat(state: _ClientState, prompt: str, model_id: str, thinking: int | None, max_tok: int | None) -> tuple[str, str]:
    from eXercise.ai_client import build_completion_kwargs, collect_streamed_response
    use_stream, kw = build_completion_kwargs(state.provider, thinking, max_tok or 2048)
    msgs = [{"role": "user", "content": prompt}]
    if use_stream:
        _th: list[str] = []
        stream = state.oa.chat.completions.create(model=model_id, messages=msgs, stream=True, **kw)
        return collect_streamed_response(stream, thinking_out=_th), "".join(_th)
    try:
        resp = state.oa.chat.completions.create(
            model=model_id, messages=msgs,
            response_format={"type": "json_object"}, **kw,
        )
    except Exception:  # noqa: BLE001
        resp = state.oa.chat.completions.create(model=model_id, messages=msgs, **kw)
    raw = resp.choices[0].message.content or ""
    thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
    return raw, thinking_text


def _call_model_with_retry(
    state: _ClientState,
    prompt: str,
    model_id: str,
    thinking: int | None,
    max_tok: int | None,
) -> tuple[str, str, str | None]:
    """Return ``(raw_text, thinking_text, error_message)``. ``raw_text`` is "" on permanent failure."""
    from eXercise.api_retry import retry_api_call

    def _do_call() -> tuple[str, str]:
        if model_id.startswith("gemini"):
            return _call_gemini(state, prompt, model_id, thinking, max_tok)
        return _call_openai_compat(state, prompt, model_id, thinking, max_tok)

    try:
        raw, thinking_text = retry_api_call(_do_call, label=f"Page order check ({model_id})")
        return raw, thinking_text, None
    except Exception as exc:
        return "", "", f"{type(exc).__name__}: {exc}"


# ─────────── Response validation ─────────────────────────────────────────────

def _validate_per_student_response(raw: str) -> tuple[PageOrderStatus, str | None, list | None]:
    """Returns (status, message, issues_list_or_None)."""
    if not raw:
        return PageOrderStatus.INCONCLUSIVE, "empty response from model", None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return PageOrderStatus.INCONCLUSIVE, f"JSON parse failed: {exc}", None
    if not isinstance(data, dict):
        return PageOrderStatus.INCONCLUSIVE, "response is not a JSON object", None
    ok = data.get("ok")
    if not isinstance(ok, bool):
        return PageOrderStatus.INCONCLUSIVE, "missing or non-bool 'ok' field", None
    if ok:
        return PageOrderStatus.PASSED, None, []
    issues = data.get("issues")
    if not isinstance(issues, list):
        return PageOrderStatus.INCONCLUSIVE, "ok=False but 'issues' is missing or not a list", None
    valid: list = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        try:
            pos = int(it.get("position"))
            scan = int(it.get("scan_page"))
        except (TypeError, ValueError):
            continue
        valid.append({
            "position": pos,
            "scan_page": scan,
            "expected": str(it.get("expected", "?")),
            "found": str(it.get("found", "?")),
            "detail": str(it.get("detail", "")),
        })
    if not valid:
        return PageOrderStatus.INCONCLUSIVE, "ok=False but every issue is malformed", None
    return PageOrderStatus.MISMATCH_FOUND, None, valid


# ─────────── Aggregation ─────────────────────────────────────────────────────

_PerStudentResult = tuple[str, PageOrderStatus, str | None, list | None]


def _aggregate(
    results: list[_PerStudentResult],
    ocr_warning: str | None,
) -> tuple[PageOrderStatus, str | None]:
    n = len(results)
    mismatches = [
        (name, issues)
        for name, status, _, issues in results
        if status == PageOrderStatus.MISMATCH_FOUND
    ]
    inconclusives = [
        (name, msg)
        for name, status, msg, _ in results
        if status == PageOrderStatus.INCONCLUSIVE
    ]
    if mismatches:
        lines = ["Scan page order / content mismatch:", ""]
        for name, issues in mismatches:
            lines.append(f"  {name}:")
            for issue in issues or []:
                lines.append(
                    f"    Position {issue['position']} (scan page {issue['scan_page']}): "
                    f"{issue['detail']}  —  expected: {issue['expected']} / found: {issue['found']}"
                )
        if inconclusives:
            lines += ["", "  Could not verify (model failed):"]
            for name, msg in inconclusives:
                lines.append(f"    {name}: {msg}")
        if ocr_warning:
            lines += ["", f"  {ocr_warning}"]
        lines += ["", "  Re-scan the affected booklet(s) in the correct page order and re-run."]
        return PageOrderStatus.MISMATCH_FOUND, "\n".join(lines)
    if inconclusives:
        verified = n - len(inconclusives)
        lines = [f"Verified {verified}/{n} students; could not verify:"]
        for name, msg in inconclusives:
            lines.append(f"    {name}: {msg}")
        if ocr_warning:
            lines += ["", f"  {ocr_warning}"]
        return PageOrderStatus.INCONCLUSIVE, "\n".join(lines)
    return PageOrderStatus.PASSED, None


def _ocr_quality_summary(students_data: list[dict]) -> str | None:
    bad = 0
    total = 0
    for sd in students_data:
        for kept, all_n in sd.get("ocr_stats", []):
            total += 1
            if all_n == 0 or (kept / all_n) < _OCR_LOW_KEPT_RATIO:
                bad += 1
    if total > 0 and bad / total > _OCR_BAD_PAGE_FRACTION:
        return (
            f"page-order OCR: {bad}/{total} pages have <{int(_OCR_LOW_KEPT_RATIO * 100)}%"
            " high-confidence words — check may be unreliable"
        )
    return None


# ─────────── Main entry ──────────────────────────────────────────────────────

def check_page_order(
    exam_pdf: Path,
    scan_pdf: Path,
    page_assignments: list["PageAssignment"],
    artifact_dir: Path | None = None,
) -> tuple[PageOrderStatus, str | None]:
    """Validate page order from step 14's per-page page-number detections.

    Heuristic only — no LLM, no OCR. For each student, looks up the AI-detected
    printed page number from ``13_student_handwriting/handwriting.json`` for
    every scan page they own, computes the expected sequence using
    ``cover_offset`` from the same metadata block, and flags students whose
    detected sequence disagrees with the expected one.

    ``exam_pdf`` and ``scan_pdf`` are kept in the signature for compat with
    the dispatcher; both are unused by the new implementation.
    """
    del exam_pdf, scan_pdf  # legacy params, retained for dispatcher compat

    if artifact_dir is None:
        return PageOrderStatus.INCONCLUSIVE, "no artifact_dir provided"

    from xscore.shared.exam_paths import artifact_handwriting_json_path
    from xscore.shared.terminal_ui import (
        format_duration,
        info_line,
        ok_line,
        warn_line,
    )

    hw_path = artifact_handwriting_json_path(artifact_dir)
    if not hw_path.exists():
        return (
            PageOrderStatus.INCONCLUSIVE,
            "step 13 artifact not found (13_student_handwriting/handwriting.json); "
            "run student_handwriting_check first",
        )
    try:
        hw_data = json.loads(hw_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return PageOrderStatus.INCONCLUSIVE, f"could not read handwriting.json: {exc}"

    metadata = hw_data.get("metadata", {})
    cover_offset = int(metadata.get("cover_offset", 0))
    by_scan: dict[int, dict] = {
        int(entry["scan_page"]): entry
        for entry in hw_data.get("scan_pages", [])
        if entry.get("scan_page") is not None
    }

    info_line(
        f"Checking page order for {len(page_assignments)} student"
        f"{'s' if len(page_assignments) != 1 else ''} (heuristic) …"
    )

    name_width = max(
        (len(a.student_name or "Unknown") for a in page_assignments),
        default=1,
    )

    issue_total: list[dict] = []
    inconclusive: list[str] = []
    passed = 0

    t_start = time.perf_counter()
    for a in page_assignments:
        has_cover = a.cover_page_number is not None
        student_issues: list[dict] = []
        n_no_pn = 0
        for p_label, scan_page in enumerate(a.page_numbers, 1):
            entry = by_scan.get(scan_page)
            if has_cover and p_label == 1:
                # Cover page: AI should say is_cover_page=True, no printed
                # page number expected.
                if entry is not None and entry.get("is_cover_page") is False:
                    student_issues.append({
                        "scan_page": scan_page,
                        "expected": "cover",
                        "detected": (
                            f"page {entry.get('detected_page_number')}"
                            if entry.get("detected_page_number") is not None
                            else "non-cover"
                        ),
                    })
                continue
            expected_pn = p_label - cover_offset
            if expected_pn < 1:
                continue  # before-first-page; nothing to check
            if entry is None:
                n_no_pn += 1
                continue
            detected_pn = entry.get("detected_page_number")
            if detected_pn is None:
                n_no_pn += 1
                continue
            if entry.get("is_cover_page") is True:
                student_issues.append({
                    "scan_page": scan_page,
                    "expected": f"page {expected_pn}",
                    "detected": "cover",
                })
                continue
            if int(detected_pn) != expected_pn:
                student_issues.append({
                    "scan_page": scan_page,
                    "expected": f"page {expected_pn}",
                    "detected": f"page {detected_pn}",
                })

        name_quoted = f"{a.student_name!r}"
        if student_issues:
            issue_total.extend({"student": a.student_name, **i} for i in student_issues)
            warn_line(
                f"{name_quoted:<{name_width + 2}}  ·  "
                f"page order MISMATCH ({len(student_issues)} issue"
                f"{'s' if len(student_issues) != 1 else ''})"
            )
        elif n_no_pn > 0 and n_no_pn >= len(a.page_numbers) - (1 if has_cover else 0):
            inconclusive.append(a.student_name)
            warn_line(
                f"{name_quoted:<{name_width + 2}}  ·  "
                f"inconclusive: AI returned no page number on {n_no_pn} pages"
            )
        else:
            passed += 1
            ok_line(f"{name_quoted:<{name_width + 2}}  ·  page order OK")

    dur = format_duration(time.perf_counter() - t_start)

    if issue_total:
        sample = "; ".join(
            f"{i['student']} scan {i['scan_page']}: {i['detected']} (expected {i['expected']})"
            for i in issue_total[:5]
        )
        more = (
            f" (and {len(issue_total) - 5} more)"
            if len(issue_total) > 5 else ""
        )
        return (
            PageOrderStatus.MISMATCH_FOUND,
            f"page-order mismatches detected in {dur}: {sample}{more}",
        )
    if inconclusive:
        return (
            PageOrderStatus.INCONCLUSIVE,
            f"{len(inconclusive)} student(s) had insufficient page-number detections "
            f"to verify order: {', '.join(inconclusive[:10])}",
        )
    return (
        PageOrderStatus.PASSED,
        f"{passed}/{len(page_assignments)} students — page order OK ({dur})",
    )

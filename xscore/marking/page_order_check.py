"""Step 13: per-student page-order check (parallel calls vs empty-exam baseline).

The dispatcher in ``xscore/steps/geometry.py`` is the single policy layer:
this module returns ``(PageOrderStatus, message)`` and never calls SystemExit
or prints. INCONCLUSIVE covers every path that today silently fails open
(parse error, missing creds, API exception, image-only exam PDF, model
omitting students from the response).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
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
    # Beyond that it's pure memory bloat. Cap at 8 to bound peak in-flight
    # pixmap + PNG-bytes + OCR-buffer memory; PAGE_ORDER_OCR_WORKERS overrides.
    default_ocr_workers = min(os.cpu_count() or 4, 8)
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
    """Validate page order and content for all students, in parallel per student.

    Returns ``(status, message)``. The dispatcher in ``geometry.py`` is the
    single policy layer; this function never calls SystemExit and never prints.
    """
    from eXercise.ai_client import parse_model_spec
    from xscore.shared.exam_paths import (
        artifact_page_order_empty_exam_txt_path,
        artifact_page_order_prompt_path,
        artifact_page_order_txt_path,
    )
    from xscore.shared.prompt_logger import save_prompt, save_response

    model_id, thinking, max_tok = parse_model_spec(
        os.environ.get("PAGE_ORDER_CHECK_MODEL", "qwen3.6-flash, 0, 2048")
    )

    # ── Empty exam baseline (with image-PDF guard) ────────────────────────
    exam_texts = _exam_page_texts(exam_pdf)
    if sum(len(t) for t in exam_texts) < 100:
        return (
            PageOrderStatus.INCONCLUSIVE,
            "empty exam PDF has no extractable text layer; "
            "export the empty exam with a text layer or skip step 13",
        )

    if artifact_dir:
        po_empty = artifact_page_order_empty_exam_txt_path(artifact_dir)
        po_empty.parent.mkdir(parents=True, exist_ok=True)
        po_empty.write_text(
            _format_text_artifact([(f"Page {i}", t) for i, t in enumerate(exam_texts, 1)]),
            encoding="utf-8",
        )

    # ── Build the model client once ───────────────────────────────────────
    client_or_err = _build_client_state(model_id)
    if isinstance(client_or_err, str):
        return PageOrderStatus.INCONCLUSIVE, client_or_err
    client_state = client_or_err

    # ── OCR all scan pages once, in parallel ──────────────────────────────
    all_page_nums: list[int] = []
    for a in page_assignments:
        all_page_nums.extend(a.page_numbers)
    ocr_results = _scan_page_texts(scan_pdf, all_page_nums)
    page_text_map: dict[int, tuple[str, int, int]] = dict(zip(all_page_nums, ocr_results))

    students_data: list[dict] = []
    for a in page_assignments:
        triples = [page_text_map[p] for p in a.page_numbers]
        texts = [t for t, _, _ in triples]
        ocr_stats = [(k, n) for _, k, n in triples]
        students_data.append({
            "name": a.student_name,
            "scan_pages": a.page_numbers,
            "texts": texts,
            "ocr_stats": ocr_stats,
        })
        if artifact_dir:
            po_student = artifact_page_order_txt_path(artifact_dir, a.student_name)
            po_student.parent.mkdir(parents=True, exist_ok=True)
            po_student.write_text(
                _format_text_artifact([
                    (f"Position {pos} (scan page {sp}, OCR: {k}/{n} high-conf words)", t)
                    for pos, (sp, t, (k, n)) in enumerate(
                        zip(a.page_numbers, texts, ocr_stats), 1
                    )
                ]),
                encoding="utf-8",
            )

    # ── Per-student model calls in parallel ───────────────────────────────
    def _check_one(sd: dict) -> _PerStudentResult:
        prompt = _build_per_student_prompt(exam_texts, sd)
        save_path = (
            artifact_page_order_prompt_path(artifact_dir, sd["name"])
            if artifact_dir else None
        )
        save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt}])
        raw, thinking_text, err = _call_model_with_retry(client_state, prompt, model_id, thinking, max_tok)
        if save_path is not None and raw:
            save_response(save_path, raw, thinking=thinking_text)
        if not raw:
            reason = f"model call failed: {err}" if err else "model returned empty response"
            return sd["name"], PageOrderStatus.INCONCLUSIVE, reason, None
        status, msg, issues = _validate_per_student_response(raw)
        return sd["name"], status, msg, issues

    workers = min(len(students_data), int(os.environ.get("PAGE_ORDER_WORKERS", "500"))) or 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_check_one, students_data))

    return _aggregate(results, _ocr_quality_summary(students_data))

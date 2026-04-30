"""Per-scan-file orientation detection via vision LLM (default gemini-3-flash-preview).

Samples up to ``SCAN_ORIENTATION_SAMPLE_PAGES`` pages spread across each
source PDF (rendered at 300 DPI, JPEG quality 95), queries the configured
vision model on each non-blank page, and majority-votes the answer. The
result is a clockwise rotation (0/90/180/270) that uprights the page, plus a
``source`` tag so callers can distinguish a confident model answer
(``"model"``) from a fallback (``"fallback"``), and a ``votes`` tuple
recording the per-page raw answers.

Provider dispatch: model names starting with ``gemini`` go through the
native ``google.genai`` SDK with ``Part.from_bytes``; everything else uses
the OpenAI-compat path with ``image_url`` content parts (Qwen and friends).

Concurrency: files are processed sequentially so terminal output stays
contiguous per file; within a file the per-page AI calls run in parallel
(capped at 8 workers) while fitz rendering stays in the outer thread (fitz
``Document``s aren't reliably thread-safe across page operations).

Used by :mod:`xscore.preprocessing.coordinator`'s ``prepare_scans`` phase
before duplex merge / single-PDF write, so steps 5–7 see correctly-oriented
input. Each file's failure is isolated — one bad file does not affect the
others' results, and a missing API key degrades gracefully (rotation 0,
loud warn) rather than crashing the pipeline.
"""

from __future__ import annotations

import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from PIL import Image

ROTATION_DETECTION_DPI = 300
_VALID_ROTATIONS = {0, 90, 180, 270}
_INNER_POOL_MAX_WORKERS = 8  # cap on parallel AI calls per file


def _sample_pages_per_file() -> int:
    """Read the configured per-file sample count, clamped to >= 1.

    Read at call time rather than import time so a runtime ``os.environ``
    override (tests, ad-hoc CLI runs) is honoured immediately.
    """
    from xscore.config import SCAN_ORIENTATION_SAMPLE_PAGES  # noqa: PLC0415
    return max(1, int(SCAN_ORIENTATION_SAMPLE_PAGES))

_SYSTEM = (
    "You analyse scanned exam pages. Look at the image and identify where the "
    "TOP of the page is — i.e. where the page header (page number, exam code, "
    "title) appears. Answer with a single edge label:\n"
    "  - \"top\":    image is already upright (header at image top)\n"
    "  - \"right\":  page is rotated 90° counter-clockwise (header at image right)\n"
    "  - \"bottom\": page is upside down (header at image bottom)\n"
    "  - \"left\":   page is rotated 90° clockwise (header at image left)\n"
    "Reply with strict JSON only: {\"page_top_at\": \"top\"|\"right\"|\"bottom\"|\"left\"}."
)
_USER = (
    "Where is the TOP of the page in this image? Look for the page header "
    "(page number, exam code, title). Reply with JSON only: "
    "{\"page_top_at\": \"top\"|\"right\"|\"bottom\"|\"left\"}."
)

# Edge → CW rotation needed to put that edge at the top.
# top→0 (already upright), right→90 (rotate CW to bring right to top is CCW=270... wait)
# Actually: if page top is currently at IMAGE right, we need to rotate the image
# 90° COUNTER-clockwise (= 270° clockwise) to bring the page top to the image top.
_EDGE_TO_CW: dict[str, int] = {
    "top":    0,
    "right":  270,   # rotate 90° CCW (= 270° CW) to bring right→top
    "bottom": 180,
    "left":   90,    # rotate 90° CW to bring left→top
}

_ROTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "page_top_at": {"type": "string", "enum": ["top", "right", "bottom", "left"]},
    },
    "required": ["page_top_at"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class PageVote:
    """One page's raw orientation answer, used to build OrientationResult.votes."""

    page_idx: int
    edge: str          # canonical lowercase: "top" | "right" | "bottom" | "left"
    rotation_cw: int   # 0 | 90 | 180 | 270


@dataclass(frozen=True)
class OrientationResult:
    """Result of one orientation-detection call.

    ``source`` is ``"model"`` for a confident model answer, ``"fallback"``
    for any failure path (no API key, parse error, all candidate pages
    blank, etc.). ``reason`` is populated only when ``source == "fallback"``.

    ``model`` is the resolved model id from :func:`make_ai_client`, or
    ``None`` when no client was constructed (e.g. missing API key).

    ``votes`` carries the per-page raw answers in page-index order.  Empty
    on the fallback path (we never reached the AI calls).
    """

    rotation_cw: int
    source: str
    reason: Optional[str] = None
    model: Optional[str] = None
    votes: tuple[PageVote, ...] = field(default_factory=tuple)


def _fallback(reason: str, model: Optional[str] = None) -> OrientationResult:
    return OrientationResult(rotation_cw=0, source="fallback", reason=reason, model=model)


def _pick_candidate_pages(
    doc: fitz.Document, max_samples: int | None = None
) -> list[int]:
    """Return page indices spread across *doc*, ordered for majority-vote sampling.

    For long files we sample evenly across the document; for short files we
    return as many distinct indices as exist.

    Special cases:
    - ``n == 0``: empty list (caller should fall back).
    - ``n <= max_samples``: return ``[0..n-1]``.
    - ``max_samples == 1``: return ``[n // 2]`` — single-page detection picks
      a middle page (typically content-rich) rather than p0 (often a sparse
      cover) or p-1 (often a blank back-cover with sparse copyright text).
    """
    if max_samples is None:
        max_samples = _sample_pages_per_file()
    n = len(doc)
    if n == 0:
        return []
    if n <= max_samples:
        return list(range(n))
    if max_samples == 1:
        return [n // 2]
    # Evenly-spaced indices across [0, n-1], deduplicated.
    raw = [round(i * (n - 1) / (max_samples - 1)) for i in range(max_samples)]
    seen: set[int] = set()
    out: list[int] = []
    for i in raw:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


_JPEG_QUALITY = 95  # never below 90 (preserves text clarity for orientation detection)


def _render_jpeg_b64(page: fitz.Page, dpi: int = ROTATION_DETECTION_DPI) -> tuple[str, Image.Image]:
    """Render *page* at *dpi* and return ``(base64_jpeg_string, pil_image)``.

    The PIL image is returned so the caller can also run the blank check on
    the same render (avoids rendering twice).
    """
    from xscore.extraction.images import to_jpeg_bytes  # noqa: PLC0415

    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csRGB)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    jpg = to_jpeg_bytes(img, quality=_JPEG_QUALITY)
    return base64.b64encode(jpg).decode("ascii"), img


def _parse_rotation(raw_text: str) -> tuple[str, int]:
    """Parse JSON, extract page_top_at edge label, map to CW rotation needed.

    Returns ``(canonical_edge_lowercase, rotation_cw_degrees)``. Raises
    ValueError on any problem (callers in this module either consume the
    tuple or use the call purely for validation).
    """
    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
    if "page_top_at" not in parsed:
        raise ValueError(f"missing page_top_at in {parsed!r}")
    edge = parsed["page_top_at"]
    if not isinstance(edge, str):
        raise ValueError(f"non-string page_top_at: {edge!r}")
    edge_lower = edge.strip().lower()
    if edge_lower not in _EDGE_TO_CW:
        raise ValueError(f"out-of-range page_top_at: {edge!r}")
    return edge_lower, _EDGE_TO_CW[edge_lower]


def detect_scan_orientation(scan_pdf: Path) -> OrientationResult:
    """Single Qwen call on the first non-blank page of *scan_pdf*.

    Always returns a result — never raises. On any failure path returns
    ``OrientationResult(0, "fallback", "<why>")`` and emits a ``warn_line``.
    """
    from xscore.preprocessing.remove_blanks_autorotate import is_blank_page  # noqa: PLC0415
    from xscore.shared.terminal_ui import warn_line  # noqa: PLC0415

    try:
        return _detect_one(scan_pdf, is_blank_page=is_blank_page)
    except Exception as exc:  # noqa: BLE001 — final safety net; helper logs already
        warn_line(f"Orientation: {scan_pdf.name} unexpected error: {exc!r} — using 0°")
        return _fallback(f"unexpected: {exc!r}")


def _make_openai_compat_caller(
    model: str, thinking_tokens: int | None, max_tokens: int | None
):
    """Return a ``(b64) -> raw_text`` callable that hits the OpenAI-compat
    chat completions endpoint with the prompt+image, handling the
    json_schema → json_object → plain format cascade. Returns None if no API key.
    """
    from eXercise.ai_client import (  # noqa: PLC0415
        build_completion_kwargs,
        collect_streamed_response,
        make_ai_client,
        provider_supports_json_schema_with_system,
    )

    # The OpenAI-compat branch is only reached when the resolved model name
    # is non-Gemini, so the default here is just a Qwen fallback for the
    # rare path where both env vars are unset and a non-Gemini default is
    # desired. The primary default (``gemini-3-flash-preview``) is set in
    # ``_detect_one``'s env-resolution above.
    result = make_ai_client(
        model_env="SCAN_ORIENTATION_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_model="qwen3.6-flash",
    )
    if result is None:
        return None
    client, resolved_model, provider, _thinking, _max_tokens = result
    use_stream, kw = build_completion_kwargs(
        provider, _thinking, _max_tokens or 256
    )
    strict_rf = {
        "type": "json_schema",
        "json_schema": {
            "name": "page_orientation",
            "schema": _ROTATION_SCHEMA,
            "strict": True,
        },
    }

    def _call(b64: str) -> str:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _USER},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            },
        ]
        formats: list[tuple[str, dict | None]] = []
        if provider_supports_json_schema_with_system(provider):
            formats.append(("json_schema", strict_rf))
        formats.append(("json_object", {"type": "json_object"}))
        formats.append(("plain", None))
        for _fmt_name, fmt_rf in formats:
            try:
                if use_stream:
                    stream = client.chat.completions.create(
                        model=resolved_model, messages=messages, stream=True, **kw,
                    )
                    raw = collect_streamed_response(stream)
                else:
                    extra = {"response_format": fmt_rf} if fmt_rf is not None else {}
                    resp = client.chat.completions.create(
                        model=resolved_model, messages=messages, **extra, **kw,
                    )
                    raw = resp.choices[0].message.content or ""
                _parse_rotation(raw)
                return raw
            except (json.JSONDecodeError, ValueError):
                continue
        raise ValueError("all formats failed (no parseable JSON)")

    return _call


def _make_gemini_caller(
    model: str, thinking_tokens: int | None, max_tokens: int | None
):
    """Return a ``(b64) -> raw_text`` callable that hits Gemini's native SDK
    with the prompt+image, using ``response_schema`` to constrain the output
    to ``{"page_top_at": "top|right|bottom|left"}``. Returns None if no key.
    """
    from eXercise.ai_client import (  # noqa: PLC0415
        build_gemini_thinking_config,
        make_gemini_native_client,
        split_gemini_response,
    )

    gai_client = make_gemini_native_client()
    if gai_client is None:
        return None

    from google.genai import types as gai_types  # noqa: PLC0415

    # Gemini's response_schema rejects additionalProperties — strip for this path.
    _gemini_schema = {k: v for k, v in _ROTATION_SCHEMA.items() if k != "additionalProperties"}
    cfg_kwargs: dict = {
        "system_instruction": _SYSTEM,
        "max_output_tokens": max_tokens or 256,
        "response_mime_type": "application/json",
        "response_schema": _gemini_schema,
    }
    if thinking_tokens is not None:
        cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
    config = gai_types.GenerateContentConfig(**cfg_kwargs)

    def _call(b64: str) -> str:
        contents = [
            gai_types.Part.from_bytes(
                data=base64.b64decode(b64), mime_type="image/jpeg"
            ),
            gai_types.Part.from_text(text=_USER),
        ]
        resp = gai_client.models.generate_content(
            model=model, contents=contents, config=config,
        )
        raw, _thinking = split_gemini_response(resp)
        # Validate response shape; raise on parse failure so retry_api_call
        # can decide whether to retry (it won't on ValueError, which is fine
        # — Gemini's response_schema should make this very rare).
        _parse_rotation(raw)
        return raw

    return _call


def _detect_one(scan_pdf: Path, *, is_blank_page) -> OrientationResult:
    """Inner body of :func:`detect_scan_orientation`. May raise; outer wraps.

    Samples up to ``SCAN_ORIENTATION_SAMPLE_PAGES`` pages spread across the
    file, queries the configured vision model on each non-blank page in
    parallel (capped at ``_INNER_POOL_MAX_WORKERS``), then **majority-votes**
    across the answers. Single-page errors (model confused by a sparse cover
    page or an outlier image) are dominated by the consistent answer from
    the rest of the pages.

    Per-page progress is **not** logged here — emission lives in
    :func:`detect_scan_orientations` so it can sit between the file-name
    header and the decision line.
    """
    from eXercise.api_retry import retry_api_call  # noqa: PLC0415
    from xscore.shared.terminal_ui import warn_line  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415

    if not scan_pdf.is_file():
        warn_line(f"Orientation: {scan_pdf.name} not found — using 0°")
        return _fallback(f"file not found: {scan_pdf}")

    # 1. Render up to N candidate pages spread across the file, dropping blanks.
    #    Rendering stays sequential in the outer thread (fitz Documents are not
    #    reliably thread-safe across page operations).
    doc = fitz.open(str(scan_pdf))
    try:
        if len(doc) == 0:
            warn_line(f"Orientation: {scan_pdf.name} has 0 pages — using 0°")
            return _fallback("empty PDF")
        candidates = _pick_candidate_pages(doc)
        renders: list[tuple[int, str]] = []  # (page_idx, b64)
        for idx in candidates:
            b64, pil_img = _render_jpeg_b64(doc[idx])
            if not is_blank_page(pil_img):
                renders.append((idx, b64))
        if not renders:
            warn_line(
                f"Orientation: {scan_pdf.name} all candidate pages "
                f"({candidates}) appear blank — using 0°"
            )
            return _fallback(f"all candidate pages blank: {candidates}")
    finally:
        doc.close()

    # 2. Resolve model. Gemini and Qwen take different SDK paths for image input;
    #    we dispatch on the model name. Qwen uses OpenAI-compat with image_url;
    #    Gemini uses the native google.genai SDK with Part.from_bytes.
    from eXercise.ai_client import parse_model_spec  # noqa: PLC0415

    raw_model_spec = (
        os.environ.get("SCAN_ORIENTATION_MODEL", "").strip()
        or os.environ.get("AI_DEFAULT_MODEL", "").strip()
        or "gemini-3-flash-preview"
    )
    model, thinking_tokens, max_tokens = parse_model_spec(raw_model_spec)
    use_gemini = model.startswith("gemini")

    if use_gemini:
        _call_for_b64 = _make_gemini_caller(model, thinking_tokens, max_tokens)
        if _call_for_b64 is None:
            warn_line(
                f"Orientation: {scan_pdf.name} no GEMINI_API_KEY — using 0°"
            )
            return _fallback("no GEMINI_API_KEY", model=model)
    else:
        _call_for_b64 = _make_openai_compat_caller(model, thinking_tokens, max_tokens)
        if _call_for_b64 is None:
            warn_line(
                f"Orientation: {scan_pdf.name} no API key for {model} — using 0°"
            )
            return _fallback(f"no API key for {model}", model=model)

    # 3. Run the per-page AI calls in parallel (capped at _INNER_POOL_MAX_WORKERS).
    def _query_page(idx: int, b64: str) -> PageVote | BaseException:
        """Return a PageVote on success, the exception on failure."""
        try:
            raw = retry_api_call(
                lambda b=b64: _call_for_b64(b),
                label=f"Orientation: {scan_pdf.name} p{idx}",
            )
            edge, rot = _parse_rotation(raw)
            return PageVote(page_idx=idx, edge=edge, rotation_cw=rot)
        except BaseException as exc:  # noqa: BLE001 — caller dispatches on type
            return exc

    pool_size = min(len(renders), _INNER_POOL_MAX_WORKERS)
    votes: list[PageVote] = []
    last_exc: BaseException | None = None
    with ThreadPoolExecutor(max_workers=pool_size) as executor:
        futures = [executor.submit(_query_page, idx, b64) for idx, b64 in renders]
        for fut in as_completed(futures):
            result = fut.result()
            if isinstance(result, PageVote):
                votes.append(result)
            else:
                last_exc = result

    if not votes:
        reason = f"all page queries failed: {last_exc!r}" if last_exc else "all page queries failed"
        warn_line(f"Orientation: {scan_pdf.name} {reason} — using 0°")
        return _fallback(reason, model=model)

    # 4. Majority-vote (Counter.most_common ties broken by insertion order →
    #    deterministic given sorted-by-page-index votes).
    votes.sort(key=lambda v: v.page_idx)
    counts = Counter(v.rotation_cw for v in votes)
    top, _top_n = counts.most_common(1)[0]
    return OrientationResult(
        rotation_cw=top, source="model", model=model, votes=tuple(votes),
    )


def detect_scan_orientations(
    scan_pdfs: list[Path],
) -> dict[Path, OrientationResult]:
    """Run :func:`detect_scan_orientation` over the input list **sequentially**
    (one file at a time) and emit per-page + per-file decision lines as each
    file completes.

    Sequential per file → terminal output stays contiguous per file. Within
    a file the per-page AI calls run in parallel inside :func:`_detect_one`.

    Returns a dict keyed by the input ``Path`` objects (unmodified — caller
    can look up using the same ``Path`` it passed in), guaranteed to contain
    an entry for every input. Empty input → ``{}``.

    Each file's failure is isolated; a single bad file produces a fallback
    entry but does not affect the others.

    Caller is responsible for de-duplicating and ordering inputs — the
    helper iterates ``scan_pdfs`` in caller-supplied order and uses inputs
    as dict keys verbatim.
    """
    from xscore.shared.terminal_ui import info_line, ok_line, warn_line  # noqa: PLC0415

    if not scan_pdfs:
        return {}

    out: dict[Path, OrientationResult] = {}
    for pdf in scan_pdfs:
        info_line(pdf.name)
        try:
            res = detect_scan_orientation(pdf)
        except BaseException as exc:  # noqa: BLE001 — final safety net
            res = _fallback(f"unexpected: {exc!r}")
        out[pdf] = res

        # Emit per-page lines (votes are already sorted by page_idx in _detect_one).
        for v in res.votes:
            info_line(
                f"  p{v.page_idx:<3d} →  {v.edge:<7s}  (rotate {v.rotation_cw:>3d}°)"
            )

        # Emit decision line.
        if res.source == "fallback":
            warn_line(
                f"{pdf.name}: detection failed ({res.reason or 'unknown'}) "
                "— using 0°"
            )
            continue
        n_votes = len(res.votes)
        top_n = sum(1 for v in res.votes if v.rotation_cw == res.rotation_cw)
        tag = "unanimous" if top_n == n_votes else "majority"
        if res.rotation_cw == 0:
            ok_line(f"{pdf.name}: already upright  ({top_n}/{n_votes} {tag})")
        else:
            ok_line(
                f"{pdf.name}: applying rotation {res.rotation_cw:>3d}° CW  "
                f"({top_n}/{n_votes} {tag})"
            )

    return out

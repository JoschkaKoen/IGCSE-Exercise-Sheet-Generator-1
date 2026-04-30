"""Per-scan-file orientation detection via vision LLM (default gemini-3-flash-preview).

**Two-stage sampling**: ``SCAN_ORIENTATION_INITIAL_PAGES`` well-spread pages
are queried first; ``SCAN_ORIENTATION_ESCALATION_PAGES`` more pages are
queried only when the initial round disagrees or one of the calls fails.
On a uniformly-fed scan the typical cost is just the initial-round calls;
the full majority vote runs only when the model is uncertain.

Pages are rendered at 300 DPI, JPEG quality 95. The configured vision
model is asked which edge of the rendered image holds the page header
(top/right/bottom/left); each answer maps to a clockwise rotation in
``{0, 90, 180, 270}`` that uprights the page. The per-file rotation is the
majority vote across every successful page query (initial + escalated).

Provider dispatch: model names starting with ``gemini`` go through the
native ``google.genai`` SDK with ``Part.from_bytes``; everything else uses
the OpenAI-compat path with ``image_url`` content parts (Qwen and friends).

Concurrency: files are processed sequentially so terminal output stays
contiguous per file; within a file each phase's AI calls run in parallel
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


def _initial_and_escalation_counts() -> tuple[int, int]:
    """Read the configured (initial, escalation) page counts.

    Clamps INITIAL to >= 1 and ESCALATION to >= 0. Read at call time rather
    than import time so a runtime ``os.environ`` override (tests, ad-hoc CLI
    runs) is honoured immediately.
    """
    from xscore.config import (  # noqa: PLC0415
        SCAN_ORIENTATION_INITIAL_PAGES,
        SCAN_ORIENTATION_ESCALATION_PAGES,
    )
    initial = max(1, int(SCAN_ORIENTATION_INITIAL_PAGES))
    escalation = max(0, int(SCAN_ORIENTATION_ESCALATION_PAGES))
    return initial, escalation


def _total_sample_pages() -> int:
    """Total max pages queried per file across both stages."""
    initial, escalation = _initial_and_escalation_counts()
    return initial + escalation

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

    Two-stage sampling: the detector queries 2 well-spread pages first
    (``initial_votes``), and only escalates to the remaining N-2 pages
    (``escalated_votes``) when the initial round didn't agree or had a
    partial API failure. ``votes`` is a convenience property combining the
    two in page-index order — used by the audit JSON writer and by the
    majority-vote tally. Empty on the fallback path.

    ``source`` is ``"model"`` for a confident model answer, ``"fallback"``
    for any failure path (no API key, parse error, all candidate pages
    blank, etc.). ``reason`` is populated only when ``source == "fallback"``.

    ``model`` is the resolved model id from :func:`make_ai_client`, or
    ``None`` when no client was constructed (e.g. missing API key).
    """

    rotation_cw: int
    source: str
    reason: Optional[str] = None
    model: Optional[str] = None
    initial_votes: tuple[PageVote, ...] = field(default_factory=tuple)
    escalated_votes: tuple[PageVote, ...] = field(default_factory=tuple)

    @property
    def votes(self) -> tuple[PageVote, ...]:
        """All votes (initial + escalated), sorted by page index."""
        return tuple(
            sorted(self.initial_votes + self.escalated_votes, key=lambda v: v.page_idx)
        )


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
        max_samples = _total_sample_pages()
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


def _split_candidates(
    candidates: list[int], initial_count: int
) -> tuple[list[int], list[int]]:
    """Split *candidates* into (initial, escalation) lists.

    The *initial* list takes content-rich inner positions (avoiding the
    spread's edges where covers / back-pages tend to confuse the model);
    the rest go to *escalation*. Both returned lists preserve original
    page-index order.

    For a 5-element spread ``[0, 16, 32, 47, 63]`` with ``initial_count=2``
    this returns ``([16, 47], [0, 32, 63])`` — exactly the heuristic the
    single-knob version used.
    """
    n = len(candidates)
    if n == 0 or initial_count <= 0:
        return [], list(candidates)
    if initial_count >= n:
        return list(candidates), []

    # Pick `initial_count` positions within the spread. For initial_count==1,
    # pick the dead-middle position. For >=2, span [1, n-2] (skip both edges)
    # evenly, dropping rounding collisions.
    if initial_count == 1:
        initial_pos = [n // 2]
    elif n >= initial_count + 2:
        # Have room to skip both edges.
        initial_pos = [
            round(1 + i * (n - 3) / (initial_count - 1))
            for i in range(initial_count)
        ]
    else:
        # Tight fit (initial_count == n - 1) — start at index 0.
        initial_pos = list(range(initial_count))
    initial_pos = sorted(set(initial_pos))

    initial_set = set(initial_pos)
    initial = [candidates[i] for i in initial_pos]
    escalation = [candidates[i] for i in range(n) if i not in initial_set]
    return initial, escalation


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

    Samples up to ``SCAN_ORIENTATION_INITIAL_PAGES + _ESCALATION_PAGES`` pages
    spread across the file, queries the configured vision model on each non-blank page in
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

    # 3. Two-stage sampling: query 2 well-spread pages first, escalate to
    #    the remainder only if they disagree or one of them fails.
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

    def _run_pool(
        page_renders: list[tuple[int, str]],
    ) -> tuple[list[PageVote], BaseException | None]:
        """Query *page_renders* in parallel, return (votes, last_exc)."""
        if not page_renders:
            return [], None
        pool_size = min(len(page_renders), _INNER_POOL_MAX_WORKERS)
        out_votes: list[PageVote] = []
        last_exception: BaseException | None = None
        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = [executor.submit(_query_page, idx, b64) for idx, b64 in page_renders]
            for fut in as_completed(futures):
                r = fut.result()
                if isinstance(r, PageVote):
                    out_votes.append(r)
                else:
                    last_exception = r
        out_votes.sort(key=lambda v: v.page_idx)
        return out_votes, last_exception

    # Pick which renders form the initial round. Use _split_candidates so the
    # initial picks come from content-rich inner positions and avoid the
    # spread's edges (empirically risky — sparse covers + back-page text).
    initial_count, _escalation_count = _initial_and_escalation_counts()
    initial_pages, _escalation_pages = _split_candidates(
        [r[0] for r in renders], initial_count
    )
    initial_set = set(initial_pages)
    initial_renders   = [r for r in renders if r[0] in initial_set]
    remaining_renders = [r for r in renders if r[0] not in initial_set]

    initial_votes, last_exc = _run_pool(initial_renders)

    # Decide whether to escalate.
    distinct_rotations = {v.rotation_cw for v in initial_votes}
    initial_complete = len(initial_votes) == len(initial_renders)
    should_escalate = bool(remaining_renders) and (
        len(distinct_rotations) > 1 or not initial_complete
    )

    if should_escalate:
        escalated_votes, esc_exc = _run_pool(remaining_renders)
        last_exc = last_exc or esc_exc
    else:
        escalated_votes = []

    all_votes = initial_votes + escalated_votes
    if not all_votes:
        reason = f"all page queries failed: {last_exc!r}" if last_exc else "all page queries failed"
        warn_line(f"Orientation: {scan_pdf.name} {reason} — using 0°")
        return _fallback(reason, model=model)

    # 4. Majority-vote across every successful vote (initial + escalated).
    counts = Counter(v.rotation_cw for v in all_votes)
    top, _top_n = counts.most_common(1)[0]
    return OrientationResult(
        rotation_cw=top,
        source="model",
        model=model,
        initial_votes=tuple(initial_votes),
        escalated_votes=tuple(escalated_votes),
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

        # Emit per-page lines for the initial round.
        for v in sorted(res.initial_votes, key=lambda v: v.page_idx):
            info_line(
                f"  p{v.page_idx:<3d} →  {v.edge:<7s}  (rotate {v.rotation_cw:>3d}°)"
            )

        # If escalation kicked in, separate it visually then print the rest.
        if res.escalated_votes:
            info_line(
                f"  not unanimous — checking {len(res.escalated_votes)} more pages"
            )
            for v in sorted(res.escalated_votes, key=lambda v: v.page_idx):
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

"""Provider-specific orientation detection paths.

Three execution paths plus the prompts + schemas they share:

- **Tesseract OSD** — :func:`_detect_one_tesseract` and the batched fast path
  :func:`_run_tesseract_batch`. Uses PyTesseract's orientation/script
  detection on a downsampled greyscale render.
- **OpenAI-compat** (Qwen, etc.) — :func:`_make_openai_compat_caller`
  returns a closure that takes a JPEG and returns the canonical edge label.
  Used inside :func:`_detect_one_ai`.
- **Gemini native** — :func:`_make_gemini_caller`, same shape but using the
  ``google.genai`` SDK with native PDF/image parts.

Plus :func:`_render_for_osd` and :func:`_render_jpeg_b64` (PyMuPDF →
PIL/JPEG renderers) and :func:`_parse_rotation` (canonicalise the
edge-string response to a clockwise rotation in degrees).

Extracted from :mod:`xscore.preprocessing.scan_orientation` so that module
stays focused on candidate-page selection + voting + decision rather than
provider plumbing. The detection-path functions are imported back by the
entry-point :func:`detect_scan_orientations`.
"""

from __future__ import annotations

import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

# Runtime imports of names defined in the parent module. ``scan_orientation``
# imports this module at the BOTTOM of its file (after these names are
# defined), so they are available by the time this module's body executes.
from xscore.preprocessing.scan_orientation import (  # noqa: E402
    OrientationResult,
    PageVote,
    SkippedPage,
    _check_tesseract_available,
    _fallback,
    _initial_and_escalation_votes,
    _pick_candidate_pages,
    _split_candidates,
    _spread_candidates,
)

# Module-level constants shared across the orientation pipeline.
# Mirrored from :mod:`xscore.preprocessing.scan_orientation` so detector
# functions can use them as default-argument values at import time.
ROTATION_DETECTION_DPI = 300
_VALID_ROTATIONS = {0, 90, 180, 270}
_INNER_POOL_MAX_WORKERS = 8  # cap on parallel AI calls per file
_TESS_CONF_THRESHOLD = 2.0   # matches existing remove_blanks_autorotate convention
_TESS_DOWNSAMPLE_AT_PIXELS = 4_000_000  # downsample 2× when image > this many pixels


def _total_vote_target() -> int:
    """Total max usable votes targeted per file across both stages."""
    from xscore.config import (
        SCAN_ORIENTATION_INITIAL_VOTES,
        SCAN_ORIENTATION_ESCALATION_VOTES,
    )
    initial = max(1, int(SCAN_ORIENTATION_INITIAL_VOTES))
    escalation = max(0, int(SCAN_ORIENTATION_ESCALATION_VOTES))
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

_JPEG_QUALITY = 95  # never below 90 (preserves text clarity for orientation detection)
TESS_OSD_DPI = 150  # Tesseract path render DPI; OSD doesn't need 300 DPI
_TESS_BATCH_SIZE = 4  # candidates dispatched per parallel Tesseract batch

def _render_for_osd(page: fitz.Page, dpi: int = TESS_OSD_DPI) -> Image.Image:
    """Render *page* at *dpi* as RGB PIL Image (no JPEG round-trip).

    Tesseract OSD is the only consumer on the Tesseract path; it doesn't
    need JPEG bytes, just the pixmap. Skipping the JPEG encode saves ~30 ms
    per page vs :func:`_render_jpeg_b64` and avoids a lossy round-trip.
    """
    pix = page.get_pixmap(
        matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csRGB,
    )
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


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


# ---------------------------------------------------------------------------
# Tesseract OSD shadow comparison
# ---------------------------------------------------------------------------

_tesseract_unavailable_logged: bool = False  # module state — single warn per session



def _tesseract_rotation_cw(pil_img: Image.Image) -> tuple[int | None, float]:
    """Run Tesseract OSD on a downsampled copy of *pil_img*.

    Returns ``(rotation_cw, confidence)``:
      - ``(int, conf)`` on a confident Tesseract verdict
      - ``(None, conf)`` when ``orientation_conf < threshold`` (we keep the
        confidence number for terminal display; the rotation is withheld)
      - ``(None, 0.0)`` when Tesseract is unavailable / errors

    Convention: returns CW degrees so the caller can compare directly with
    the AI path's ``rotation_cw``. **Verified empirically on first run** —
    if the convention turns out to be CCW, flip the marked line below.

    This helper runs inside the AI per-page worker and must NEVER raise —
    a Tesseract failure must not poison the AI vote. A whole-body except
    catches any unforeseen exception path.
    """
    try:
        import pytesseract  # noqa: PLC0415
    except ImportError:
        return None, 0.0
    try:
        # Downsample for OSD speed (300 → 150 DPI ≈ half the wall time).
        w, h = pil_img.size
        osd_img = (
            pil_img.resize((w // 2, h // 2), Image.Resampling.LANCZOS)
            if w * h > _TESS_DOWNSAMPLE_AT_PIXELS else pil_img
        )
        osd = pytesseract.image_to_osd(osd_img, output_type=pytesseract.Output.DICT)
        raw_angle = int(osd.get("rotate", 0)) % 360
        conf = float(osd.get("orientation_conf", 0))
        if conf < _TESS_CONF_THRESHOLD:
            return None, conf
        # CONVENTION FLIP POINT — see helper docstring. Default: assume CW.
        cw = raw_angle
        # If the verification run shows disagreement on top-fed scans, change to:
        #   cw = (360 - raw_angle) % 360
        if cw not in _VALID_ROTATIONS:
            return None, conf
        return cw, conf
    except BaseException:  # noqa: BLE001 — final safety net; helper never raises
        return None, 0.0


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
        make_request_timeout,
        provider_supports_json_schema_with_system,
    )

    # The OpenAI-compat branch is only reached when the resolved model name
    # is non-Gemini, so the default here is just a Qwen fallback for the
    # rare path where both env vars are unset and a non-Gemini default is
    # desired. The primary default (``gemini-3.5-flash``) is set in
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
    _timeout = make_request_timeout("quick")
    _timeout_kw: dict = {"timeout": _timeout} if _timeout is not None else {}
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
                        model=resolved_model, messages=messages, stream=True, **kw, **_timeout_kw,
                    )
                    raw = collect_streamed_response(stream)
                else:
                    extra = {"response_format": fmt_rf} if fmt_rf is not None else {}
                    resp = client.chat.completions.create(
                        model=resolved_model, messages=messages, **extra, **kw, **_timeout_kw,
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



def _detect_one_tesseract(
    scan_pdf: Path,
    *,
    is_blank_page,
) -> OrientationResult:
    """Tesseract-primary detection with usable-vote two-stage sampling +
    a full-document expansion when every sampled page is blank.

    Walks bisection-ordered candidate pages in parallel batches; each batch
    is rendered at 150 DPI (sequential, fitz isn't thread-safe) and OSD'd in
    parallel. Pages that return low confidence or errors are recorded in the
    round's skipped list and we keep walking until we collect
    ``initial_target`` usable votes (or run out of candidates). If the
    initial votes don't unanimously agree (or we couldn't fill the target),
    we walk more candidates for an additional ``escalation_target`` usable
    votes and majority-vote across the full set.

    **Expansion pass**: when the initial + escalation rounds yield zero
    usable votes AND every recorded skip is ``reason=="blank"``, we sweep
    every non-sampled page through the same blank-check + OSD pipeline. If
    that recovers a usable vote we use it; if it finds non-blank but
    unreadable content we fall back without drop; if every page is
    confirmed blank we return a fallback with ``dropped=True`` so the
    orchestrator can exclude the file from the merge.

    Failures (file missing, Tesseract unavailable, empty PDF, no usable
    votes) return a fallback ``OrientationResult``; the caller's
    ``_emit_decision_line`` is responsible for the user-facing warn.
    """
    from collections import Counter  # noqa: PLC0415

    if not scan_pdf.is_file():
        return _fallback(f"file not found: {scan_pdf}", detector="tesseract")

    # Tesseract missing → caller will warn via the decision line.
    if not _check_tesseract_available():
        return _fallback("Tesseract not installed", detector="tesseract")

    initial_target, escalation_target = _initial_and_escalation_votes()

    doc = fitz.open(str(scan_pdf))
    try:
        n = len(doc)
        if n == 0:
            return _fallback("empty PDF", detector="tesseract")

        # Generous candidate sequence in bisection order. We need slack
        # because some pages (sparse, blank, scribbles-only) will fail OSD.
        target_total = initial_target + escalation_target
        candidate_count = max(target_total * 2, target_total + 4)
        candidates = _spread_candidates(n, candidate_count)
        if not candidates:
            return _fallback("no candidate pages", detector="tesseract", total_pages=n)

        cursor = [0]  # mutable holder so nested closure can advance it

        def _walk_until(
            target: int,
        ) -> tuple[list[PageVote], list[SkippedPage]]:
            """Walk candidates in batches, return up to *target* fresh usable
            votes plus every skip encountered along the way. Updates
            ``cursor`` in place."""
            collected: list[PageVote] = []
            skipped: list[SkippedPage] = []
            while cursor[0] < len(candidates) and len(collected) < target:
                batch_end = min(cursor[0] + _TESS_BATCH_SIZE, len(candidates))
                batch = candidates[cursor[0]:batch_end]
                cursor[0] = batch_end
                results = _run_tesseract_batch(doc, batch, is_blank_page=is_blank_page)
                # Process in batch (page) order so the caller's terminal log
                # shows pages in the order they were tried.
                for item in results:
                    if isinstance(item, SkippedPage):
                        skipped.append(item)
                        continue
                    collected.append(item)
                    if len(collected) >= target:
                        break
            return collected, skipped

        initial_votes, initial_skipped = _walk_until(initial_target)

        distinct = {v.rotation_cw for v in initial_votes}
        need_escalation = (
            len(initial_votes) < initial_target  # didn't fill initial target
            or len(distinct) > 1                  # didn't unanimously agree
        )
        escalated_votes: list[PageVote] = []
        escalated_skipped: list[SkippedPage] = []
        if need_escalation and escalation_target > 0:
            escalated_votes, escalated_skipped = _walk_until(escalation_target)

        initial_skipped_t = tuple(sorted(initial_skipped, key=lambda s: s.page_idx))
        escalated_skipped_t = tuple(
            sorted(escalated_skipped, key=lambda s: s.page_idx)
        )
        all_votes = initial_votes + escalated_votes

        # Expansion: every sampled page came back blank → walk the rest of
        # the document to either recover an orientation from a missed
        # content page, or confirm the file is wholly blank and droppable.
        sampled = set(candidates[:cursor[0]])
        sampled_skipped = initial_skipped + escalated_skipped
        all_sampled_blank = (
            not all_votes
            and len(sampled_skipped) == len(sampled)
            and all(s.reason == "blank" for s in sampled_skipped)
        )
        if all_sampled_blank and len(sampled) < n:
            expansion_vote, expansion_skipped_t, content_unreadable = _expand_walk(
                doc, sampled, is_blank_page=is_blank_page,
            )
            if expansion_vote is not None:
                return OrientationResult(
                    rotation_cw=expansion_vote.rotation_cw,
                    source="model",
                    detector="tesseract",
                    initial_votes=(),
                    escalated_votes=(),
                    expansion_votes=(expansion_vote,),
                    initial_skipped=initial_skipped_t,
                    escalated_skipped=escalated_skipped_t,
                    expansion_skipped=expansion_skipped_t,
                    total_pages=n,
                )
            if content_unreadable:
                return _fallback(
                    "no usable Tesseract votes (expansion found unreadable content)",
                    detector="tesseract",
                    initial_skipped=initial_skipped_t,
                    escalated_skipped=escalated_skipped_t,
                    expansion_skipped=expansion_skipped_t,
                    total_pages=n,
                )
            # Every page (sampled + remaining) was blank.
            confirmed = (
                len(initial_skipped_t)
                + len(escalated_skipped_t)
                + len(expansion_skipped_t)
            )
            return _fallback(
                f"full-document-blank: {confirmed}/{n} pages confirmed blank",
                detector="tesseract",
                initial_skipped=initial_skipped_t,
                escalated_skipped=escalated_skipped_t,
                expansion_skipped=expansion_skipped_t,
                dropped=True,
                total_pages=n,
            )

        if not all_votes:
            return _fallback(
                "no usable Tesseract votes",
                detector="tesseract",
                initial_skipped=initial_skipped_t,
                escalated_skipped=escalated_skipped_t,
                total_pages=n,
            )

        counts = Counter(v.rotation_cw for v in all_votes)
        top, _top_n = counts.most_common(1)[0]
        return OrientationResult(
            rotation_cw=top,
            source="model",
            detector="tesseract",
            initial_votes=tuple(sorted(initial_votes, key=lambda v: v.page_idx)),
            escalated_votes=tuple(sorted(escalated_votes, key=lambda v: v.page_idx)),
            initial_skipped=initial_skipped_t,
            escalated_skipped=escalated_skipped_t,
            total_pages=n,
        )
    finally:
        doc.close()


def _expand_walk(
    doc: fitz.Document,
    sampled: set[int],
    *,
    is_blank_page,
) -> tuple["PageVote | None", tuple[SkippedPage, ...], bool]:
    """Sweep every non-sampled page through the blank-check + OSD pipeline.

    Returns ``(vote_or_none, skipped_tuple, content_unreadable)``:

    - ``vote_or_none`` — the first usable :class:`PageVote` recovered, or
      ``None`` if none found.
    - ``skipped_tuple`` — every :class:`SkippedPage` produced during the
      sweep (in page-index order), including the page that triggered an
      early ``content_unreadable`` exit.
    - ``content_unreadable`` — ``True`` iff a non-blank skip
      (``low_conf`` / ``tess_error`` / ``render_error``) was seen, which
      proves the file has content even though we couldn't orient it.

    Early-exits on the first usable vote *or* the first non-blank skip;
    if everything is blank we walk every remaining page so the caller can
    confidently mark the PDF dropped.
    """
    remaining = sorted(set(range(len(doc))) - sampled)
    skipped: list[SkippedPage] = []
    for start in range(0, len(remaining), _TESS_BATCH_SIZE):
        batch = remaining[start:start + _TESS_BATCH_SIZE]
        for item in _run_tesseract_batch(doc, batch, is_blank_page=is_blank_page):
            if isinstance(item, PageVote):
                return item, tuple(sorted(skipped, key=lambda s: s.page_idx)), False
            skipped.append(item)
            if item.reason != "blank":
                return None, tuple(sorted(skipped, key=lambda s: s.page_idx)), True
    return None, tuple(sorted(skipped, key=lambda s: s.page_idx)), False



def _run_tesseract_batch(
    doc: fitz.Document,
    page_indices: list[int],
    *,
    is_blank_page,
) -> list["PageVote | SkippedPage"]:
    """Render+blank-check+OSD a batch of pages in parallel.

    Rendering happens **sequentially in the outer thread** (fitz documents
    aren't reliably thread-safe across page operations). OSD calls run in
    parallel via :class:`ThreadPoolExecutor`.

    Returns one ``PageVote`` per usable page and one ``SkippedPage`` per
    page that couldn't yield a vote (blank, low OSD confidence, Tesseract
    error, render error). The returned list preserves the input
    ``page_indices`` order.
    """
    if not page_indices:
        return []

    # Render sequentially.
    renders: list[tuple[int, "Image.Image | None"]] = []
    for idx in page_indices:
        try:
            pil_img = _render_for_osd(doc[idx])
        except BaseException:  # noqa: BLE001 — can't render → treat as skip
            renders.append((idx, None))
            continue
        renders.append((idx, pil_img))

    # OSD in parallel.
    pool_size = min(len(renders), _INNER_POOL_MAX_WORKERS)
    by_idx: dict[int, "PageVote | SkippedPage"] = {}

    def _osd_one(
        item: tuple[int, "Image.Image | None"],
    ) -> "PageVote | SkippedPage":
        idx, pil_img = item
        if pil_img is None:
            return SkippedPage(page_idx=idx, reason="render_error")
        try:
            if is_blank_page(pil_img):
                return SkippedPage(page_idx=idx, reason="blank")
        except BaseException:  # noqa: BLE001
            return SkippedPage(page_idx=idx, reason="tess_error")
        t0 = time.perf_counter()
        rot, conf = _tesseract_rotation_cw(pil_img)
        elapsed = time.perf_counter() - t0
        if rot is None:
            # _tesseract_rotation_cw returns (None, 0.0) on import/runtime
            # error and (None, conf>0) on sub-threshold confidence.
            reason = "low_conf" if conf > 0 else "tess_error"
            return SkippedPage(
                page_idx=idx, reason=reason, confidence=conf, elapsed_s=elapsed,
            )
        return PageVote(
            page_idx=idx, rotation_cw=rot, confidence=conf, elapsed_s=elapsed,
        )

    with ThreadPoolExecutor(max_workers=pool_size) as executor:
        for result in executor.map(_osd_one, renders):
            by_idx[result.page_idx] = result

    # Re-emit in the original batch (page_indices) order.
    return [by_idx[idx] for idx in page_indices]


# ---------------------------------------------------------------------------
# AI path — kept as fallback (DETECTOR=ai or auto+tesseract-missing)
# ---------------------------------------------------------------------------


def _detect_one_ai(
    scan_pdf: Path,
    *,
    is_blank_page,
) -> OrientationResult:
    """AI vision two-stage detection (legacy primary path).

    Renders candidate pages at 300 DPI as JPEG, sends each to the configured
    vision model with the edge-label prompt, and majority-votes across the
    answers. Two-stage: ``INITIAL_VOTES`` initial pages + ``ESCALATION_VOTES``
    additional pages on disagreement / partial failure.
    """
    from eXercise.api_retry import retry_api_call  # noqa: PLC0415
    from xscore.shared.terminal_ui import warn_line  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415

    if not scan_pdf.is_file():
        warn_line(f"Orientation: {scan_pdf.name} not found — using 0°")
        return _fallback(f"file not found: {scan_pdf}", detector="ai")

    doc = fitz.open(str(scan_pdf))
    try:
        if len(doc) == 0:
            warn_line(f"Orientation: {scan_pdf.name} has 0 pages — using 0°")
            return _fallback("empty PDF", detector="ai")
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
            return _fallback(
                f"all candidate pages blank: {candidates}", detector="ai",
            )
    finally:
        doc.close()

    from eXercise.ai_client import parse_model_spec  # noqa: PLC0415
    raw_model_spec = (
        os.environ.get("SCAN_ORIENTATION_MODEL", "").strip()
        or os.environ.get("AI_DEFAULT_MODEL", "").strip()
        or "gemini-3.5-flash"
    )
    model, thinking_tokens, max_tokens = parse_model_spec(raw_model_spec)
    use_gemini = model.startswith("gemini")

    if use_gemini:
        _call_for_b64 = _make_gemini_caller(model, thinking_tokens, max_tokens)
        if _call_for_b64 is None:
            warn_line(f"Orientation: {scan_pdf.name} no GEMINI_API_KEY — using 0°")
            return _fallback("no GEMINI_API_KEY", model=model, detector="ai")
    else:
        _call_for_b64 = _make_openai_compat_caller(model, thinking_tokens, max_tokens)
        if _call_for_b64 is None:
            warn_line(
                f"Orientation: {scan_pdf.name} no API key for {model} — using 0°"
            )
            return _fallback(
                f"no API key for {model}", model=model, detector="ai",
            )

    def _query_page(idx: int, b64: str) -> "PageVote | BaseException":
        t0 = time.perf_counter()
        try:
            raw = retry_api_call(
                lambda b=b64: _call_for_b64(b),
                label=f"Orientation: {scan_pdf.name} p{idx}",
            )
            edge, rot = _parse_rotation(raw)
            return PageVote(
                page_idx=idx, rotation_cw=rot, confidence=0.0, edge=edge,
                elapsed_s=time.perf_counter() - t0,
            )
        except BaseException as exc:  # noqa: BLE001
            return exc

    def _run_pool(
        page_renders: list[tuple[int, str]],
    ) -> tuple[list[PageVote], BaseException | None]:
        if not page_renders:
            return [], None
        pool_size = min(len(page_renders), _INNER_POOL_MAX_WORKERS)
        votes: list[PageVote] = []
        last_exc: BaseException | None = None
        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = [
                executor.submit(_query_page, idx, b64) for idx, b64 in page_renders
            ]
            for fut in as_completed(futures):
                r = fut.result()
                if isinstance(r, PageVote):
                    votes.append(r)
                else:
                    last_exc = r
        votes.sort(key=lambda v: v.page_idx)
        return votes, last_exc

    initial_count, _ = _initial_and_escalation_votes()
    initial_pages, _ = _split_candidates(
        [r[0] for r in renders], initial_count,
    )
    initial_set = set(initial_pages)
    initial_renders = [r for r in renders if r[0] in initial_set]
    remaining_renders = [r for r in renders if r[0] not in initial_set]

    initial_votes, last_exc = _run_pool(initial_renders)
    distinct = {v.rotation_cw for v in initial_votes}
    initial_complete = len(initial_votes) == len(initial_renders)
    should_escalate = bool(remaining_renders) and (
        len(distinct) > 1 or not initial_complete
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
        return _fallback(reason, model=model, detector="ai")

    counts = Counter(v.rotation_cw for v in all_votes)
    top, _ = counts.most_common(1)[0]
    return OrientationResult(
        rotation_cw=top,
        source="model",
        model=model,
        detector="ai",
        initial_votes=tuple(initial_votes),
        escalated_votes=tuple(escalated_votes),
    )



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
before duplex merge / single-PDF write, so the downstream blank-detect /
autorotate / deskew steps see correctly-oriented
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

# Tesseract OSD shadow-comparison constants
_TESS_CONF_THRESHOLD = 2.0   # matches existing remove_blanks_autorotate convention
_TESS_DOWNSAMPLE_AT_PIXELS = 4_000_000  # downsample 2× when image > this many pixels


def _initial_and_escalation_votes() -> tuple[int, int]:
    """Read the configured (initial, escalation) usable-vote targets.

    Clamps INITIAL_VOTES to >= 1 and ESCALATION_VOTES to >= 0. Read at call
    time rather than import time so a runtime ``os.environ`` override is
    honoured immediately.
    """
    from xscore.config import (  # noqa: PLC0415
        SCAN_ORIENTATION_INITIAL_VOTES,
        SCAN_ORIENTATION_ESCALATION_VOTES,
    )
    initial = max(1, int(SCAN_ORIENTATION_INITIAL_VOTES))
    escalation = max(0, int(SCAN_ORIENTATION_ESCALATION_VOTES))
    return initial, escalation


def _total_vote_target() -> int:
    """Total max usable votes targeted per file across both stages."""
    initial, escalation = _initial_and_escalation_votes()
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
    """One page's orientation verdict from the active detector.

    ``rotation_cw`` is the CW rotation in degrees needed to upright the page
    (0 / 90 / 180 / 270). ``confidence`` is the Tesseract OSD confidence
    score on the Tesseract path (0.0 on the AI path — AI doesn't expose
    a numeric confidence). ``edge`` is populated only on the AI path with
    the model's edge label ("top" / "right" / "bottom" / "left"); empty
    string on the Tesseract path.
    """

    page_idx: int
    rotation_cw: int
    confidence: float = 0.0
    edge: str = ""


@dataclass(frozen=True)
class OrientationResult:
    """Result of one orientation-detection call.

    Two-stage sampling: the detector queries an initial batch first
    (``initial_votes``); if those don't agree (or the round under-fills
    the target) it escalates and queries more (``escalated_votes``). The
    final ``rotation_cw`` is the majority vote across every successful
    vote.

    On the Tesseract path we additionally record ``pages_skipped`` —
    candidate pages that returned low confidence, errored, or were blank.

    ``source`` is ``"model"`` for a confident detector answer,
    ``"fallback"`` for any failure path (Tesseract unavailable, API key
    missing, all candidate pages blank, no usable votes, etc.).
    ``detector`` records which path produced the result.
    """

    rotation_cw: int
    source: str
    reason: Optional[str] = None
    model: Optional[str] = None
    detector: str = "tesseract"  # "tesseract" | "ai" | "fallback"
    initial_votes: tuple[PageVote, ...] = field(default_factory=tuple)
    escalated_votes: tuple[PageVote, ...] = field(default_factory=tuple)
    pages_skipped: tuple[int, ...] = field(default_factory=tuple)

    @property
    def votes(self) -> tuple[PageVote, ...]:
        """All votes (initial + escalated), sorted by page index."""
        return tuple(
            sorted(self.initial_votes + self.escalated_votes, key=lambda v: v.page_idx)
        )


def _fallback(
    reason: str,
    *,
    model: Optional[str] = None,
    detector: str = "fallback",
) -> OrientationResult:
    return OrientationResult(
        rotation_cw=0, source="fallback", reason=reason,
        model=model, detector=detector,
    )


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
        max_samples = _total_vote_target()
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
TESS_OSD_DPI = 150  # Tesseract path render DPI; OSD doesn't need 300 DPI
_TESS_BATCH_SIZE = 4  # candidates dispatched per parallel Tesseract batch


def _spread_candidates(n_pages: int, count: int) -> list[int]:
    """Return up to *count* page indices in **bisection order** (mid first,
    then quarters, then eighths, etc.).

    Avoids the absolute edges (p0 and p_{n-1}) which are typically sparse
    covers / back-page text where Tesseract OSD struggles. For very small
    docs (n <= 2) returns ``[0..n-1]`` since there's nothing to bisect.

    The returned ordering guarantees that any prefix of the list is well-
    spread across the document — the first 3 indices are spread across
    rough quarters, the next few fill in the gaps, etc.
    """
    if n_pages <= 0 or count <= 0:
        return []
    if n_pages <= 2:
        return list(range(n_pages))[:count]
    lo, hi = 1, n_pages - 2
    if lo > hi:
        return [n_pages // 2][:count]
    seen: set[int] = set()
    out: list[int] = []
    from collections import deque  # noqa: PLC0415
    queue = deque([(lo, hi)])
    while queue and len(out) < count:
        a, b = queue.popleft()
        m = (a + b) // 2
        if m not in seen:
            seen.add(m)
            out.append(m)
        if a <= m - 1:
            queue.append((a, m - 1))
        if m + 1 <= b:
            queue.append((m + 1, b))
    return out[:count]


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


def _check_tesseract_available() -> bool:
    """Probe pytesseract + the tesseract binary. Returns True on success.

    Silent — callers control whether/how to log. The :func:`_resolve_detector`
    helper warns appropriately based on the SCAN_ORIENTATION_DETECTOR mode.
    """
    try:
        import pytesseract  # noqa: PLC0415
        # pytesseract import succeeds even when the tesseract executable is
        # missing — only image_to_osd raises. Probe the binary too.
        _ = pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 — broad catch is intentional
        return False
    return True


def _resolve_detector() -> str:
    """Resolve SCAN_ORIENTATION_DETECTOR + Tesseract availability into the
    concrete detector to use: ``"tesseract"`` or ``"ai"``.

    - ``DETECTOR=tesseract``: returns ``"tesseract"`` regardless of
      availability. The actual Tesseract calls fail loudly if the binary
      is missing; the user gets a clear hint to flip to ``ai`` or ``auto``.
    - ``DETECTOR=ai``: returns ``"ai"`` always.
    - ``DETECTOR=auto``: returns ``"tesseract"`` if available, else
      ``"ai"`` silently.
    """
    from xscore.config import SCAN_ORIENTATION_DETECTOR  # noqa: PLC0415
    if SCAN_ORIENTATION_DETECTOR == "ai":
        return "ai"
    if SCAN_ORIENTATION_DETECTOR == "tesseract":
        return "tesseract"
    # auto
    return "tesseract" if _check_tesseract_available() else "ai"


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


def detect_scan_orientation(scan_pdf: Path) -> OrientationResult:
    """Detect orientation for *scan_pdf*.

    Routes through :func:`_detect_one` to the configured detector
    (Tesseract by default, AI fallback). Always returns a result — never
    raises. On any failure path returns
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


def _detect_one(
    scan_pdf: Path,
    *,
    is_blank_page,
) -> OrientationResult:
    """Dispatcher: route to the configured detector path."""
    detector = _resolve_detector()
    if detector == "tesseract":
        return _detect_one_tesseract(scan_pdf, is_blank_page=is_blank_page)
    return _detect_one_ai(scan_pdf, is_blank_page=is_blank_page)


# ---------------------------------------------------------------------------
# Tesseract path — primary
# ---------------------------------------------------------------------------

def _detect_one_tesseract(
    scan_pdf: Path,
    *,
    is_blank_page,
) -> OrientationResult:
    """Tesseract-primary detection with usable-vote two-stage sampling.

    Walks bisection-ordered candidate pages in parallel batches; each batch
    is rendered at 150 DPI (sequential, fitz isn't thread-safe) and OSD'd in
    parallel. Pages that return low confidence or errors are recorded in
    ``pages_skipped`` and we keep walking until we collect ``initial_target``
    usable votes (or run out of candidates). If the initial votes don't
    unanimously agree (or we couldn't fill the target), we walk more
    candidates for an additional ``escalation_target`` usable votes and
    majority-vote across the full set.
    """
    from xscore.shared.terminal_ui import warn_line  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415

    if not scan_pdf.is_file():
        warn_line(f"Orientation: {scan_pdf.name} not found — using 0°")
        return _fallback(f"file not found: {scan_pdf}", detector="tesseract")

    # Loud-fail when explicit tesseract mode is set but binary is missing.
    if not _check_tesseract_available():
        from xscore.config import SCAN_ORIENTATION_DETECTOR  # noqa: PLC0415
        if SCAN_ORIENTATION_DETECTOR == "tesseract":
            warn_line(
                f"Orientation: {scan_pdf.name} Tesseract not installed — using "
                "0°. Set SCAN_ORIENTATION_DETECTOR=auto or =ai to use AI vision."
            )
        return _fallback("Tesseract not installed", detector="tesseract")

    initial_target, escalation_target = _initial_and_escalation_votes()

    doc = fitz.open(str(scan_pdf))
    try:
        n = len(doc)
        if n == 0:
            warn_line(f"Orientation: {scan_pdf.name} has 0 pages — using 0°")
            return _fallback("empty PDF", detector="tesseract")

        # Generous candidate sequence in bisection order. We need slack
        # because some pages (sparse, blank, scribbles-only) will fail OSD.
        target_total = initial_target + escalation_target
        candidate_count = max(target_total * 2, target_total + 4)
        candidates = _spread_candidates(n, candidate_count)
        if not candidates:
            return _fallback("no candidate pages", detector="tesseract")

        cursor = [0]  # mutable holder so nested closure can advance it
        skipped: list[int] = []

        def _walk_until(target: int) -> list[PageVote]:
            """Walk candidates in batches, return up to *target* fresh
            usable votes. Updates `cursor` and `skipped` in place."""
            collected: list[PageVote] = []
            while cursor[0] < len(candidates) and len(collected) < target:
                batch_end = min(cursor[0] + _TESS_BATCH_SIZE, len(candidates))
                batch = candidates[cursor[0]:batch_end]
                cursor[0] = batch_end
                results = _run_tesseract_batch(doc, batch, is_blank_page=is_blank_page)
                # Process in batch order so the caller's terminal log shows
                # pages in the order they were tried, not in completion order.
                for vote, status_idx in results:
                    if vote is None:
                        skipped.append(status_idx)
                        continue
                    collected.append(vote)
                    if len(collected) >= target:
                        break
            return collected

        initial_votes = _walk_until(initial_target)

        distinct = {v.rotation_cw for v in initial_votes}
        need_escalation = (
            len(initial_votes) < initial_target  # didn't fill initial target
            or len(distinct) > 1                  # didn't unanimously agree
        )
        escalated_votes: list[PageVote] = []
        if need_escalation and escalation_target > 0:
            escalated_votes = _walk_until(escalation_target)

        all_votes = initial_votes + escalated_votes
        if not all_votes:
            warn_line(
                f"Orientation: {scan_pdf.name} no usable Tesseract votes — using 0°"
            )
            return _fallback(
                "no usable Tesseract votes",
                detector="tesseract",
            )

        counts = Counter(v.rotation_cw for v in all_votes)
        top, _top_n = counts.most_common(1)[0]
        return OrientationResult(
            rotation_cw=top,
            source="model",
            detector="tesseract",
            initial_votes=tuple(sorted(initial_votes, key=lambda v: v.page_idx)),
            escalated_votes=tuple(sorted(escalated_votes, key=lambda v: v.page_idx)),
            pages_skipped=tuple(sorted(set(skipped))),
        )
    finally:
        doc.close()


def _run_tesseract_batch(
    doc: fitz.Document,
    page_indices: list[int],
    *,
    is_blank_page,
) -> list[tuple["PageVote | None", int]]:
    """Render+blank-check+OSD a batch of pages in parallel.

    Rendering happens **sequentially in the outer thread** (fitz documents
    aren't reliably thread-safe across page operations). OSD calls run in
    parallel via :class:`ThreadPoolExecutor`.

    Returns a list of ``(PageVote_or_None, page_idx)`` pairs in original
    batch order. ``None`` means the page was blank or Tesseract returned
    a low-confidence / errored answer. ``page_idx`` is always populated for
    skip-tracking.
    """
    if not page_indices:
        return []

    # Render sequentially.
    renders: list[tuple[int, Image.Image]] = []
    for idx in page_indices:
        try:
            pil_img = _render_for_osd(doc[idx])
        except BaseException:  # noqa: BLE001 — can't render → treat as skip
            renders.append((idx, None))  # type: ignore[arg-type]
            continue
        renders.append((idx, pil_img))

    # OSD in parallel.
    pool_size = min(len(renders), _INNER_POOL_MAX_WORKERS)
    by_idx: dict[int, "PageVote | None"] = {}

    def _osd_one(item: tuple[int, "Image.Image | None"]) -> tuple[int, "PageVote | None"]:
        idx, pil_img = item
        if pil_img is None:
            return idx, None
        try:
            if is_blank_page(pil_img):
                return idx, None
        except BaseException:  # noqa: BLE001
            return idx, None
        rot, conf = _tesseract_rotation_cw(pil_img)
        if rot is None:
            return idx, None
        return idx, PageVote(page_idx=idx, rotation_cw=rot, confidence=conf)

    with ThreadPoolExecutor(max_workers=pool_size) as executor:
        for idx, vote in executor.map(_osd_one, renders):
            by_idx[idx] = vote

    # Re-emit in the original batch (page_indices) order.
    return [(by_idx.get(idx), idx) for idx in page_indices]


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
        or "gemini-3-flash-preview"
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
        try:
            raw = retry_api_call(
                lambda b=b64: _call_for_b64(b),
                label=f"Orientation: {scan_pdf.name} p{idx}",
            )
            edge, rot = _parse_rotation(raw)
            return PageVote(
                page_idx=idx, rotation_cw=rot, confidence=0.0, edge=edge,
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
    from xscore.config import SCAN_ORIENTATION_DETECTOR  # noqa: PLC0415
    from xscore.shared.terminal_ui import (  # noqa: PLC0415
        blank_line,
        file_header_line,
        info_line,
        ok_line,
        warn_line,
    )

    if not scan_pdfs:
        return {}

    # Resolve detector ONCE up-front so the auto-fallback warning (if any)
    # appears at the top of prepare_scans rather than buried mid-stream.
    declared = SCAN_ORIENTATION_DETECTOR
    detector = _resolve_detector()
    if declared == "auto" and detector == "ai":
        warn_line("auto: Tesseract unavailable, falling back to AI vision")
    elif declared == "tesseract" and not _check_tesseract_available():
        warn_line(
            "SCAN_ORIENTATION_DETECTOR=tesseract but Tesseract is not installed "
            "— orientation detection will fall back to 0°. Set "
            "SCAN_ORIENTATION_DETECTOR=auto or =ai to use AI vision instead."
        )

    out: dict[Path, OrientationResult] = {}
    for i, pdf in enumerate(scan_pdfs):
        if i:
            blank_line()
        file_header_line(pdf.name)
        try:
            res = detect_scan_orientation(pdf)
        except BaseException as exc:  # noqa: BLE001 — final safety net
            res = _fallback(f"unexpected: {exc!r}")
        out[pdf] = res

        # Initial round of per-page lines.
        for v in sorted(res.initial_votes, key=lambda v: v.page_idx):
            info_line(_fmt_page_line(v, res.detector))

        # Escalation, if any.
        if res.escalated_votes:
            from collections import Counter  # noqa: PLC0415
            tally = Counter(v.rotation_cw for v in res.initial_votes)
            breakdown = " / ".join(
                f"{n}× {rot}°"
                for rot, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            info_line(
                f"  split {breakdown} — escalating with {len(res.escalated_votes)} more votes"
            )
            for v in sorted(res.escalated_votes, key=lambda v: v.page_idx):
                info_line(_fmt_page_line(v, res.detector))

        # Skipped pages (Tesseract path only — AI path doesn't skip, it errors).
        if res.pages_skipped:
            info_line(
                f"  ({len(res.pages_skipped)} page(s) skipped: "
                f"{list(res.pages_skipped)})"
            )

        _emit_decision_line(pdf, res, info_line, ok_line, warn_line)

    return out


def _fmt_page_line(v: PageVote, detector: str) -> str:
    """Format one per-page line, choosing the layout based on detector path."""
    if detector == "tesseract":
        return (
            f"  p{v.page_idx:<3d} → {v.rotation_cw:>3d}° CW   "
            f"(conf {v.confidence:>4.1f})"
        )
    # AI path
    return (
        f"  p{v.page_idx:<3d} →  {v.edge:<7s}  (rotate {v.rotation_cw:>3d}°)"
    )


def _emit_decision_line(
    pdf: Path,
    res: OrientationResult,
    info_line,
    ok_line,
    warn_line,
) -> None:
    """Emit the per-file decision line."""
    if res.source == "fallback":
        warn_line(
            f"{pdf.name}: detection failed ({res.reason or 'unknown'}) — using 0°"
        )
        return

    from collections import Counter  # noqa: PLC0415
    counts = Counter(v.rotation_cw for v in res.votes)
    n_votes = sum(counts.values())
    top_n = counts[res.rotation_cw]
    if top_n == n_votes:
        summary = f"unanimous, {top_n}/{n_votes}"
    else:
        breakdown = " vs ".join(
            f"{n}× {rot}°"
            for rot, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        summary = f"majority {top_n}/{n_votes} — {breakdown}"
    skipped_note = (
        f", {len(res.pages_skipped)} skipped" if res.pages_skipped else ""
    )
    if res.rotation_cw == 0:
        ok_line(f"{pdf.name}: already upright ({summary}{skipped_note})")
    else:
        ok_line(
            f"{pdf.name}: applying rotation {res.rotation_cw}° CW "
            f"({summary}{skipped_note})"
        )

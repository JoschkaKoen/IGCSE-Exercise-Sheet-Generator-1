"""Per-scan-file orientation detection via vision LLM (default gemini-3.5-flash).

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
import time
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



@dataclass(frozen=True)
class PageVote:
    """One page's orientation verdict from the active detector.

    ``rotation_cw`` is the CW rotation in degrees needed to upright the page
    (0 / 90 / 180 / 270). ``confidence`` is the Tesseract OSD confidence
    score on the Tesseract path (0.0 on the AI path — AI doesn't expose
    a numeric confidence). ``edge`` is populated only on the AI path with
    the model's edge label ("top" / "right" / "bottom" / "left"); empty
    string on the Tesseract path. ``elapsed_s`` is the wall time of the
    detector call (``retry_api_call`` for AI incl. backoff,
    ``_tesseract_rotation_cw`` for Tesseract); excludes rendering and the
    blank-page check, which run before the worker.
    """

    page_idx: int
    rotation_cw: int
    confidence: float = 0.0
    edge: str = ""
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class SkippedPage:
    """One candidate page that couldn't contribute a usable vote.

    ``reason`` is a short token: ``"blank"`` (blank-page check rejected it),
    ``"low_conf"`` (Tesseract OSD ran but confidence was below threshold),
    ``"tess_error"`` (OSD or the blank check raised / Tesseract missing), or
    ``"render_error"`` (PyMuPDF couldn't render the page). ``confidence`` is
    populated only for ``"low_conf"``. ``elapsed_s`` is the OSD call wall
    time when OSD ran (``low_conf``, ``tess_error``); 0 for paths that short-
    circuited before OSD (``blank``, ``render_error``).
    """

    page_idx: int
    reason: str
    confidence: float = 0.0
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class OrientationResult:
    """Result of one orientation-detection call.

    Two-stage sampling: the detector queries an initial batch first
    (``initial_votes``); if those don't agree (or the round under-fills
    the target) it escalates and queries more (``escalated_votes``). The
    final ``rotation_cw`` is the majority vote across every successful
    vote.

    A third **expansion** round runs (Tesseract path only) when the
    initial + escalation candidates were all blank: every non-sampled
    page is checked too, so we can either (a) recover a usable vote from
    a content page the bisection missed or (b) confirm the file is
    entirely blank and set ``dropped=True``.

    On the Tesseract path we additionally record skipped candidates per
    round (``initial_skipped``, ``escalated_skipped``, ``expansion_skipped``)
    — pages that returned low confidence, errored, or were blank. The
    legacy ``pages_skipped`` accessor returns the combined tuple in
    page-index order.

    ``source`` is ``"model"`` for a confident detector answer,
    ``"fallback"`` for any failure path (Tesseract unavailable, API key
    missing, all candidate pages blank, no usable votes, etc.).
    ``detector`` records which path produced the result. ``dropped=True``
    signals downstream that the PDF should be excluded from subsequent
    pipeline steps because every page was confirmed blank.
    """

    rotation_cw: int
    source: str
    reason: Optional[str] = None
    model: Optional[str] = None
    detector: str = "tesseract"  # "tesseract" | "ai" | "fallback"
    initial_votes: tuple[PageVote, ...] = field(default_factory=tuple)
    escalated_votes: tuple[PageVote, ...] = field(default_factory=tuple)
    expansion_votes: tuple[PageVote, ...] = field(default_factory=tuple)
    initial_skipped: tuple[SkippedPage, ...] = field(default_factory=tuple)
    escalated_skipped: tuple[SkippedPage, ...] = field(default_factory=tuple)
    expansion_skipped: tuple[SkippedPage, ...] = field(default_factory=tuple)
    dropped: bool = False
    total_pages: int = 0  # set when the detector opened the PDF; 0 otherwise

    @property
    def votes(self) -> tuple[PageVote, ...]:
        """All votes (initial + escalated + expansion), sorted by page index."""
        return tuple(
            sorted(
                self.initial_votes + self.escalated_votes + self.expansion_votes,
                key=lambda v: v.page_idx,
            )
        )

    @property
    def pages_skipped(self) -> tuple[SkippedPage, ...]:
        """All skipped pages (every round), sorted by page index."""
        return tuple(
            sorted(
                self.initial_skipped + self.escalated_skipped + self.expansion_skipped,
                key=lambda s: s.page_idx,
            )
        )


def _fallback(
    reason: str,
    *,
    model: Optional[str] = None,
    detector: str = "fallback",
    initial_skipped: tuple[SkippedPage, ...] = (),
    escalated_skipped: tuple[SkippedPage, ...] = (),
    expansion_skipped: tuple[SkippedPage, ...] = (),
    dropped: bool = False,
    total_pages: int = 0,
) -> OrientationResult:
    return OrientationResult(
        rotation_cw=0, source="fallback", reason=reason,
        model=model, detector=detector,
        initial_skipped=initial_skipped,
        escalated_skipped=escalated_skipped,
        expansion_skipped=expansion_skipped,
        dropped=dropped,
        total_pages=total_pages,
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

        _emit_round(res.initial_votes, res.initial_skipped, res.detector, info_line)

        # Escalation, if any.
        if res.escalated_votes or res.escalated_skipped:
            info_line(_escalation_banner(res))
            _emit_round(
                res.escalated_votes, res.escalated_skipped, res.detector, info_line,
            )

        # Expansion, if any (Tesseract path: every sampled page was blank →
        # we swept the remaining pages too).
        if res.expansion_votes or res.expansion_skipped:
            info_line(
                "  all sampled candidates blank — checking remaining pages with Tesseract"
            )
            _emit_round(
                res.expansion_votes, res.expansion_skipped, res.detector, info_line,
            )

        _emit_decision_line(pdf, res, info_line, ok_line, warn_line)

    return out


def _fmt_page_line(v: PageVote, detector: str) -> str:
    """Format one per-page line, choosing the layout based on detector path."""
    from xscore.shared.terminal_ui import format_duration  # noqa: PLC0415
    dur = format_duration(v.elapsed_s)
    if detector == "tesseract":
        return (
            f"  p{v.page_idx:<3d} → {v.rotation_cw:>3d}° CW   "
            f"(conf {v.confidence:>4.1f})  ·  {dur}"
        )
    # AI path
    return (
        f"  p{v.page_idx:<3d} →  {v.edge:<7s}  (rotate {v.rotation_cw:>3d}°)"
        f"  ·  {dur}"
    )


_SKIP_REASON_DETAIL: dict[str, str] = {
    "blank": "(blank)",
    "tess_error": "(tesseract error)",
    "render_error": "(render error)",
}


def _fmt_skipped_line(s: SkippedPage) -> str:
    """Format one per-page line for a candidate that didn't yield a usable vote.

    Shares the ``p{idx} → {verdict} (detail) · {dur}`` shape with
    :func:`_fmt_page_line`; ``verdict`` is the literal word ``skipped`` and
    the parenthesised detail names the reason (with the rejected OSD
    confidence for ``low_conf``). Duration is included only for reasons
    that actually ran OSD.
    """
    from xscore.shared.terminal_ui import format_duration  # noqa: PLC0415
    if s.reason == "low_conf":
        detail = f"(low conf {s.confidence:>4.1f})"
    else:
        detail = _SKIP_REASON_DETAIL.get(s.reason, f"({s.reason})")
    line = f"  p{s.page_idx:<3d} → skipped   {detail}"
    if s.elapsed_s > 0:
        line += f"  ·  {format_duration(s.elapsed_s)}"
    return line


def _emit_round(
    votes: tuple[PageVote, ...],
    skipped: tuple[SkippedPage, ...],
    detector: str,
    info_line,
) -> None:
    """Print one round's per-page lines, usable and skipped interleaved by page index."""
    items: list[tuple[int, str]] = []
    for v in votes:
        items.append((v.page_idx, _fmt_page_line(v, detector)))
    for s in skipped:
        items.append((s.page_idx, _fmt_skipped_line(s)))
    for _idx, line in sorted(items, key=lambda kv: kv[0]):
        info_line(line)


def _escalation_banner(res: OrientationResult) -> str:
    """One-line banner explaining why the escalation round ran."""
    from collections import Counter  # noqa: PLC0415
    n_more = len(res.escalated_votes) + len(res.escalated_skipped)
    if res.initial_votes:
        tally = Counter(v.rotation_cw for v in res.initial_votes)
        if len(tally) > 1:
            breakdown = " / ".join(
                f"{n}× {rot}°"
                for rot, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            return f"  split {breakdown} — escalating with {n_more} more candidates"
    return f"  under-filled — escalating with {n_more} more candidates"


def _emit_decision_line(
    pdf: Path,
    res: OrientationResult,
    info_line,
    ok_line,
    warn_line,
) -> None:
    """Emit the per-file decision line."""
    if res.dropped:
        confirmed = len(res.pages_skipped)
        total = res.total_pages or confirmed
        warn_line(
            f"{pdf.name}: every page blank ({confirmed}/{total}) — dropping from pipeline"
        )
        return
    if res.source == "fallback":
        warn_line(
            f"{pdf.name}: detection failed ({res.reason or 'unknown'}) — using 0°"
        )
        return

    from collections import Counter  # noqa: PLC0415
    counts = Counter(v.rotation_cw for v in res.votes)
    n_votes = sum(counts.values())
    top_n = counts[res.rotation_cw]
    if res.expansion_votes:
        summary = f"recovered {top_n}/{n_votes} from expansion sweep"
    elif top_n == n_votes:
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

# ---------------------------------------------------------------------------
# Backwards-compat re-exports — provider-specific detection paths now live
# in :mod:`scan_orientation_detectors`. External call sites
# (``coordinator.py``) historically import a few symbols from this module
# directly; the re-exports below keep that working.
# ---------------------------------------------------------------------------

from xscore.preprocessing.scan_orientation_detectors import (  # noqa: E402, F401
    TESS_OSD_DPI,
    _EDGE_TO_CW,
    _JPEG_QUALITY,
    _ROTATION_SCHEMA,
    _SYSTEM,
    _USER,
    _detect_one_ai,
    _detect_one_tesseract,
    _make_gemini_caller,
    _make_openai_compat_caller,
    _parse_rotation,
    _render_for_osd,
    _render_jpeg_b64,
    _run_tesseract_batch,
    _tesseract_rotation_cw,
)

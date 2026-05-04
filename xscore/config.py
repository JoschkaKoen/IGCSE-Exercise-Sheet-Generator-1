#!/usr/bin/env python3
"""
config.py
---------
Configuration for xScore. Edit values here or set the noted environment
variables. See README.md for full detail.

AI provider usage by step (see ``xscore.shared.pipeline_steps.STEPS`` for the live ordering):
  parse_grading_instructions     : KIMI_API_KEY  — natural-language prompt parsing
  cover_page_empty_exam          : Gemini        — empty-exam cover-page check
  cover_page_scan_first          : Gemini        — scan cover-page detection (page 1 only)
  student_names                  : configurable  — student-name OCR (NAME_DETECTION_MODEL)
  detect_exam_layout / extract questions / mark scheme :
                                   configurable — exam/mark-scheme parsing
                                   (DETECT_LAYOUT_MODEL, EXTRACT_EXAM_QUESTION_NUMBERS_MODEL,
                                    EXTRACT_EXAM_QUESTIONS_MODEL, DETECT_SCHEME_GRAPHICS_MODEL,
                                    ASSIGN_SCHEME_QUESTIONS_MODEL, READ_MARK_SCHEME_MODEL)
  ai_marking                     : configurable  — AI marking (MARKING_MODEL)

How to run (from repo root, with venv activated and dependencies installed):

  Setup once:
    python3 -m venv .venv && source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
    pip install -r requirements.txt
    # Create .env with GOOGLE_API_KEY or GEMINI_API_KEY (Gemini) and/or KIMI_API_KEY (Kimi).

  Activate the virtual environment:
    source .venv/bin/activate

  Grade an exam folder from a natural-language prompt (uses Kimi only; KIMI_API_KEY):
    python3 xscore.py "check all multiple choice question answers"
    python3 xscore.py "..." --folder "path/to/exam_folder"
    # Optional CLI (also inferable from prompt JSON): --dpi  --folder
    #   --force-clean-scan  --no-report

Tunables below apply to extraction/, the other top-level packages, and xscore.py
(PIPELINE_*, NAME_*, etc.).
"""

import os
from typing import Any

# =============================================================================
# AI Model Configuration
# =============================================================================

# Select which AI model to use for OCR/extraction.
# Options (exact model names - edit this line to change model):
#   - "gemini-3.1-pro-preview"  : Google Gemini 3.1 Pro (highest accuracy)
#   - "gemini-3.0-flash"        : Google Gemini 3.0 Flash (faster, lower accuracy)
#   - "kimi-k2.5"               : Moonshot Kimi K2.5 (OpenAI-compatible API)
#   - "kimi-k2"                 : Moonshot Kimi K2 (alternative name)
#
# To change the model, either:
#   1. Edit the line below, OR
#   2. Set AI_MODEL environment variable (takes precedence)
# Scope: extraction/benchmarking (extraction/providers/kimi.py, Kimi path) — not xscore.py marking.
AI_MODEL = os.getenv("AI_MODEL", "kimi-k2.5")

# Exam layout + prompt + schema (see extraction/profiles/)
# NOTE: used ONLY by xscore/extraction/ benchmarking scripts, NOT by the main grading pipeline.
EXAM_PROFILE = "igcse_physics"

# =============================================================================
# API Configuration
# =============================================================================

# API keys are read from environment variables:
#   - GOOGLE_API_KEY or GEMINI_API_KEY : For Gemini models
#   - KIMI_API_KEY                      : For Kimi models (via Moonshot API)

# Delay between API calls (seconds). Set to 0 for no delay.
API_CALL_DELAY_S = 0

# Maximum retries for failed API calls (0 = no retries, try once).
try:
    MAX_RETRIES: int = int(os.getenv("ALL_MAX_RETRIES", "0"))
except ValueError as _e:
    raise RuntimeError(f"ALL_MAX_RETRIES must be an integer; got: {os.getenv('ALL_MAX_RETRIES')!r}") from _e

# Initial backoff time for retries (seconds). Doubles after each failure.
try:
    RETRY_BACKOFF_S: float = float(os.getenv("ALL_RETRY_BACKOFF_S", "1"))
except ValueError as _e:
    raise RuntimeError(f"ALL_RETRY_BACKOFF_S must be a number; got: {os.getenv('ALL_RETRY_BACKOFF_S')!r}") from _e

# =============================================================================
# Image Processing Configuration
# =============================================================================

# Page images embedded in cleaned_scan.pdf after deskew (see preprocessing/deskew.py).
#   "jpeg" — faster write, smaller file, lossy (uses CLEANED_SCAN_JPEG_QUALITY).
#   "png"  — lossless; still written with parallel encoding (slower than jpeg).
# Change default by editing the getenv second argument, or set CLEANED_SCAN_EMBED_FORMAT.
_csef = os.getenv("CLEANED_SCAN_EMBED_FORMAT", "jpeg").strip().lower()
CLEANED_SCAN_EMBED_FORMAT: str = _csef if _csef in ("jpeg", "png") else "jpeg"
CLEANED_SCAN_JPEG_QUALITY = int(os.getenv("CLEANED_SCAN_JPEG_QUALITY", "95"))

# Deprecated: step 4 (prepare_scans) is now the single rotation authority and
# already supports both AI-vision and Tesseract-OSD detection. The flag is
# retained so existing user envs don't fail at import, but it is no longer
# read by the scan pipeline.
_scan_tess_rot = os.getenv("SCAN_USE_TESSERACT_ROTATION", "").strip().lower()
SCAN_USE_TESSERACT_ROTATION: bool = _scan_tess_rot in ("1", "true", "yes", "on")

# After each half is deskewed, optionally run morphological vertical ruling-line detection.
# Default off (sidecar ``top`` / ``bot`` arrays stay empty). Set ``XSCORE_DESKEW_REFERENCE_LINES=1``
# to enable (debug reflines overlay only; IGCSE anchors are separate).
_deskew_refl = os.getenv("XSCORE_DESKEW_REFERENCE_LINES", "").strip().lower()
DESKEW_DETECT_REFERENCE_LINES: bool = _deskew_refl in ("1", "true", "yes", "on")

try:
    DESKEW_ACCURACY: float = float(os.getenv("DESKEW_ACCURACY", "0.01"))
except ValueError as _e:
    raise RuntimeError(
        f"DESKEW_ACCURACY must be a number; got: {os.getenv('DESKEW_ACCURACY')!r}"
    ) from _e

# =============================================================================
# Ensemble Configuration  (xscore/extraction/ benchmarking only — not the grading pipeline)
# =============================================================================

_use_ensemble = os.getenv("USE_ENSEMBLE", "").strip().lower()
USE_ENSEMBLE: bool = _use_ensemble in ("1", "true", "yes", "on")
ENSEMBLE_CALLS: int = int(os.getenv("ENSEMBLE_CALLS", "3"))

# =============================================================================
# Gemini Model Parameters
# =============================================================================

# Temperature controls randomness in model output.
# 0.0 = deterministic, higher = more creative.
GEMINI_TEMPERATURE: float = float(os.getenv("ALL_GEMINI_TEMPERATURE", "0.0"))

# Maximum output tokens for Gemini response.
GEMINI_MAX_OUTPUT_TOKENS: int = int(os.getenv("ALL_GEMINI_MAX_OUTPUT_TOKENS", "64000"))

# =============================================================================
# Kimi Model Parameters
# =============================================================================

# Maximum tokens for Kimi response
KIMI_MAX_TOKENS: int = int(os.getenv("KIMI_MAX_TOKENS", "64000"))

# =============================================================================
# Generic Pipeline Configuration (xscore.py)
# =============================================================================

# deskew + all coordinate-dependent geometric steps.
# All pixel coordinates in JSON sidecars share this DPI — change as a unit.
PIPELINE_DEFAULT_DPI: int = int(os.getenv("PIPELINE_DEFAULT_DPI", "300"))

# detect_blank_pages: raster (mean/std; 72 DPI is sufficient).
BLANK_DETECTION_DPI: int = int(os.getenv("BLANK_DETECTION_DPI", "72"))

# Recorded in scan_blanks.json as informational metadata. The autorotate step
# no longer rasterizes at this DPI (step 4 is the rotation authority); kept so
# existing audit JSONs and downstream consumers stay happy.
ROTATION_ANALYSIS_DPI: int = int(os.getenv("ROTATION_ANALYSIS_DPI", "150"))

# prepare_scans: orientation detector selection.
#   tesseract — primary; local OSD, no API cost, no API key (default)
#   ai        — vision-LLM path (kept as fallback for users without Tesseract)
#   auto      — tesseract if available, else fall back to ai silently
_orient_detector_raw = os.getenv("SCAN_ORIENTATION_DETECTOR", "tesseract").strip().lower()
SCAN_ORIENTATION_DETECTOR: str = (
    _orient_detector_raw if _orient_detector_raw in ("tesseract", "ai", "auto") else "tesseract"
)

# prepare_scans: two-stage Tesseract sampling targets *usable* votes (high-confidence
# OSD answers). Pages with low confidence or errors are skipped. We collect
# INITIAL_VOTES first; if they're not unanimous (or we couldn't fill the
# initial target), we collect ESCALATION_VOTES more and majority-vote across
# every successful vote. Both clamped to >= 1 / >= 0 at read time.
SCAN_ORIENTATION_INITIAL_VOTES: int = int(os.getenv("SCAN_ORIENTATION_INITIAL_VOTES", "3"))
SCAN_ORIENTATION_ESCALATION_VOTES: int = int(os.getenv("SCAN_ORIENTATION_ESCALATION_VOTES", "3"))

# prepare_scans: AI-fallback model — only used when SCAN_ORIENTATION_DETECTOR is
# "ai" (or "auto" and Tesseract is unavailable). See
# xscore/preprocessing/scan_orientation.py.
SCAN_ORIENTATION_MODEL: str = os.getenv("SCAN_ORIENTATION_MODEL", "gemini-3-flash-preview")

# Deprecation warnings for renamed knobs. Emitted once at module load.
def _warn_deprecated_orientation_knob(old: str, new: str) -> None:
    if os.getenv(old):
        # Print to stderr to match the existing pre-pipeline-init style; the
        # pipeline's own warn_line is wired up later.
        import sys
        print(
            f"WARNING: env var {old} is deprecated — use {new} instead.",
            file=sys.stderr,
        )

_warn_deprecated_orientation_knob("SCAN_ORIENTATION_INITIAL_PAGES", "SCAN_ORIENTATION_INITIAL_VOTES")
_warn_deprecated_orientation_knob("SCAN_ORIENTATION_ESCALATION_PAGES", "SCAN_ORIENTATION_ESCALATION_VOTES")
_warn_deprecated_orientation_knob("SCAN_ORIENTATION_COMPARE_TESSERACT", "SCAN_ORIENTATION_DETECTOR")

# cover_page_empty_exam: model for the informational check on the empty exam's first page.
EMPTY_EXAM_COVER_MODEL: str = os.getenv("EMPTY_EXAM_COVER_MODEL", "gemini-2.5-flash")

# cover_page_scan_first: model for the cover-page check on scan page 1.
# Independent of NAME_DETECTION_MODEL (name OCR) and EMPTY_EXAM_COVER_MODEL.
COVER_PAGE_DETECTION_MODEL: str = os.getenv("COVER_PAGE_DETECTION_MODEL", "gemini-2.5-flash")
COVER_PAGE_DETECTION_DPI: int = int(os.getenv("COVER_PAGE_DETECTION_DPI", "150"))

# student_names: name-recognition crop sent to vision API.
NAME_RECOGNITION_DPI: int = int(os.getenv("NAME_RECOGNITION_DPI", "300"))
NAME_JPEG_QUALITY: int = int(os.getenv("NAME_JPEG_QUALITY", "85"))

# ai_marking: full scan page sent to vision API for marking.
MARKING_DPI: int = int(os.getenv("MARKING_DPI", "300"))
MARKING_JPEG_QUALITY: int = int(os.getenv("MARKING_JPEG_QUALITY", "90"))

# student_handwriting_check (step 15): per-scan-page JPEG render parameters.
# Independent of the step-14 empty-exam classifier, which still uses the
# 150/75 defaults baked into _render_page_jpeg in blank_page_detection.py.
HANDWRITING_CHECK_JPEG_DPI: int = int(os.getenv("HANDWRITING_CHECK_JPEG_DPI", "150"))
HANDWRITING_CHECK_JPEG_QUALITY: int = int(os.getenv("HANDWRITING_CHECK_JPEG_QUALITY", "75"))

# Step 15 out-of-order recheck. When the primary call's page_number does
# not match the expected order (yellow line), re-call with this model
# at this DPI/quality. Empty model disables the recheck entirely.
HANDWRITING_CHECK_RECHECK_MODEL: str = os.getenv("HANDWRITING_CHECK_RECHECK_MODEL", "qwen3.6-plus, 0, 96")
HANDWRITING_CHECK_RECHECK_JPEG_DPI: int = int(os.getenv("HANDWRITING_CHECK_RECHECK_JPEG_DPI", "300"))
HANDWRITING_CHECK_RECHECK_JPEG_QUALITY: int = int(os.getenv("HANDWRITING_CHECK_RECHECK_JPEG_QUALITY", "95"))

# Per-page thinking-budget boost. When a page contains a question worth
# at least *_THRESHOLD marks, the thinking budget for that page's marking
# call is multiplied by *_MULTIPLIER. No-op when the model's base thinking
# budget is None or 0, or when MULTIPLIER is 1.
MARKING_THINKING_BOOST_THRESHOLD: int = int(os.getenv("MARKING_THINKING_BOOST_THRESHOLD", "10"))
MARKING_THINKING_BOOST_MULTIPLIER: float = float(os.getenv("MARKING_THINKING_BOOST_MULTIPLIER", "2"))

# Inter-call delays in the marking pipeline (rate limiting). Override via env if needed.
GRADE_QUESTION_DELAY_S: float = float(os.getenv("GRADE_QUESTION_DELAY_S", "0.0"))
PAGE_API_DELAY_S: float = float(os.getenv("PAGE_API_DELAY_S", "0.0"))


def apply_kimi_k2_extra(model: str, kwargs: dict[str, Any], *, thinking: bool = False) -> None:
    """If *model* is a kimi-k2.x id, set ``kwargs['extra_body']`` for the thinking API.

    No-op for other model ids. *thinking* True → ``\"enabled\"``, False → ``\"disabled\"``.
    """
    if model.lower().startswith("kimi-k2"):
        kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}


# =============================================================================
# Per-step model defaults (scaffold + marking)
# Each reads its dedicated env var first, then falls back to AI_DEFAULT_MODEL or
# a hard-coded default that matches the value in default.env.
# =============================================================================

_ai_default = os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash")

EXTRACT_EXAM_QUESTION_NUMBERS_MODEL: str = os.getenv("EXTRACT_EXAM_QUESTION_NUMBERS_MODEL") or "gemini-3-flash-preview, 2048, 8192"
EXTRACT_EXAM_QUESTIONS_MODEL: str = os.getenv("EXTRACT_EXAM_QUESTIONS_MODEL") or _ai_default
READ_MARK_SCHEME_MODEL: str = os.getenv("READ_MARK_SCHEME_MODEL") or _ai_default
DETECT_LAYOUT_MODEL: str = os.getenv("DETECT_LAYOUT_MODEL") or "gemini-2.5-flash, low"
DETECT_SCHEME_GRAPHICS_MODEL: str = os.getenv("DETECT_SCHEME_GRAPHICS_MODEL") or "gemini-2.5-flash, off"

MARKING_MODEL_DEFAULT: str = os.getenv("MARKING_MODEL") or "qwen3.6-plus, low"

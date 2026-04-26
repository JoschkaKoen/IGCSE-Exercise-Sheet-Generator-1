#!/usr/bin/env python3
"""
config.py
---------
Configuration for xScore. Edit values here or set the noted environment
variables. See README.md for full detail.

AI provider usage by step (live 30-step pipeline; see ``xscore.shared.pipeline_steps.STEPS``):
  Step 1               : KIMI_API_KEY  — natural-language prompt parsing
  Step 9               : Gemini        — empty-exam cover-page check
  Step 10              : Gemini        — scan cover-page detection
  Step 11              : configurable  — student-name OCR (NAME_DETECTION_MODEL)
  Steps 15 / 17 / 18 / 19: configurable — exam/mark-scheme parsing
                          (DETECT_LAYOUT_MODEL, READ_EXAM_PDF_MODEL,
                           DETECT_SCHEME_GRAPHICS_MODEL, READ_MARK_SCHEME_MODEL)
  Step 23              : configurable  — AI marking (MARKING_MODEL)

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
from pathlib import Path
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

# Class scan rotation (see preprocessing/remove_blanks_autorotate.py).
# False (default): trust PDF /Rotate per page; only one Poppler raster (72 DPI) for blank detection.
# True: add a full-DPI raster + Tesseract OSD for extra rotation hints (slow; rare mis-scanned PDFs).
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
# Paths and File Handling
# =============================================================================

# Default PDF — leave empty so the folder-find logic raises a clear error if no folder is given
DEFAULT_PDF = ""

# Ground truth file path (for accuracy evaluation; repo-relative)
GROUND_TRUTH_PATH = Path(__file__).resolve().parent / "Ground Truth"

# =============================================================================
# Generic Pipeline Configuration (xscore.py)
# =============================================================================

# Steps 9–11: deskew + all coordinate-dependent geometric steps.
# All pixel coordinates in JSON sidecars share this DPI — change as a unit.
PIPELINE_DEFAULT_DPI: int = int(os.getenv("PIPELINE_DEFAULT_DPI", "300"))

# Step 5: blank-page detection raster (mean/std; 72 DPI is sufficient).
BLANK_DETECTION_DPI: int = int(os.getenv("BLANK_DETECTION_DPI", "72"))

# Step 6: Tesseract OSD rotation (only when SCAN_USE_TESSERACT_ROTATION=true).
ROTATION_ANALYSIS_DPI: int = int(os.getenv("ROTATION_ANALYSIS_DPI", "150"))

# Step 9: model for the informational check on the empty exam's first page.
EMPTY_EXAM_COVER_MODEL: str = os.getenv("EMPTY_EXAM_COVER_MODEL", "gemini-2.5-flash")

# Step 10: model for the authoritative cover-page check on scan page 1 and per-block verification.
# Independent of NAME_DETECTION_MODEL (name OCR) and EMPTY_EXAM_COVER_MODEL.
COVER_PAGE_DETECTION_MODEL: str = os.getenv("COVER_PAGE_DETECTION_MODEL", "gemini-2.5-flash")
COVER_PAGE_DETECTION_DPI: int = int(os.getenv("COVER_PAGE_DETECTION_DPI", "150"))

# Step 11: name-recognition crop sent to vision API.
NAME_RECOGNITION_DPI: int = int(os.getenv("NAME_RECOGNITION_DPI", "300"))
NAME_JPEG_QUALITY: int = int(os.getenv("NAME_JPEG_QUALITY", "85"))

# Step 23: full scan page sent to vision API for marking.
MARKING_DPI: int = int(os.getenv("MARKING_DPI", "300"))
MARKING_JPEG_QUALITY: int = int(os.getenv("MARKING_JPEG_QUALITY", "90"))

# Inter-call delays in the marking pipeline (rate limiting). Override via env if needed.
GRADE_QUESTION_DELAY_S: float = float(os.getenv("GRADE_QUESTION_DELAY_S", "0.0"))
PAGE_API_DELAY_S: float = float(os.getenv("PAGE_API_DELAY_S", "0.0"))


def apply_kimi_k2_extra(model: str, kwargs: dict[str, Any], *, thinking: bool = False) -> None:
    """If *model* is a kimi-k2.x id, set ``kwargs['extra_body']`` for the thinking API.

    No-op for other model ids. *thinking* True → ``\"enabled\"``, False → ``\"disabled\"``.
    """
    if model.startswith("kimi-k2"):
        kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}


# =============================================================================
# Per-step model defaults (scaffold + marking)
# Each reads its dedicated env var first, then falls back to AI_DEFAULT_MODEL or
# a hard-coded default that matches the value in default.env.
# =============================================================================

_ai_default = os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash")

READ_EXAM_PDF_MODEL: str = os.getenv("READ_EXAM_PDF_MODEL") or _ai_default
READ_MARK_SCHEME_MODEL: str = os.getenv("READ_MARK_SCHEME_MODEL") or _ai_default
DETECT_LAYOUT_MODEL: str = os.getenv("DETECT_LAYOUT_MODEL", "gemini-2.5-flash, low")
DETECT_SCHEME_GRAPHICS_MODEL: str = os.getenv("DETECT_SCHEME_GRAPHICS_MODEL", "gemini-2.5-flash, off")

MARKING_MODEL_DEFAULT: str = os.getenv("MARKING_MODEL", "qwen3.6-plus, low")

AI_OUTPUT_FORMAT: str = os.getenv("ALL_AI_OUTPUT_FORMAT", "yaml").strip().lower()

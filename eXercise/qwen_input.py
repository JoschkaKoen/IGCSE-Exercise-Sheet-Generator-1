"""Qwen native PDF input helpers (DashScope file-extract).

DashScope's ``compatible-mode/v1`` endpoint accepts PDFs uploaded via
``client.files.create(file=path, purpose="file-extract")``. The returned
file ID is referenced in the chat call via a system message of the form
``{"role": "system", "content": "fileid://<id>"}`` — distinct from OpenAI's
``{"type": "file", "file_id": ...}`` content block, which DashScope ignores.

This module provides the two primitives — upload + system-message builder —
used by the call sites that currently route Qwen through PNG/JPEG
``image_url`` blocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Empirically verified via scripts/diagnose_qwen_pdf_upload.py against DashScope
# (Singapore international endpoint), 2026-04-28. Only these model families
# actually consume the ``fileid://`` system message; qwen3.6-flash, qwen3.6-plus,
# and qwen3-vl-plus all return a refusal ("I cannot access the file referenced
# by fileid://"). Add new entries here only after verifying with the smoke test.
_QWEN_PDF_PREFIXES: tuple[str, ...] = ("qwen-doc-turbo", "qwen-long")

# Multi-file ``fileid://`` (more than one PDF in a single chat call).
# qwen-doc-turbo's doc-id mode caps at 1 file per request; qwen-long is
# documented to accept up to 100. Used by callers that send exercise + answer
# PDFs together (e.g. difficulty ranking).
_QWEN_MULTI_PDF_PREFIXES: tuple[str, ...] = ("qwen-long",)


def model_supports_pdf_input(model_id: str) -> bool:
    """Return True if *model_id* accepts the DashScope ``fileid://`` pattern."""
    return any(model_id.startswith(p) for p in _QWEN_PDF_PREFIXES)


def model_supports_multi_pdf_input(model_id: str) -> bool:
    """Return True if *model_id* accepts more than one ``fileid://`` per call."""
    return any(model_id.startswith(p) for p in _QWEN_MULTI_PDF_PREFIXES)


def upload_pdf_for_extract(client: Any, pdf_path: Path) -> str:
    """Upload *pdf_path* to DashScope file-extract; return the file id.

    The upload is free; files persist against the account quota (10,000 files
    / 100 GB total) with no expiration. Callers that re-upload the same
    content repeatedly may want a SHA256-keyed cache; this helper does not
    cache.

    Parameters
    ----------
    client:
        OpenAI-compat client returned by :func:`make_ai_client` for a Qwen
        provider — i.e. configured against ``dashscope.aliyuncs.com/compatible-mode/v1``.
    pdf_path:
        Local path to the PDF. Must exist and be ≤150 MB (DashScope cap).
    """
    file_obj = client.files.create(file=Path(pdf_path), purpose="file-extract")
    return file_obj.id


def qwen_pdf_system_message(file_id: str) -> dict:
    """Return the ``fileid://<id>`` system message that references an uploaded PDF.

    Insert this message *after* the original system prompt and *before* the
    user message. DashScope expects two distinct system messages — the prompt
    and the file reference — not a merged single one.
    """
    return {"role": "system", "content": f"fileid://{file_id}"}

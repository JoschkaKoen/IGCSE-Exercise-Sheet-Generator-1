"""Layout detection, PDF splitting, and layout artifact helpers for the scaffold pipeline."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from pathlib import Path

from eXercise.api_retry import retry_api_call
from xscore.scaffold.scaffold_prompts import (
    _LAYOUT_DETECT_JSON_SCHEMA,
    _LayoutDetectSchema,
    _SYSTEM_LAYOUT,
    _USER_LAYOUT,
)


def _detect_layout(
    client, exam_pdf: Path, model: str,
    thinking_tokens: int | None = None,
    max_tokens: int | None = None,
) -> tuple["_LayoutDetectSchema", float, "str | None", str, "str | None"]:
    """Cheap layout detection: render first page as JPEG, ask the model for rows/cols/order.

    Routes to the right provider based on *model*; *client* is used only on the
    Gemini branch (Qwen/Grok build their own OpenAI-compat client internally).

    Returns (result, elapsed_s, raw_response_text, thinking_text, error_summary).
    On success: error_summary is None.
    On failure: falls back to 1×1; error_summary is a one-line description; raw_response_text
    may still be set if the API succeeded but JSON parsing failed.
    """
    import fitz

    with fitz.open(str(exam_pdf)) as doc:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.0, 1.0))  # 72 DPI
    img_bytes = pix.tobytes("jpeg")

    from xscore.config import GEMINI_MAX_OUTPUT_TOKENS

    raw_text: str | None = None
    thinking_text: str = ""
    t0 = time.perf_counter()
    last_exc: Exception = RuntimeError("no attempts made")

    if model.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config, split_gemini_response
        cfg_kwargs: dict = {
            "max_output_tokens": max_tokens or GEMINI_MAX_OUTPUT_TOKENS,
            "response_mime_type": "application/json",
            "response_json_schema": _LAYOUT_DETECT_JSON_SCHEMA,
        }
        if thinking_tokens is not None:
            cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
        cfg = gai_types.GenerateContentConfig(system_instruction=_SYSTEM_LAYOUT, **cfg_kwargs)

        def _do_gemini() -> tuple[str | None, str, "_LayoutDetectSchema"]:
            _resp = client.models.generate_content(
                model=model,
                contents=[
                    gai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                    gai_types.Part.from_text(text=_USER_LAYOUT),
                ],
                config=cfg,
            )
            _raw, _th = split_gemini_response(_resp)
            return _raw, _th, _LayoutDetectSchema.model_validate_json(_raw)

        try:
            raw_text, thinking_text, result = retry_api_call(
                _do_gemini, label="Layout detection",
            )
            elapsed = time.perf_counter() - t0
            return result, elapsed, raw_text, thinking_text, None
        except Exception as exc:
            last_exc = exc
    else:
        # OpenAI-compat path (Qwen, Grok, …)
        import base64 as _base64
        from eXercise.ai_client import (
            build_completion_kwargs,
            collect_streamed_response,
            make_ai_client,
        )
        _result = make_ai_client(model_env="DETECT_LAYOUT_MODEL")
        if _result is None:
            elapsed = time.perf_counter() - t0
            return (
                _LayoutDetectSchema(rows=1, cols=1, reading_order=[]),
                elapsed, None, "",
                f"DETECT_LAYOUT_MODEL={model} requires API key for its provider",
            )
        _oa_client, _, _provider, _, _ = _result
        _use_stream, _kw = build_completion_kwargs(
            _provider, thinking_tokens, max_tokens or GEMINI_MAX_OUTPUT_TOKENS,
        )
        _b64 = _base64.b64encode(img_bytes).decode()
        _msgs = [
            {"role": "system", "content": _SYSTEM_LAYOUT},
            {"role": "user", "content": [
                {"type": "text", "text": _USER_LAYOUT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64}"}},
            ]},
        ]
        _strict_rf = {
            "type": "json_schema",
            "json_schema": {
                "name": "layout",
                "schema": _LAYOUT_DETECT_JSON_SCHEMA,
                "strict": True,
            },
        }

        def _do_oa(rf: dict | None) -> tuple[str, str]:
            if _use_stream:
                _th: list[str] = []
                _stream = _oa_client.chat.completions.create(
                    model=model, messages=_msgs, stream=True, **_kw,
                )
                _raw = collect_streamed_response(_stream, thinking_out=_th)
                return _raw, "".join(_th)
            extra = {"response_format": rf} if rf is not None else {}
            _resp = _oa_client.chat.completions.create(
                model=model, messages=_msgs, **extra, **_kw,
            )
            return (
                _resp.choices[0].message.content or "",
                getattr(_resp.choices[0].message, "reasoning_content", "") or "",
            )

        # Format cascade: strict json_schema → json_object → plain.
        # Skip strict json_schema on providers that reject it together with a
        # system message (DashScope/Qwen — verified via
        # scripts/diagnose_qwen_json_schema.py).
        from eXercise.ai_client import provider_supports_json_schema_with_system  # noqa: PLC0415
        formats: list[tuple[str, dict | None]] = []
        if provider_supports_json_schema_with_system(_provider):
            formats.append(("json_schema", _strict_rf))
        formats.append(("json_object", {"type": "json_object"}))
        formats.append(("plain", None))

        raw_text, thinking_text = "", ""
        for _fmt_name, _fmt_rf in formats:
            try:
                raw_text, thinking_text = retry_api_call(
                    lambda rf=_fmt_rf: _do_oa(rf),
                    label=f"Layout detection ({_fmt_name})",
                )
                break
            except Exception as exc:
                last_exc = exc

        if raw_text:
            try:
                result = _LayoutDetectSchema.model_validate_json(raw_text)
                elapsed = time.perf_counter() - t0
                return result, elapsed, raw_text, thinking_text, None
            except Exception as exc:
                last_exc = exc

    elapsed = time.perf_counter() - t0
    err_summary = str(last_exc).split("\n")[0]
    return _LayoutDetectSchema(rows=1, cols=1, reading_order=[]), elapsed, raw_text, thinking_text, err_summary


def _order_cells(page_rect, layout: "_LayoutDetectSchema") -> list:
    """Crop rects for *page_rect* in the detected reading order (row, col entries are 1-based)."""
    import fitz

    r = page_rect
    cw = r.width / layout.cols
    rh = r.height / layout.rows

    def cell(row: int, col: int) -> "fitz.Rect":
        return fitz.Rect(
            r.x0 + (col - 1) * cw, r.y0 + (row - 1) * rh,
            r.x0 + col * cw,       r.y0 + row * rh,
        )

    order = layout.reading_order
    if not order:
        order = [[row + 1, col + 1] for row in range(layout.rows) for col in range(layout.cols)]
    return [cell(rc[0], rc[1]) for rc in order]


def _split_pdf_by_layout(exam_pdf: Path, layout: "_LayoutDetectSchema") -> tuple[Path, int, int]:
    """Split *exam_pdf* into a temp PDF where each page = one sub-page in reading order.

    Returns *(temp_path, n_physical_pages, n_split_pages)*.
    The caller must delete *temp_path* when done.
    """
    import fitz
    import tempfile

    src = fitz.open(str(exam_pdf))
    dst = fitz.open()
    for page_idx in range(len(src)):
        for cell in _order_cells(src[page_idx].rect, layout):
            new_page = dst.new_page(width=cell.width, height=cell.height)
            new_page.show_pdf_page(new_page.rect, src, page_idx, clip=cell)
    n_physical = len(src)
    n_split = len(dst)
    src.close()

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    dst.save(str(tmp_path))
    dst.close()
    return tmp_path, n_physical, n_split


def _cell_label(row: int, col: int) -> str:
    return ("T" if row == 1 else "B") + ("L" if col == 1 else "R")


def _serialize_layout_xml(
    layout: "_LayoutDetectSchema",
    model: str,
    elapsed: float,
    n_physical: int,
    n_split: int,
) -> str:
    _LABEL = {(1, 1): "TL", (1, 2): "TR", (2, 1): "BL", (2, 2): "BR"}
    root = ET.Element("layout")
    root.set("rows", str(layout.rows))
    root.set("cols", str(layout.cols))
    root.set("model", model)
    root.set("elapsed_s", f"{elapsed:.2f}")
    root.set("n_physical_pages", str(n_physical))
    root.set("n_split_pages", str(n_split))
    order = layout.reading_order or [
        [r + 1, c + 1] for r in range(layout.rows) for c in range(layout.cols)
    ]
    labels = [_LABEL.get((rc[0], rc[1]), f"r{rc[0]}c{rc[1]}") for rc in order]
    root.set("reading_order", " ".join(labels))
    for i, (rc, label) in enumerate(zip(order, labels)):
        cel = ET.SubElement(root, "cell")
        cel.set("position", str(i + 1))
        cel.set("row", str(rc[0]))
        cel.set("col", str(rc[1]))
        cel.set("label", label)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def _save_layout_artifact(
    artifact_dir: Path,
    layout: "_LayoutDetectSchema",
    model: str,
    elapsed: float,
    n_physical: int,
    n_split: int,
) -> None:
    """Write step-14 (split mode) layout detection artifacts to artifact_dir."""
    from xscore.shared.exam_paths import (
        artifact_exam_layout_markdown_path,
        artifact_exam_layout_xml_path,
    )

    n_cells = layout.rows * layout.cols
    order = layout.reading_order or [
        [r + 1, c + 1] for r in range(layout.rows) for c in range(layout.cols)
    ]
    order_labels = [_cell_label(rc[0], rc[1]) for rc in order]

    if n_cells > 1:
        layout_label = f"{layout.rows}×{layout.cols} ({n_cells}-up)"
    else:
        layout_label = "1×1 (single)"

    if n_cells > 1:
        order_str = " → ".join(order_labels)
        md_lines = [
            "# Exam Layout",
            "",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Layout | {layout_label} |",
            f"| Reading order | {order_str} |",
            f"| Physical pages | {n_physical} |",
            f"| Sub-pages | {n_split} |",
            f"| Model | {model} |",
            f"| Elapsed | {elapsed:.1f} s |",
            "",
        ]
    else:
        md_lines = [
            "# Exam Layout",
            "",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Layout | {layout_label} |",
            f"| Model | {model} |",
            f"| Elapsed | {elapsed:.1f} s |",
            "",
        ]

    try:
        p = artifact_exam_layout_xml_path(artifact_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_serialize_layout_xml(layout, model, elapsed, n_physical, n_split), encoding="utf-8")

        with open(artifact_exam_layout_markdown_path(artifact_dir), "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))
    except OSError:
        pass

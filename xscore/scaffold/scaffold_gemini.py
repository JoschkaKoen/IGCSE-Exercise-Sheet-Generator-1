"""Gemini API helpers, generation config, and exam/scheme inference calls for the scaffold pipeline."""

from __future__ import annotations

import base64 as _base64
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.genai import types as gai_types

from eXercise.ai_client import (
    build_completion_kwargs,
    build_gemini_thinking_config,
    collect_streamed_response,
    make_ai_client,
    split_gemini_response,
)
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS
from xscore.scaffold.scaffold_prompts import (
    _SCHEME_GRAPHICS_JSON_SCHEMA,
    _USER_GRAPHICS,
)
from xscore.scaffold.scaffold_xml import (
    _extract_scheme_graphics,
    _merge_scheme_results,
)
from xscore.shared.exam_paths import (
    artifact_exam_questions_raw_path,
    artifact_exam_questions_raw_xml_path,
    artifact_mark_scheme_graphics_dir,
    artifact_mark_scheme_graphics_json_path,
    artifact_mark_scheme_pages_dir,
    artifact_mark_scheme_path,
    artifact_mark_scheme_xml_path,
    artifact_scaffold_prompt_path,
)
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.terminal_ui import (
    api_latency_line,
    format_duration,
    info_line,
    ok_line,
    warn_line,
)


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _upload_and_poll(client, path: Path, label: str):
    """Upload *path* to the Gemini Files API, poll until ACTIVE, return the file object.

    Polling cadence is configurable via env vars ``GEMINI_UPLOAD_TIMEOUT_S``
    (default 360) and ``GEMINI_UPLOAD_POLL_S`` (default 3).
    """
    f = client.files.upload(file=path)
    _interval = float(os.environ.get("GEMINI_UPLOAD_POLL_S", "3"))
    _timeout = float(os.environ.get("GEMINI_UPLOAD_TIMEOUT_S", "360"))
    _max_iters = max(1, int(_timeout / _interval))
    for _ in range(_max_iters):
        if getattr(f.state, "name", str(f.state)) != "PROCESSING":
            break
        time.sleep(_interval)
        f = client.files.get(name=f.name)
    else:
        raise TimeoutError(f"Gemini file upload timed out after {_timeout:.0f}s ({label}): {f.name}")
    state = getattr(f.state, "name", str(f.state))
    if state == "FAILED":
        raise RuntimeError(f"Gemini file processing failed ({label}): {f.name}")
    return f


def _extract_text(resp) -> str:
    """Return resp.text, tolerating None and empty-candidates responses."""
    try:
        return resp.text or ""
    except (AttributeError, ValueError):
        return ""


def _finish_reason(resp) -> str:
    """Return a human-readable diagnostic: finish_reason + block_reason if set."""
    parts = []
    try:
        if resp.candidates:
            parts.append(f"finish_reason={resp.candidates[0].finish_reason.name}")
        pf = getattr(resp, "prompt_feedback", None)
        if pf and getattr(pf, "block_reason", None):
            parts.append(f"block_reason={pf.block_reason.name}")
    except Exception:
        pass
    return ", ".join(parts) or "unknown"


# ---------------------------------------------------------------------------
# Generation config builder
# ---------------------------------------------------------------------------

def _make_gen_config(
    thinking_tokens: int | None, system: str,
    schema: dict | None = None,
    pydantic_schema=None,
    max_tokens: int | None = None,
) -> "gai_types.GenerateContentConfig":
    cfg: dict = {"max_output_tokens": max_tokens or GEMINI_MAX_OUTPUT_TOKENS}
    if pydantic_schema is not None:
        cfg["response_mime_type"] = "application/json"
        cfg["response_schema"] = pydantic_schema
    elif schema is not None:
        cfg["response_mime_type"] = "application/json"
        cfg["response_json_schema"] = schema
    if thinking_tokens is not None:
        cfg["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
    return gai_types.GenerateContentConfig(system_instruction=system, **cfg)


# ---------------------------------------------------------------------------
# Exam extraction
# ---------------------------------------------------------------------------

def _do_exam_call(
    client,
    exam_model: str,
    exam_thinking: int | None,
    exam_max_tokens: int | None,
    *,
    actual_exam_pdf: Path,
    layout_result,
    split_pdf_path: "Path | None",
    n_split_pages: int,
    artifact_dir: "Path | None",
    fmt=None,
) -> tuple[list[dict], dict]:
    if fmt is None:
        from xscore.scaffold.formats.xml_format import XmlScaffoldFormat
        fmt = XmlScaffoldFormat()
    user_exam = fmt.build_exam_prompt(layout_result, split_pdf_path is not None, n_split_pages)

    # Non-Gemini path: OpenAI-compatible client + base64 PNG images
    _oa_client = None
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}
    if not exam_model.startswith("gemini"):
        _oa_result = make_ai_client(model_env="READ_EXAM_PDF_MODEL")
        if _oa_result is None:
            raise RuntimeError(f"No API key set for exam model {exam_model!r}")
        _oa_client, _, _oa_provider, _, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_completion_kwargs(
            _oa_provider, exam_thinking, exam_max_tokens
        )

    # Gemini: upload PDF.  Qwen: rasterize all pages to PNG (300 DPI by default).
    exam_file = None
    _exam_page_b64s: list[str] = []
    if _oa_client is None:
        exam_file = _upload_and_poll(client, actual_exam_pdf, "exam")
    else:
        import fitz as _fitz
        _dpi = int(os.environ.get("READ_EXAM_PDF_DPI", "300"))
        with _fitz.open(str(actual_exam_pdf)) as _doc:
            for _i in range(_doc.page_count):
                pix = _doc[_i].get_pixmap(dpi=_dpi)
                _exam_page_b64s.append(_base64.b64encode(pix.tobytes("png")).decode())
                pix = None  # release pixmap memory

    def _make_exam_call(label: str | None) -> tuple[str, str]:
        _t0 = time.perf_counter()
        thinking_text = ""
        if _oa_client is not None:
            _content = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                for b64 in _exam_page_b64s
            ]
            _content.append({"type": "text", "text": user_exam})
            kwargs: dict = dict(
                model=exam_model,
                messages=[
                    {"role": "system", "content": fmt.system_exam_prompt()},
                    {"role": "user", "content": _content},
                ],
            )
            kwargs.update(_oa_thinking_kw)
            if _oa_use_stream:
                _th: list[str] = []
                stream = _oa_client.chat.completions.create(**kwargs, stream=True)
                raw = collect_streamed_response(stream, thinking_out=_th)
                thinking_text = "".join(_th)
            else:
                resp = _oa_client.chat.completions.create(**kwargs)
                raw = resp.choices[0].message.content or ""
                thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
        else:
            resp = client.models.generate_content(
                model=exam_model,
                contents=[
                    gai_types.Part.from_uri(file_uri=exam_file.uri, mime_type="application/pdf"),
                    gai_types.Part.from_text(text=user_exam),
                ],
                config=_make_gen_config(
                    exam_thinking, fmt.system_exam_prompt(),
                    pydantic_schema=fmt.pydantic_schema_exam(),
                    max_tokens=exam_max_tokens,
                ),
            )
            raw, thinking_text = split_gemini_response(resp)
        api_latency_line(time.perf_counter() - _t0, label=label)
        return raw, thinking_text

    try:
        raw_exam, exam_thinking_text = _make_exam_call(None)
        if not raw_exam:
            warn_line("Exam API: empty response — retrying once …")
            raw_exam, exam_thinking_text = _make_exam_call("exam retry")
            if not raw_exam:
                if artifact_dir is not None:
                    try:
                        p = artifact_exam_questions_raw_path(artifact_dir, fmt=fmt.artifact_ext())
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text("# empty response after retry", encoding="utf-8")
                    except OSError as e:
                        warn_line(f"Could not save empty-response stub: {e}")
                raise RuntimeError(f"Exam response empty after retry — {exam_model}")
        if artifact_dir is not None:
            _exam_prompt_path = artifact_scaffold_prompt_path(artifact_dir, "exam_questions")
            save_prompt(
                _exam_prompt_path,
                model=exam_model, system=fmt.system_exam_prompt(),
                messages=[{
                    "role": "user",
                    "content": f"[PDF: {actual_exam_pdf.name}]\n\n{user_exam}",
                }],
            )
            save_response(_exam_prompt_path, raw_exam, thinking=exam_thinking_text)
            try:
                p = artifact_exam_questions_raw_path(artifact_dir, fmt=fmt.artifact_ext())
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(raw_exam, encoding="utf-8")
            except OSError as e:
                warn_line(f"Could not save raw exam response: {e}")
        try:
            return fmt.parse_exam_response(raw_exam)
        except Exception as exc:
            raise RuntimeError(
                f"Exam response failed parsing: {exc}: {raw_exam[:300]!r}"
            )
    finally:
        if exam_file is not None:
            try:
                client.files.delete(name=exam_file.name)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Mark scheme preprocessing — shared by step 18 (graphics) and step 19 (scheme parse)
# ---------------------------------------------------------------------------

def _norm_qnum(s: str) -> str:
    return re.sub(r"[()]", "", s)


def split_mark_scheme_into_pages(
    marking_scheme_pdf: Path, artifact_dir: "Path | None"
) -> tuple[int, list[Path], "Path | None"]:
    """Split *marking_scheme_pdf* into single-page PDFs under step-18's pages dir.

    Returns ``(n_pages, page_paths, tmp_dir)``. ``tmp_dir`` is non-None only when
    ``artifact_dir`` is None (caller is responsible for cleanup).
    """
    import fitz

    _tmp_dir: Path | None = None
    if artifact_dir is not None:
        pages_dir = artifact_mark_scheme_pages_dir(artifact_dir)
    else:
        import tempfile
        _tmp_dir = Path(tempfile.mkdtemp())
        pages_dir = _tmp_dir
    pages_dir.mkdir(parents=True, exist_ok=True)

    page_paths: list[Path] = []
    with fitz.open(str(marking_scheme_pdf)) as _doc:
        n_pages = _doc.page_count
        for _i in range(n_pages):
            _out_path = pages_dir / f"page_{_i + 1}.pdf"
            _out = fitz.open()
            try:
                _out.insert_pdf(_doc, from_page=_i, to_page=_i)
                _out.save(str(_out_path))
            finally:
                _out.close()
            page_paths.append(_out_path)
    return n_pages, page_paths, _tmp_dir


def _rasterize_scheme_pages(marking_scheme_pdf: Path, n_pages: int) -> dict[int, bytes]:
    """Rasterize each mark scheme page to PNG (DPI controlled by MARK_SCHEME_GRAPHICS_DPI)."""
    import fitz
    _gfx_dpi = int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300"))
    page_pngs: dict[int, bytes] = {}
    with fitz.open(str(marking_scheme_pdf)) as _doc_r:
        for _i in range(n_pages):
            pix = _doc_r[_i].get_pixmap(dpi=_gfx_dpi)
            page_pngs[_i + 1] = pix.tobytes("png")
            pix = None  # release pixmap memory
    return page_pngs


# ---------------------------------------------------------------------------
# Step 18 — Detect mark scheme graphics
# ---------------------------------------------------------------------------

def detect_scheme_graphics(
    marking_scheme_pdf: Path,
    scaffold_str: str,
    *,
    artifact_dir: "Path | None",
    fmt=None,
) -> tuple[dict, list[dict] | None]:
    """Detect graphics in the mark scheme via vision API.

    Splits the mark scheme into per-page PDFs (always — needed by step 19 too)
    then, if ``DETECT_SCHEME_GRAPHICS_MODEL`` is set, runs graphics detection on
    each rasterized page in parallel.

    Returns ``(graphics_by_qnum, graphics_questions)`` where:
      * ``graphics_by_qnum`` is ``{normalised_qnum: [{page, x0, y0, x1, y1}, ...]}``
        — empty when graphics detection is skipped.
      * ``graphics_questions`` is the per-question list used by downstream artifact
        extraction — ``None`` when detection was skipped (no model configured),
        ``[]`` when run but no graphics found.

    Side effects: writes per-page PDFs to step-18's pages dir, plus graphics JSON
    and extracted graphic images when graphics are detected.
    """
    if fmt is None:
        from xscore.scaffold.formats.xml_format import XmlScaffoldFormat
        fmt = XmlScaffoldFormat()

    n_pages, page_paths, _tmp_dir = split_mark_scheme_into_pages(marking_scheme_pdf, artifact_dir)

    _gfx_client_result = make_ai_client(model_env="DETECT_SCHEME_GRAPHICS_MODEL")
    if _gfx_client_result is None:
        if _tmp_dir is not None:
            import shutil
            shutil.rmtree(_tmp_dir, ignore_errors=True)
        return {}, None

    page_pngs = _rasterize_scheme_pages(marking_scheme_pdf, n_pages)

    _gfx_schema_str = json.dumps(_SCHEME_GRAPHICS_JSON_SCHEMA, indent=2)
    from xscore.prompts.loader import load_prompt as _load_prompt
    _, _gfx_system = _load_prompt(
        "detect_mark_scheme_graphics", section="system", schema=_gfx_schema_str,
    )

    _all_qnums = fmt.extract_question_numbers(scaffold_str)
    _qnum_hint = ", ".join(f'"{n}"' for n in _all_qnums)

    _det_client, _det_model, _det_provider, _det_thinking, _det_max_tok = _gfx_client_result
    _, _det_thinking_kw = build_completion_kwargs(
        _det_provider, _det_thinking, _det_max_tok
    )
    info_line(f"Detecting graphics ({_det_model}) …")

    def _detect_graphics_page(page_num: int) -> dict:
        b64 = _base64.b64encode(page_pngs[page_num]).decode()
        _t0 = time.perf_counter()
        _hint = (
            f"Valid question numbers in this mark scheme: {_qnum_hint}\n"
            "Return exactly one of these as the question_number for each graphic.\n\n"
        ) if _qnum_hint else ""
        _user_msg = _hint + _USER_GRAPHICS
        try:
            resp = _det_client.chat.completions.create(
                model=_det_model,
                messages=[
                    {"role": "system", "content": _gfx_system},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": _user_msg},
                    ]},
                ],
                response_format={"type": "json_object"},
                **_det_thinking_kw,
            )
            raw = resp.choices[0].message.content or '{"graphics":[]}'
            _thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
        except Exception as _exc:
            warn_line(
                f"Scheme graphics p{page_num}: API error  ·  "
                f"{format_duration(time.perf_counter() - _t0)}  —  {_exc}"
            )
            return {"questions": []}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            ok_line(f"p{page_num}  ·  {format_duration(time.perf_counter() - _t0)}")
            return {"questions": []}
        if artifact_dir is not None:
            _prompt_path = artifact_scaffold_prompt_path(
                artifact_dir, f"mark_scheme_graphics_detect_p{page_num}"
            )
            save_prompt(
                _prompt_path, model=_det_model, system=_gfx_system,
                messages=[{"role": "user", "content": f"[PNG: p{page_num}]\n\n{_user_msg}"}],
            )
            save_response(_prompt_path, raw, thinking=_thinking_text)
        questions_map: dict[str, list] = {}
        for g in data.get("graphics", []):
            qnum = str(g.get("question_number", "")).strip()
            bbox = g.get("bbox") or []
            if not qnum or len(bbox) != 4:
                continue
            x_min, y_min, x_max, y_max = bbox
            questions_map.setdefault(qnum, []).append({
                "page": page_num,
                "x0": x_min / 1000.0, "y0": y_min / 1000.0,
                "x1": x_max / 1000.0, "y1": y_max / 1000.0,
            })
        _qnums_str = (
            f"  q{', q'.join(questions_map.keys())}"
            if questions_map else ""
        )
        ok_line(f"p{page_num}{_qnums_str}  ·  {format_duration(time.perf_counter() - _t0)}")
        return {
            "questions": [
                {"number": qnum, "correct_answer": None, "mark_scheme": [], "graphics": gfx}
                for qnum, gfx in questions_map.items()
            ]
        }

    with ThreadPoolExecutor(max_workers=min(n_pages, int(os.environ.get("SCHEME_GRAPHICS_WORKERS", "500")))) as pool:
        graphics_page_results = list(pool.map(_detect_graphics_page, range(1, n_pages + 1)))

    _graphics_merged = _merge_scheme_results(graphics_page_results)
    _graphics_by_qnum = {
        _norm_qnum(q["number"]): q["graphics"]
        for q in _graphics_merged.get("questions", [])
        if q.get("graphics")
    }
    _n_graphics = sum(len(g) for g in _graphics_by_qnum.values())

    if artifact_dir is not None:
        try:
            p = artifact_mark_scheme_graphics_json_path(artifact_dir)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(_graphics_merged, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            warn_line(f"Could not save mark-scheme graphics JSON: {e}")

    if artifact_dir is not None and _n_graphics:
        _graphics_margin = float(os.environ.get("SCHEME_GRAPHICS_MARGIN", "0.01"))
        _gfx_dpi = int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300"))
        try:
            _extract_scheme_graphics(
                _graphics_merged.get("questions", []),
                marking_scheme_pdf,
                artifact_mark_scheme_graphics_dir(artifact_dir),
                dpi=_gfx_dpi,
                margin=_graphics_margin,
            )
        except Exception:
            warn_line("Mark scheme: graphic extraction failed")

    return _graphics_by_qnum, _graphics_merged.get("questions", [])


# ---------------------------------------------------------------------------
# Step 19 — Parse mark scheme
# ---------------------------------------------------------------------------

def parse_mark_scheme_pages(
    client,
    scheme_model: str,
    scheme_thinking: int | None,
    scheme_max_tokens: int | None,
    *,
    marking_scheme_pdf: Path,
    scaffold_str: str,
    graphics_by_qnum: "dict[str, list] | None" = None,
    artifact_dir: "Path | None",
    fmt=None,
) -> dict:
    """Parse the mark scheme via Gemini (or OpenAI-compatible client) page by page.

    Reads per-page PDFs from step 18's pages dir; falls back to splitting the
    PDF if that dir doesn't exist (allows running step 19 in isolation).
    Attaches ``graphics_by_qnum`` (from step 18) onto matching scheme entries.
    """
    if fmt is None:
        from xscore.scaffold.formats.xml_format import XmlScaffoldFormat
        fmt = XmlScaffoldFormat()

    # Reuse step-18 per-page splits if they're already on disk; otherwise create them.
    _tmp_dir: Path | None = None
    page_paths: list[Path] = []
    n_pages = 0
    if artifact_dir is not None:
        pages_dir = artifact_mark_scheme_pages_dir(artifact_dir)
        if pages_dir.is_dir():
            page_paths = sorted(
                pages_dir.glob("page_*.pdf"),
                key=lambda p: int(re.search(r"page_(\d+)\.pdf$", p.name).group(1)),
            )
            n_pages = len(page_paths)
    if not page_paths:
        n_pages, page_paths, _tmp_dir = split_mark_scheme_into_pages(marking_scheme_pdf, artifact_dir)

    # OpenAI-compatible client path requires PNGs — only rasterize when needed.
    _oa_client = None
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}
    page_pngs: dict[int, bytes] = {}
    if not scheme_model.startswith("gemini"):
        _oa_result = make_ai_client(model_env="READ_MARK_SCHEME_MODEL")
        if _oa_result is None:
            raise RuntimeError(f"No API key set for mark scheme model {scheme_model!r}")
        _oa_client, _, _oa_provider, _, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_completion_kwargs(
            _oa_provider, scheme_thinking, scheme_max_tokens
        )
        page_pngs = _rasterize_scheme_pages(marking_scheme_pdf, n_pages)

    page_uris: dict[int, str] = {}
    if _oa_client is None:
        info_line(f"Uploading {n_pages} page(s) …")

        def _upload_page(item: tuple[int, Path]):
            page_num, path = item
            return page_num, _upload_and_poll(client, path, f"scheme p{page_num}")

        with ThreadPoolExecutor(max_workers=min(n_pages, int(os.environ.get("PARSE_SCHEME_WORKERS", "500")))) as pool:
            for page_num, f in pool.map(_upload_page, enumerate(page_paths, 1)):
                page_uris[page_num] = f.uri

    def _call_page(page_num: int) -> dict:
        _input_label = "image" if _oa_client is not None else "PDF"
        user_msg = fmt.build_scheme_user_msg(scaffold_str, page_num, n_pages, input_label=_input_label)
        _t0 = time.perf_counter()
        thinking_text = ""
        try:
            if _oa_client is not None:
                # OpenAI-compatible path (Qwen, Grok, etc.)
                # PNG from page_pngs: 300 DPI lossless — preserves fine mark-scheme text
                b64 = _base64.b64encode(page_pngs[page_num]).decode()
                kwargs: dict = dict(
                    model=scheme_model,
                    messages=[
                        {"role": "system", "content": fmt.system_scheme_prompt()},
                        {"role": "user", "content": [
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/png;base64,{b64}"}},
                            {"type": "text", "text": user_msg},
                        ]},
                    ],
                )
                kwargs.update(_oa_thinking_kw)
                kwargs.update(fmt.scheme_oa_extra_kwargs())
                if _oa_use_stream:
                    _th: list[str] = []
                    stream = _oa_client.chat.completions.create(**kwargs, stream=True)
                    raw = collect_streamed_response(stream, thinking_out=_th)
                    thinking_text = "".join(_th)
                else:
                    resp = _oa_client.chat.completions.create(**kwargs)
                    raw = resp.choices[0].message.content or ""
                    thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
                ok_line(f"p{page_num}  ·  {format_duration(time.perf_counter() - _t0)}")
            else:
                # Gemini native path
                resp = client.models.generate_content(
                    model=scheme_model,
                    contents=[
                        gai_types.Part.from_uri(
                            file_uri=page_uris[page_num], mime_type="application/pdf"
                        ),
                        gai_types.Part.from_text(text=user_msg),
                    ],
                    config=_make_gen_config(
                        scheme_thinking, fmt.system_scheme_prompt(),
                        pydantic_schema=fmt.pydantic_schema_scheme(),
                        max_tokens=scheme_max_tokens,
                    ),
                )
                ok_line(f"p{page_num}  ·  {format_duration(time.perf_counter() - _t0)}")
                raw, thinking_text = split_gemini_response(resp)
        except Exception as _exc:
            warn_line(
                f"Mark scheme p{page_num}: API error  ·  "
                f"{format_duration(time.perf_counter() - _t0)}  —  {_exc}"
            )
            return {"questions": []}
        if not raw:
            _reason = "" if _oa_client is not None else f" ({_finish_reason(resp)})"
            warn_line(f"Mark scheme p{page_num}: empty response{_reason}")
        if artifact_dir is not None:
            _prompt_path = artifact_scaffold_prompt_path(artifact_dir, f"mark_scheme_p{page_num}")
            save_prompt(
                _prompt_path,
                model=scheme_model, system=fmt.system_scheme_prompt(),
                messages=[{
                    "role": "user",
                    "content": f"[PDF: {marking_scheme_pdf.name} p{page_num}]\n\n{user_msg}",
                }],
            )
            save_response(_prompt_path, raw or "", thinking=thinking_text)
        return fmt.parse_scheme_response(raw or "")

    with ThreadPoolExecutor(max_workers=min(n_pages, int(os.environ.get("PARSE_SCHEME_WORKERS", "500")))) as pool:
        page_results = list(pool.map(_call_page, range(1, n_pages + 1)))

    result = _merge_scheme_results(page_results)

    # Attach graphics positions from step 18 onto matching scheme entries.
    if graphics_by_qnum:
        for _q in result.get("questions", []):
            _key = _norm_qnum(_q["number"])
            if _key in graphics_by_qnum:
                _q["graphics"] = graphics_by_qnum[_key]

    # Save merged artifacts under step 19's folder.
    if artifact_dir is not None:
        try:
            from xscore.scaffold.scaffold_markdown import write_mark_scheme_markdown
            _ext = fmt.artifact_ext()
            if _ext == "xml":
                _root = ET.Element("scheme")
                for _q in result.get("questions", []):
                    _qel = ET.SubElement(_root, "question")
                    _qel.set("number", _q["number"])
                    _qel.set("correct_answer", _q.get("correct_answer") or "")
                    for _c in (_q.get("mark_scheme") or []):
                        _cel = ET.SubElement(_qel, "criterion")
                        _cel.set("mark", _c.get("mark", ""))
                        _cel.text = _c.get("criterion", "")
                ET.indent(_root)
                _out_str = ET.tostring(_root, encoding="unicode", xml_declaration=False)
            elif _ext == "yaml":
                import yaml as _yaml
                _out_str = _yaml.dump(result, allow_unicode=True, default_flow_style=False)
            else:
                _out_str = json.dumps(result, ensure_ascii=False, indent=2)
            p = artifact_mark_scheme_path(artifact_dir, fmt=_ext)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_out_str, encoding="utf-8")
            write_mark_scheme_markdown(artifact_dir, result.get("questions", []))
        except Exception:
            pass

    if _tmp_dir is not None:
        import shutil
        shutil.rmtree(_tmp_dir, ignore_errors=True)
    return result

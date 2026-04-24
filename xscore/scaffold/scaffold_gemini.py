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

from eXercise.ai_client import build_thinking_kwargs, collect_streamed_response, make_ai_client
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


_THINKING_MAP = {"off": 0, "low": 1024, "high": 8192}


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _upload_and_poll(client, path: Path, label: str):
    """Upload *path* to the Gemini Files API, poll until ACTIVE, return the file object."""
    f = client.files.upload(file=path)
    for _ in range(120):  # up to 6 minutes at 3 s intervals
        if getattr(f.state, "name", str(f.state)) != "PROCESSING":
            break
        time.sleep(3)
        f = client.files.get(name=f.name)
    else:
        raise TimeoutError(f"Gemini file upload timed out after 6 min ({label}): {f.name}")
    state = getattr(f.state, "name", str(f.state))
    if state == "FAILED":
        raise RuntimeError(f"Gemini file processing failed ({label}): {f.name}")
    return f


def _extract_text(resp) -> str:
    """Return resp.text, tolerating None and empty-candidates responses."""
    try:
        return resp.text or ""
    except Exception:
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
    effort: str | None, system: str,
    schema: dict | None = None,
    pydantic_schema=None,
) -> "gai_types.GenerateContentConfig":
    cfg: dict = {"max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS}
    if pydantic_schema is not None:
        cfg["response_mime_type"] = "application/json"
        cfg["response_schema"] = pydantic_schema
    elif schema is not None:
        cfg["response_mime_type"] = "application/json"
        cfg["response_json_schema"] = schema
    if effort in _THINKING_MAP:
        cfg["thinking_config"] = gai_types.ThinkingConfig(
            thinking_budget=_THINKING_MAP[effort],
            include_thoughts=False,
        )
    return gai_types.GenerateContentConfig(system_instruction=system, **cfg)


# ---------------------------------------------------------------------------
# Exam extraction
# ---------------------------------------------------------------------------

def _do_exam_call(
    client,
    exam_model: str,
    exam_effort: str | None,
    *,
    actual_exam_pdf: Path,
    layout_result,
    split_pdf_path: "Path | None",
    n_split_pages: int,
    artifact_dir: "Path | None",
    fmt=None,
    step_offset: int = 0,
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
        _oa_client, _, _oa_provider, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_thinking_kwargs(_oa_provider, exam_effort or "off")

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

    def _make_exam_call(label: str) -> str:
        _t0 = time.perf_counter()
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
                stream = _oa_client.chat.completions.create(**kwargs, stream=True)
                raw = collect_streamed_response(stream)
            else:
                resp = _oa_client.chat.completions.create(**kwargs)
                raw = resp.choices[0].message.content or ""
        else:
            resp = client.models.generate_content(
                model=exam_model,
                contents=[
                    gai_types.Part.from_uri(file_uri=exam_file.uri, mime_type="application/pdf"),
                    gai_types.Part.from_text(text=user_exam),
                ],
                config=_make_gen_config(exam_effort, fmt.system_exam_prompt(), pydantic_schema=fmt.pydantic_schema_exam()),
            )
            raw = _extract_text(resp)
        api_latency_line(time.perf_counter() - _t0, label=label)
        return raw

    try:
        raw_exam = _make_exam_call("exam")
        if not raw_exam:
            warn_line("Exam API: empty response — retrying once …")
            raw_exam = _make_exam_call("exam retry")
            if not raw_exam:
                if artifact_dir is not None:
                    try:
                        p = artifact_exam_questions_raw_path(artifact_dir, fmt=fmt.artifact_ext())
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text("# empty response after retry", encoding="utf-8")
                    except OSError:
                        pass
                raise RuntimeError(f"Exam response empty after retry — {exam_model}")
        if artifact_dir is not None:
            save_prompt(
                artifact_scaffold_prompt_path(artifact_dir, "exam_questions", step_offset),
                model=exam_model, system=fmt.system_exam_prompt(),
                messages=[{
                    "role": "user",
                    "content": f"[PDF: {actual_exam_pdf.name}]\n\n{user_exam}",
                }],
            )
            try:
                p = artifact_exam_questions_raw_path(artifact_dir, fmt=fmt.artifact_ext())
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(raw_exam, encoding="utf-8")
            except OSError:
                pass
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
# Mark scheme extraction
# ---------------------------------------------------------------------------

def _do_scheme_call(
    client,
    scheme_model: str,
    scheme_effort: str | None,
    *,
    marking_scheme_pdf: Path,
    scaffold_xml: str = "",
    scaffold_str: str = "",
    artifact_dir: "Path | None",
    fmt=None,
    step_offset: int = 0,
) -> dict:
    # Accept both old (scaffold_xml) and new (scaffold_str) param names for compatibility.
    if not scaffold_str:
        scaffold_str = scaffold_xml
    if fmt is None:
        from xscore.scaffold.formats.xml_format import XmlScaffoldFormat
        fmt = XmlScaffoldFormat()
    import fitz

    # 1. Extract single-page PDFs from the mark scheme
    _tmp_dir: Path | None = None
    if artifact_dir is not None:
        pages_dir = artifact_mark_scheme_pages_dir(artifact_dir, step_offset)
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

    # 1b. Rasterize pages to PNG for graphics detection
    _gfx_dpi = int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300"))
    page_pngs: dict[int, bytes] = {}
    with fitz.open(str(marking_scheme_pdf)) as _doc_r:
        for _i in range(n_pages):
            pix = _doc_r[_i].get_pixmap(dpi=_gfx_dpi)
            page_pngs[_i + 1] = pix.tobytes("png")

    # 2. Detect provider; for non-Gemini use OpenAI-compatible client with base64 PNG
    _oa_client = None
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}
    if not scheme_model.startswith("gemini"):
        _oa_result = make_ai_client(model_env="READ_MARK_SCHEME_MODEL")
        if _oa_result is None:
            raise RuntimeError(f"No API key set for mark scheme model {scheme_model!r}")
        _oa_client, _, _oa_provider, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_thinking_kwargs(_oa_provider, scheme_effort or "off")

    # 3. Upload all pages in parallel (Gemini only; non-Gemini uses base64 PNG from page_pngs)
    page_uris: dict[int, str] = {}
    if _oa_client is None:
        info_line(f"Mark scheme: uploading {n_pages} page(s) …")

        def _upload_page(item: tuple[int, Path]):
            page_num, path = item
            return page_num, _upload_and_poll(client, path, f"scheme p{page_num}")

        with ThreadPoolExecutor(max_workers=n_pages) as pool:
            for page_num, f in pool.map(_upload_page, enumerate(page_paths, 1)):
                page_uris[page_num] = f.uri
    else:
        info_line(f"Mark scheme: parsing {n_pages} page(s) via {scheme_model} …")

    # 4. Per-page API calls in parallel

    def _call_page(page_num: int) -> dict:
        _input_label = "image" if _oa_client is not None else "PDF"
        user_msg = fmt.build_scheme_user_msg(scaffold_str, page_num, n_pages, input_label=_input_label)
        _t0 = time.perf_counter()
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
                    stream = _oa_client.chat.completions.create(**kwargs, stream=True)
                    raw = collect_streamed_response(stream)
                else:
                    resp = _oa_client.chat.completions.create(**kwargs)
                    raw = resp.choices[0].message.content or ""
                ok_line(f"{format_duration(time.perf_counter() - _t0)}  (mark scheme p{page_num})")
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
                        scheme_effort, fmt.system_scheme_prompt(),
                        pydantic_schema=fmt.pydantic_schema_scheme(),
                    ),
                )
                ok_line(f"{format_duration(time.perf_counter() - _t0)}  (mark scheme p{page_num})")
                raw = _extract_text(resp)
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
            _prompt_path = artifact_scaffold_prompt_path(artifact_dir, f"mark_scheme_p{page_num}", step_offset)
            save_prompt(
                _prompt_path,
                model=scheme_model, system=fmt.system_scheme_prompt(),
                messages=[{
                    "role": "user",
                    "content": f"[PDF: {marking_scheme_pdf.name} p{page_num}]\n\n{user_msg}",
                }],
            )
            save_response(_prompt_path, raw or "")
        return fmt.parse_scheme_response(raw or "")

    # Build graphics detection system message (schema embedded once, shared across threads)
    _gfx_schema_str = json.dumps(_SCHEME_GRAPHICS_JSON_SCHEMA, indent=2)
    _gfx_system = (
        "You are a graphic-detection assistant for Cambridge IGCSE mark schemes. "
        "Respond ONLY with valid JSON matching this schema:\n" + _gfx_schema_str + "\n\n"
        "Return bounding boxes as [x_min, y_min, x_max, y_max] with integer "
        "coordinates on a 0\u20131000 scale (0=top-left, 1000=bottom-right of the image)."
    )

    # Extract canonical question numbers from scaffold so graphics detector returns exact matches
    _all_qnums = fmt.extract_question_numbers(scaffold_str)
    _qnum_hint = ", ".join(f'"{n}"' for n in _all_qnums)

    def _detect_graphics_page(page_num: int) -> dict:  # no-op fallback
        return {"questions": []}

    _gfx_client_result = make_ai_client(model_env="DETECT_SCHEME_GRAPHICS_MODEL")
    if _gfx_client_result is None:
        warn_line("DETECT_SCHEME_GRAPHICS_MODEL: API key missing — graphics detection skipped")
    else:
        _det_client, _det_model, _det_provider, _ = _gfx_client_result
        _, _det_thinking_kw = build_thinking_kwargs(_det_provider, "off")
        info_line(f"Mark scheme: detecting graphics ({_det_model}) …")

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
            except Exception as _exc:
                warn_line(
                    f"Scheme graphics p{page_num}: API error  ·  "
                    f"{format_duration(time.perf_counter() - _t0)}  —  {_exc}"
                )
                return {"questions": []}
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                ok_line(f"{format_duration(time.perf_counter() - _t0)}  (scheme graphics p{page_num})")
                return {"questions": []}
            if artifact_dir is not None:
                _prompt_path = artifact_scaffold_prompt_path(
                    artifact_dir, f"mark_scheme_graphics_detect_p{page_num}", step_offset
                )
                save_prompt(
                    _prompt_path, model=_det_model, system=_gfx_system,
                    messages=[{"role": "user", "content": f"[PNG: p{page_num}]\n\n{_user_msg}"}],
                )
                save_response(_prompt_path, raw)
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
                f"  ·  q{', q'.join(questions_map.keys())}"
                if questions_map else ""
            )
            ok_line(
                f"{format_duration(time.perf_counter() - _t0)}  "
                f"(scheme graphics p{page_num}){_qnums_str}"
            )
            return {
                "questions": [
                    {"number": qnum, "correct_answer": None, "mark_scheme": [], "graphics": gfx}
                    for qnum, gfx in questions_map.items()
                ]
            }

    with ThreadPoolExecutor(max_workers=n_pages * 2) as pool:
        _scheme_futs   = [pool.submit(_call_page,            p) for p in range(1, n_pages + 1)]
        _graphics_futs = [pool.submit(_detect_graphics_page, p) for p in range(1, n_pages + 1)]
        page_results          = [f.result() for f in _scheme_futs]
        graphics_page_results = [f.result() for f in _graphics_futs]

    # 4. Merge scheme results and graphics results
    result = _merge_scheme_results(page_results)
    _graphics_merged = _merge_scheme_results(graphics_page_results)

    def _norm_qnum(s: str) -> str:
        return re.sub(r"[()]", "", s)

    _graphics_by_qnum = {
        _norm_qnum(q["number"]): q["graphics"]
        for q in _graphics_merged.get("questions", [])
        if q.get("graphics")
    }
    for _q in result.get("questions", []):
        _key = _norm_qnum(_q["number"])
        if _key in _graphics_by_qnum:
            _q["graphics"] = _graphics_by_qnum[_key]
    ok_line(
        f"Mark scheme: merged {n_pages} page(s)  ·  "
        f"{len(result['questions'])} question(s)"
    )

    # 5. Save merged artifacts
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

    # 6. Graphics extraction
    if artifact_dir is not None and marking_scheme_pdf is not None:
        _graphics_dpi = int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300"))
        _graphics_margin = float(os.environ.get("SCHEME_GRAPHICS_MARGIN", "0.01"))
        _n_graphics = sum(len(q.get("graphics") or []) for q in result.get("questions", []))
        if _n_graphics:
            try:
                _extract_scheme_graphics(
                    result.get("questions", []),
                    marking_scheme_pdf,
                    artifact_mark_scheme_graphics_dir(artifact_dir, step_offset),
                    dpi=_graphics_dpi,
                    margin=_graphics_margin,
                )
                ok_line(f"Mark scheme: {_n_graphics} graphic(s) extracted")
            except Exception:
                warn_line("Mark scheme: graphic extraction failed")

    if _tmp_dir is not None:
        import shutil
        shutil.rmtree(_tmp_dir, ignore_errors=True)
    return result

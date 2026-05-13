"""Step detect_mark_scheme_graphics — Detect graphics (figures, diagrams) on each mark scheme page.

Per-page parallel vision call. OpenAI-compatible only (no Gemini path today).
Always runs; the model is configured via ``DETECT_SCHEME_GRAPHICS_MODEL``.
"""

from __future__ import annotations

import base64 as _base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eXercise.ai_client import build_completion_kwargs, make_ai_client
from eXercise.api_retry import retry_api_call
from xscore.scaffold.scaffold_prompts import _USER_GRAPHICS
from xscore.scaffold.scaffold_qtree import _norm_qnum
from xscore.scaffold.scaffold_scheme_pdf import (
    _rasterize_scheme_pages, split_mark_scheme_into_pages,
)
from xscore.scaffold.scaffold_xml import _merge_scheme_results
from xscore.shared.exam_paths import (
    artifact_mark_scheme_graphics_dir,
    artifact_mark_scheme_graphics_yaml_path,
    artifact_scaffold_prompt_path,
)
from xscore.shared.prompt_logger import save_output_data, save_prompt, save_response
from xscore.shared.terminal_ui import (
    format_duration, info_line, ok_line, warn_line,
)


def _question_number_parents(leaves: list[str]) -> set[str]:
    """For each leaf qnum like '2(b)(i)', emit every non-empty prefix
    parent — e.g. {'2(b)', '2'}. Leaves themselves are NOT included."""
    parents: set[str] = set()
    for leaf in leaves:
        cur = leaf
        while True:
            stripped = re.sub(r"\s*\([^()]*\)\s*$", "", cur).rstrip()
            if not stripped or stripped == cur:
                break
            parents.add(stripped)
            cur = stripped
    return parents


def detect_scheme_graphics(
    marking_scheme_pdf: Path,
    scaffold_str: str,
    *,
    artifact_dir: "Path | None",
    fmt=None,
) -> tuple[dict, list[dict]]:
    """Detect graphics in the mark scheme via vision API.

    Splits the mark scheme into per-page PDFs (always — needed by steps 23 and 24 too)
    then runs graphics detection on each rasterized page in parallel.

    Returns ``(graphics_by_qnum, graphics_questions)`` where:
      * ``graphics_by_qnum`` is ``{normalised_qnum: [{page, x0, y0, x1, y1}, ...]}``
        — empty when no graphics found.
      * ``graphics_questions`` is the per-question list used by downstream artifact
        extraction — ``[]`` when no graphics found.

    Side effects: writes per-page PDFs to detect_mark_scheme_graphics's pages dir, plus the graphics
    YAML catalog and extracted graphic images when graphics are detected.
    """
    if fmt is None:
        from xscore.scaffold.formats.base import ScaffoldFormat
        fmt = ScaffoldFormat()

    n_pages, page_paths, _tmp_dir = split_mark_scheme_into_pages(marking_scheme_pdf, artifact_dir)

    _gfx_client_result = make_ai_client(model_env="DETECT_SCHEME_GRAPHICS_MODEL")
    if _gfx_client_result is None:
        if _tmp_dir is not None:
            import shutil
            shutil.rmtree(_tmp_dir, ignore_errors=True)
        raise RuntimeError(
            "DETECT_SCHEME_GRAPHICS_MODEL client could not be created — "
            "check DASHSCOPE_API_KEY / GEMINI_API_KEY in .env"
        )

    page_pngs = _rasterize_scheme_pages(marking_scheme_pdf, n_pages)

    from xscore.prompts.loader import load_prompt as _load_prompt
    _, _gfx_system = _load_prompt(
        "detect_mark_scheme_graphics", section="system",
    )

    _all_qnums = fmt.extract_question_numbers(scaffold_str)
    _leaf_qnums = set(_all_qnums)
    _parent_qnums = _question_number_parents(_all_qnums) - _leaf_qnums
    _qnum_hint = ", ".join(f'"{n}"' for n in [*_all_qnums, *sorted(_parent_qnums)])

    _det_client, _det_model, _det_provider, _det_thinking, _det_max_tok = _gfx_client_result
    _, _det_thinking_kw = build_completion_kwargs(
        _det_provider, _det_thinking, _det_max_tok
    )
    from eXercise.ai_client import make_request_timeout  # noqa: PLC0415
    _det_timeout = make_request_timeout("standard")
    _det_timeout_kw: dict = {"timeout": _det_timeout} if _det_timeout is not None else {}
    info_line(f"Detecting graphics ({_det_model}) …")

    def _detect_graphics_page(page_num: int) -> dict:
        b64 = _base64.b64encode(page_pngs[page_num]).decode()
        _t0 = time.perf_counter()
        _hint = (
            f"Valid question numbers in this mark scheme: {_qnum_hint}\n"
            "Prefer the most specific match; a broader parent is acceptable when the sub-part is unclear.\n\n"
        ) if _qnum_hint else ""
        _user_msg = _hint + _USER_GRAPHICS

        def _do_call() -> tuple[str, str]:
            _resp = _det_client.chat.completions.create(
                model=_det_model,
                messages=[
                    {"role": "system", "content": _gfx_system},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": _user_msg},
                    ]},
                ],
                **_det_thinking_kw,
                **_det_timeout_kw,
            )
            return (
                _resp.choices[0].message.content or "graphics: []",
                getattr(_resp.choices[0].message, "reasoning_content", "") or "",
            )

        try:
            raw, _thinking_text = retry_api_call(
                _do_call, label=f"Scheme graphics p{page_num}",
            )
        except Exception as _exc:
            warn_line(
                f"Scheme graphics p{page_num}: giving up after retries  ·  "
                f"{format_duration(time.perf_counter() - _t0)}  —  {_exc}"
            )
            return {"questions": []}
        graphics = fmt.parse_graphics_response(raw)
        if artifact_dir is not None:
            from xscore.shared.prompt_logger import attachment_part
            _prompt_path = artifact_scaffold_prompt_path(
                artifact_dir, f"mark_scheme_graphics_detect_p{page_num}"
            )
            save_prompt(
                _prompt_path, model=_det_model,
                messages=[
                    {"role": "system", "content": _gfx_system},
                    {"role": "user", "content": [
                        attachment_part(page_pngs[page_num], "image/png"),
                        {"type": "text", "text": _user_msg},
                    ]},
                ],
            )
            save_response(_prompt_path, raw, thinking=_thinking_text)
            save_output_data(_prompt_path, raw, ext="yaml")
        questions_map: dict[str, list] = {}
        for g in graphics:
            qnum = g.get("question_number", "").strip()
            bbox = g.get("bbox") or []
            if not qnum or len(bbox) != 4:
                continue
            if qnum not in _leaf_qnums and qnum in _parent_qnums:
                warn_line(
                    f"Scheme graphics p{page_num}: graphic assigned to parent {qnum!r} "
                    "— sub-part not determined"
                )
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
    # Normalise question numbers to the canonical form used everywhere else
    # in the pipeline (`7a`, not `7(a)`). The AI's raw output keeps parens
    # because that's how the mark scheme PDF labels questions; downstream
    # code expects the normalised form.
    for q in _graphics_merged.get("questions", []):
        if q.get("number") is not None:
            q["number"] = _norm_qnum(q["number"])
    _graphics_by_qnum = {
        q["number"]: q["graphics"]
        for q in _graphics_merged.get("questions", [])
        if q.get("graphics")
    }
    _n_graphics = sum(len(g) for g in _graphics_by_qnum.values())

    if artifact_dir is not None:
        try:
            import yaml as _yaml
            p = artifact_mark_scheme_graphics_yaml_path(artifact_dir)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                _yaml.safe_dump(
                    _graphics_merged,
                    allow_unicode=True, default_flow_style=False, sort_keys=False,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            warn_line(f"Could not save mark-scheme graphics YAML: {e}")

    if artifact_dir is not None and _n_graphics:
        from xscore.scaffold.scaffold_xml import _extract_scheme_graphics
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

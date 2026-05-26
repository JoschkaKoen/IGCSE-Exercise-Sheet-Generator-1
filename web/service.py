# -*- coding: utf-8 -*-
"""Run the same NL → extraction flow as the CLI (for the web worker thread)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from itertools import groupby
from pathlib import Path
from typing import Any

from eXercise.natural_language import resolve_natural_language
from eXercise.output_paths import resolve_output_path_fresh, set_run_command
from eXercise.pipeline import run_extraction_jobs

_LIBRARY_CACHE: dict | None = None
_LIBRARY_CACHE_LOCK = threading.Lock()


def invalidate_library_cache() -> None:
    """Discard the cached library index; the next call to list_library_pdfs() rebuilds it."""
    global _LIBRARY_CACHE
    with _LIBRARY_CACHE_LOCK:
        _LIBRARY_CACHE = None

def run_nl_prompt(
    prompt: str,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[Path, Path | None, Path | None, Path | None, Path | None, Path | None, Path | None, dict[str, Any]]:
    """
    Resolve natural language, run extraction jobs, return main PDF, optional answers PDF,
    optional pdfjam siblings (4-up / 2-up landscape) when those files exist, and an
    ``overview`` dict for the web UI (papers + exercise anchors for in-PDF navigation).

    When ``on_progress`` is set, stdout/stderr are redirected for the *entire* pipeline
    (including the NL model call) so that streaming thinking tokens reach the web UI live.

    Answers PDF is returned only when that file exists on disk (mark scheme path produced output).
    """

    def emit(msg: str) -> None:
        # print() goes through the stdout capture when run_with_last_log_line is active;
        # the direct on_progress call is a fast path for callers without capture.
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    set_run_command(prompt)

    overview_holder: list[dict[str, Any]] = []

    from eXercise.cost_recorder import collect_run_cost

    def full_pipeline() -> str:
        """NL resolution + PDF extraction — all under stdout capture when web UI is active."""
        emit("Resolving natural-language request…")
        with rec.phase("Resolve instruction"):
            exam_root, data = resolve_natural_language(prompt, on_progress=on_progress)
        emit(f"Exam folder: {exam_root} ({data.get('exam', '')})")
        emit(f"Papers in this run: {len(data['extractions'])}")
        jobs = []
        for ex in data["extractions"]:
            jobs.append(
                {
                    "input_pdf": str(exam_root / ex["input_pdf"]),
                    "questions": ex["questions"],
                    "mark_scheme_pdf": str(exam_root / ex["mark_scheme_pdf"])
                    if ex.get("mark_scheme_pdf")
                    else None,
                }
            )
        emit("Preparing output and extracting PDFs…")
        output_pdf = resolve_output_path_fresh(data["output_pdf"])
        output_str = str(output_pdf)
        # run_extraction_jobs picks up the active recorder via current_recorder()
        # and wraps its own phases ("QP extraction", "MS prep", "AI explanations",
        # ...) plus writes ai_costs/cost.{json,md} at the end.
        overview = run_extraction_jobs(jobs, output_str, exam_key=data.get("exam"), run_ranking=False)
        overview_holder.clear()
        overview_holder.append(overview)
        return output_str

    with collect_run_cost() as rec:
        if on_progress:
            from .process_log import run_with_last_log_line

            output_str = run_with_last_log_line(full_pipeline, on_progress)
        else:
            output_str = full_pipeline()

    out_path = Path(output_str)
    answers_path = out_path.parent / f"{out_path.stem}_answers{out_path.suffix}"
    four_up = out_path.parent / f"{out_path.stem}_4up{out_path.suffix}"
    two_up = out_path.parent / f"{out_path.stem}_2up{out_path.suffix}"
    ans_four_up = out_path.parent / f"{out_path.stem}_answers_4up{out_path.suffix}"
    ans_two_up = out_path.parent / f"{out_path.stem}_answers_2up{out_path.suffix}"
    ranking_path = out_path.parent / f"{out_path.stem}_ranking{out_path.suffix}"
    ans: Path | None = answers_path if answers_path.is_file() else None
    u4: Path | None = four_up if four_up.is_file() else None
    u2: Path | None = two_up if two_up.is_file() else None
    a4: Path | None = ans_four_up if ans_four_up.is_file() else None
    a2: Path | None = ans_two_up if ans_two_up.is_file() else None
    ranking: Path | None = ranking_path if ranking_path.is_file() else None
    overview = overview_holder[0] if overview_holder else {"papers": [], "anchors": []}
    return out_path, ans, u4, u2, a4, a2, ranking, overview


def run_nl_prompt_logged(
    prompt: str,
    on_line: Callable[[str], None],
) -> tuple[Path, Path | None, Path | None, Path | None, Path | None, Path | None, Path | None, dict[str, Any]]:
    """Web worker entry: same as ``run_nl_prompt`` with live progress lines."""
    return run_nl_prompt(prompt, on_progress=on_line)


def _library_grouped_blocks(subject_key: str, names: list[str]) -> list[dict[str, Any]]:
    """
    Group sorted filenames by year (descending in ``names`` order) and session M/W/S.

    ``itertools.groupby`` preserves order; Jinja's ``groupby`` filter re-sorts keys and must not be used.
    """
    from urllib.parse import quote

    from eXercise.labels import (
        library_pdf_display_name,
        library_pdf_group_meta,
    )

    rows: list[dict[str, str]] = []
    for n in names:
        meta = library_pdf_group_meta(n)
        rows.append(
            {
                "name": n,
                "display_name": library_pdf_display_name(n),
                "download_url": f"/api/library/{subject_key}/{quote(n, safe='')}",
                **meta,
            }
        )
    blocks: list[dict[str, Any]] = []
    for _year_key, year_iter in groupby(rows, key=lambda r: r["group_year"]):
        year_rows = list(year_iter)
        sessions: list[dict[str, Any]] = []
        for _sess_key, sess_iter in groupby(year_rows, key=lambda r: r["group_session"]):
            sess_rows = list(sess_iter)
            sessions.append(
                {
                    "session": sess_rows[0]["group_session"],
                    "session_heading": sess_rows[0]["session_heading"],
                    "session_title": sess_rows[0]["session_title"],
                    "rows": sess_rows,
                }
            )
        blocks.append({"year": year_rows[0]["group_year"], "sessions": sessions})
    return blocks


def _newest_syllabus_block(subject_key: str) -> dict[str, Any] | None:
    """Pinned top-of-list block holding the newest syllabus PDF for a subject."""
    from urllib.parse import quote

    from eXercise.config import SYLLABI_DIR, SYLLABUS_CODE_BY_KEY
    from web.syllabus_topics import parse_year_range

    code = SYLLABUS_CODE_BY_KEY.get(subject_key)
    if not code or not SYLLABI_DIR.is_dir():
        return None
    # Prefer full Syllabus Documents; fall back to Updates when no Document exists.
    # Recursive glob — PDFs may live in syllabi/igcse/ or syllabi/a_level/ subfolders.
    docs = list(SYLLABI_DIR.glob(f"**/{code} *Syllabus Document.pdf"))
    pdfs = docs or list(SYLLABI_DIR.glob(f"**/{code} *Syllabus*.pdf"))
    if not pdfs:
        return None

    def end_year(p: Path) -> int:
        r = parse_year_range(p)
        return r[1] if r else 0

    newest = max(pdfs, key=end_year)
    return {
        "year": "_syllabus",
        "sessions": [
            {
                "session": "_",
                "session_heading": "",
                "session_title": "",
                "rows": [
                    {
                        "name": newest.name,
                        "display_name": "Syllabus",
                        "download_url": f"/api/library/{subject_key}/syllabus/{quote(newest.name, safe='')}",
                        "group_year": "_syllabus",
                        "group_session": "_",
                        "paper_kind": "syllabus",
                        "session_heading": "",
                        "session_title": "",
                    }
                ],
            }
        ],
    }


def list_library_pdfs() -> dict[str, list[dict[str, Any]]]:
    """Scan bundled exam dirs; nested year → session → file rows for the library page."""
    global _LIBRARY_CACHE
    with _LIBRARY_CACHE_LOCK:
        if _LIBRARY_CACHE is not None:
            return _LIBRARY_CACHE

        from eXercise.config import EXAM_ROOT_BY_KEY
        from eXercise.labels import library_pdf_sort_key

        out: dict[str, list[dict[str, Any]]] = {}
        for key, root in EXAM_ROOT_BY_KEY.items():
            blocks: list[dict[str, Any]] = []
            syllabus = _newest_syllabus_block(key)
            if syllabus is not None:
                blocks.append(syllabus)
            if root.is_dir():
                names = sorted((p.name for p in root.glob("*.pdf")), key=library_pdf_sort_key)
                blocks.extend(_library_grouped_blocks(key, names))
            out[key] = blocks
        _LIBRARY_CACHE = out
        return out

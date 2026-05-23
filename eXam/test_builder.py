"""Test builder.

Thin wrapper around ``resolve_natural_language`` + per-paper enrichment +
per-question helper pregeneration. Two entry points:

- ``build_test(prompt, ...)`` — CLI synchronous: resolves, enriches, generates
  helpers, returns the test id. Suitable for verification.

- ``run_build(test_id)`` — async-ish: reads the row out of SQLite (already
  created in ``status='building'``), runs the same work, updates
  ``status`` / ``build_progress`` / ``build_error``. Submitted to a
  module-level ``ThreadPoolExecutor`` by the web POST endpoint.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz

from eXercise.env_load import load_project_env
from eXam.bank import bank_dir_for, ensure_paper_indexed
from eXam.db import connect

# Module-level executor — submitted to by the web POST endpoint.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="eXam-build")
_log_lock = threading.Lock()


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _log(msg: str) -> None:
    with _log_lock:
        print(f"[build] {msg}", flush=True)


def _set_progress(test_id: str, step: str, current: int, total: int, qnum: str | None = None) -> None:
    payload = {"step": step, "current": current, "total": total}
    if qnum is not None:
        payload["qnum"] = qnum
    with connect() as conn:
        conn.execute(
            "UPDATE tests SET build_progress=? WHERE id=?",
            (json.dumps(payload), test_id),
        )


def _expand_questions(input_pdf: Path, questions) -> list[int]:
    if questions == "all":
        from eXercise.config import get_subject_config
        from eXercise.questions import find_question_positions

        doc = fitz.open(input_pdf)
        try:
            positions = find_question_positions(doc, get_subject_config(None))
            return sorted({int(p[0]) for p in positions})
        finally:
            doc.close()
    return [int(q) for q in questions]


def _build_question_records(data: dict, exam_root: Path) -> list[dict]:
    """Flatten resolver output to ``[{question_id, paper_path, ms_path, qnum}]``."""
    subject = data["exam"]
    records: list[dict] = []
    for ext in data["extractions"]:
        paper_path = exam_root / ext["input_pdf"]
        ms_name = ext.get("mark_scheme_pdf")
        ms_path = (exam_root / ms_name) if ms_name else None
        qnums = _expand_questions(paper_path, ext["questions"])
        for q in qnums:
            qid = f"{subject}::{Path(ext['input_pdf']).stem}::{q}"
            records.append(
                {
                    "question_id": qid,
                    "paper_path": str(paper_path),
                    "ms_path": str(ms_path) if ms_path else None,
                    "qnum": q,
                }
            )
    return records


def _enrich_papers(records: list[dict], subject: str, test_id: str) -> None:
    # Unique (paper, ms) pairs.
    from eXam.cost_tracker import track

    seen: set[tuple[str, str | None]] = set()
    unique: list[tuple[str, str | None]] = []
    for r in records:
        key = (r["paper_path"], r["ms_path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    for i, (paper, ms) in enumerate(unique, start=1):
        _set_progress(test_id, "enrich_paper", i, len(unique), qnum=Path(paper).stem)
        _log(f"enrich paper {i}/{len(unique)}: {Path(paper).name}")
        # xscore scaffold AI calls (detect/fill/scheme) made by ensure_paper_indexed
        # land under this phase in the ai_calls log.
        with track("build_enrich_paper", test_id=test_id):
            ensure_paper_indexed(Path(paper), Path(ms) if ms else None, subject)


def _pregenerate_helpers(records: list[dict], subject: str, test_id: str) -> None:
    # Imported lazily because Phase D adds eXam.pregenerate.
    try:
        from eXam.pregenerate import pregenerate_for_question
    except ImportError:
        _log("pregenerate not available — skipping (Phase D not landed yet)")
        return
    total = len(records) * 4
    done = 0
    for rec in records:
        for kind in ("hint", "solution", "example", "kb"):
            done += 1
            _set_progress(test_id, f"pregen_{kind}", done, total, qnum=rec["question_id"])
            try:
                pregenerate_for_question(rec, subject, kind, test_id=test_id)
            except Exception as e:  # noqa: BLE001
                _log(f"pregen {kind} failed for {rec['question_id']}: {e}")


def run_build(test_id: str) -> None:
    """Run the build for an already-created ``tests`` row. Updates status/progress.

    Opens a CostRecorder for the whole build (via :func:`eXam.cost_tracker.track`)
    so all AI calls from resolve_natural_language → ensure_paper_indexed →
    pregenerate_for_question are tagged with this ``test_id`` in ``ai_calls``,
    and an aggregate ``cost.json`` + ``cost.md`` lands in
    ``output/eXam/builds/<test_id>/`` on success.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT teacher_prompt, subject, question_ids FROM tests WHERE id=?",
            (test_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown test_id: {test_id}")
    prompt = row["teacher_prompt"]
    subject = row["subject"]
    question_ids = json.loads(row["question_ids"])

    from eXam.cost_tracker import track
    from eXercise.config import PROJECT_ROOT
    from eXercise.cost_recorder import current_recorder
    from eXercise.cost_report import write_cost_report

    try:
        load_project_env()
        # Re-resolve to obtain paper paths (needed for enrichment).
        from eXercise.natural_language import resolve_natural_language

        with track("build", test_id=test_id) as build_rec:
            _set_progress(test_id, "resolve_nl", 0, 1)
            with track("build_resolve_nl", test_id=test_id):
                exam_root, data = resolve_natural_language(prompt)
            records = _build_question_records(data, exam_root)
            # Sanity check: question_ids written by build_test should match what
            # the resolver returns now. If they don't, prefer the freshly-resolved
            # set (rare drift case).
            fresh_ids = [r["question_id"] for r in records]
            if fresh_ids != question_ids:
                with connect() as conn:
                    conn.execute(
                        "UPDATE tests SET question_ids=? WHERE id=?",
                        (json.dumps(fresh_ids), test_id),
                    )

            _enrich_papers(records, subject, test_id)
            _pregenerate_helpers(records, subject, test_id)

            # Write per-build cost artifact while the recorder is still in scope.
            rec = current_recorder()
            if not rec.is_null:
                build_dir = PROJECT_ROOT / "output" / "eXam" / "builds" / test_id
                try:
                    write_cost_report(
                        build_dir,
                        total_usage=rec.total_usage,
                        per_phase_usage=rec.per_phase_usage,
                        per_phase_calls=rec.per_phase_calls,
                        phase_label="Operation",
                    )
                except Exception as e:  # noqa: BLE001 — artifact write must not fail the build
                    _log(f"cost.json write failed: {e}")

        with connect() as conn:
            conn.execute(
                "UPDATE tests SET status='ready', ready_at=?, build_progress=? WHERE id=?",
                (_now(), json.dumps({"step": "done", "current": 1, "total": 1}), test_id),
            )
        _log(f"test {test_id} ready")
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        _log(f"build failed for {test_id}:\n{tb}")
        with connect() as conn:
            conn.execute(
                "UPDATE tests SET status='failed', build_error=? WHERE id=?",
                (tb, test_id),
            )


def build_test(
    teacher_prompt: str,
    *,
    title: str | None = None,
    class_label: str | None = None,
    randomize: bool = False,
    no_helpers: bool = False,
    synchronous: bool = False,
) -> str:
    """Create a ``tests`` row and either submit to the executor or run inline.

    Returns the new test_id. When ``synchronous=True`` (CLI smoke) the build
    runs inline in the current thread and the call returns only after
    completion.
    """
    load_project_env()
    from eXercise.natural_language import resolve_natural_language

    exam_root, data = resolve_natural_language(teacher_prompt)
    subject = data["exam"]
    records = _build_question_records(data, exam_root)
    if not records:
        raise RuntimeError("Resolver returned no questions")
    test_id = uuid.uuid4().hex[:24]
    derived_title = title or (data.get("output_pdf") or "Practice test").rsplit(".", 1)[0]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tests
                (id, title, teacher_prompt, subject, class_label, question_ids,
                 randomize, status, build_progress, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'building', ?, ?)
            """,
            (
                test_id,
                derived_title,
                teacher_prompt,
                subject,
                class_label,
                json.dumps([r["question_id"] for r in records]),
                1 if randomize else 0,
                json.dumps({"step": "queued", "current": 0, "total": 1}),
                _now(),
            ),
        )

    if no_helpers:
        # Skip pregen but still enrich + render snippets so the take page works.
        def _build_no_helpers():
            try:
                _enrich_papers(records, subject, test_id)
                with connect() as conn:
                    conn.execute(
                        "UPDATE tests SET status='ready', ready_at=? WHERE id=?",
                        (_now(), test_id),
                    )
            except Exception:
                tb = traceback.format_exc()
                with connect() as conn:
                    conn.execute(
                        "UPDATE tests SET status='failed', build_error=? WHERE id=?",
                        (tb, test_id),
                    )

        if synchronous:
            _build_no_helpers()
        else:
            _executor.submit(_build_no_helpers)
        return test_id

    if synchronous:
        run_build(test_id)
    else:
        _executor.submit(run_build, test_id)
    return test_id


def _cli() -> int:
    p = argparse.ArgumentParser(prog="eXam.test_builder")
    p.add_argument("prompt", help="natural-language description of the test")
    p.add_argument("--title", default=None)
    p.add_argument("--class-label", default=None)
    p.add_argument("--randomize", action="store_true")
    p.add_argument("--no-helpers", action="store_true")
    args = p.parse_args()
    tid = build_test(
        args.prompt,
        title=args.title,
        class_label=args.class_label,
        randomize=args.randomize,
        no_helpers=args.no_helpers,
        synchronous=True,
    )
    print(f"test_id: {tid}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

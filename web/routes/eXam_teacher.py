# -*- coding: utf-8 -*-
"""eXam teacher-facing routes: dashboard, build, roster, regenerate-helper, export.

Reuses the grade-unlock gate (``esg_grade_auth`` cookie) as the teacher gate.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from eXam.db import connect
from eXam.roster import generate_pin_pdf, import_roster
from eXam.results_export import export_test_xlsx
from eXam.test_builder import build_test
from ..grade_auth import is_grade_unlocked

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

router = APIRouter(prefix="/eXam/teacher", tags=["eXam-teacher"])


def _require_teacher(request: Request) -> None:
    if not is_grade_unlocked(request):
        raise HTTPException(status_code=403, detail="Teacher access required (use the grade unlock).")


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    _require_teacher(request)
    with connect() as conn:
        tests = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, title, subject, class_label, status, created_at, ready_at
                FROM tests
                ORDER BY created_at DESC
                """
            )
        ]
        students = conn.execute("SELECT count(*) AS c FROM students").fetchone()["c"]
    return TEMPLATES.TemplateResponse(
        "eXam/teacher_dashboard.html",
        {"request": request, "tests": tests, "students": students},
    )


@router.get("/build", response_class=HTMLResponse)
async def build_page(request: Request):
    _require_teacher(request)
    return TEMPLATES.TemplateResponse(
        "eXam/teacher_build.html", {"request": request}
    )


class BuildBody(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000)
    title: str | None = None
    class_label: str | None = None
    randomize: bool = False
    no_helpers: bool = False


@router.post("/build", response_class=JSONResponse)
async def build_submit(body: BuildBody, request: Request):
    _require_teacher(request)
    try:
        test_id = build_test(
            body.prompt,
            title=body.title or None,
            class_label=body.class_label or None,
            randomize=body.randomize,
            no_helpers=body.no_helpers,
            synchronous=False,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "test_id": test_id}


@router.get("/builds/{test_id}", response_class=JSONResponse)
async def build_status(test_id: str, request: Request):
    _require_teacher(request)
    with connect() as conn:
        row = conn.execute(
            "SELECT status, build_progress, build_error, ready_at FROM tests WHERE id=?",
            (test_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown test")
    progress = json.loads(row["build_progress"]) if row["build_progress"] else None
    return {
        "status": row["status"],
        "progress": progress,
        "error": row["build_error"],
        "ready_at": row["ready_at"],
    }


@router.get("/roster", response_class=HTMLResponse)
async def roster_page(request: Request):
    _require_teacher(request)
    with connect() as conn:
        students = [
            dict(r)
            for r in conn.execute(
                "SELECT name, class_label FROM students ORDER BY class_label, name"
            )
        ]
    return TEMPLATES.TemplateResponse(
        "eXam/teacher_roster.html", {"request": request, "students": students}
    )


@router.post("/roster/import", response_class=JSONResponse)
async def roster_import(
    request: Request,
    file: UploadFile = File(...),
    regenerate_pins: bool = Form(False),
):
    _require_teacher(request)
    data = await file.read()
    try:
        result = import_roster(data, regenerate_pins=regenerate_pins)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    # Build a PINs PDF for inserted + regenerated rows.
    pin_rows = result["inserted"] + [r for r in result["updated"] if r.get("pin")]
    pin_pdf_path = None
    if pin_rows:
        from eXercise.config import PROJECT_ROOT
        pin_pdf_path = PROJECT_ROOT / "output" / "eXam" / f"pins_{file.filename or 'roster'}.pdf"
        generate_pin_pdf(pin_rows, pin_pdf_path)
    return {
        "ok": True,
        "summary": {
            "inserted": len(result["inserted"]),
            "updated": len(result["updated"]),
            "untouched": len(result["untouched"]),
        },
        "pin_pdf": str(pin_pdf_path) if pin_pdf_path else None,
    }


@router.get("/roster/pins.pdf")
async def roster_pins_pdf(request: Request, name: str):
    _require_teacher(request)
    from eXercise.config import PROJECT_ROOT
    p = PROJECT_ROOT / "output" / "eXam" / name
    if not p.exists() or not p.name.startswith("pins_"):
        raise HTTPException(status_code=404)
    return Response(
        p.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
    )


@router.get("/test/{test_id}", response_class=HTMLResponse)
async def test_detail(test_id: str, request: Request):
    _require_teacher(request)
    with connect() as conn:
        test = conn.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
        if test is None:
            raise HTTPException(status_code=404)
        rows = conn.execute(
            """
            SELECT a.student_id, s.name, s.class_label, a.question_id,
                   MAX(a.attempt_number) AS attempts,
                   (SELECT assigned_marks FROM attempts a2
                     WHERE a2.student_id=a.student_id AND a2.test_id=a.test_id
                       AND a2.question_id=a.question_id
                     ORDER BY a2.attempt_number DESC LIMIT 1) AS latest_marks,
                   (SELECT max_marks FROM attempts a2
                     WHERE a2.student_id=a.student_id AND a2.test_id=a.test_id
                       AND a2.question_id=a.question_id
                     ORDER BY a2.attempt_number DESC LIMIT 1) AS latest_max
            FROM attempts a
            JOIN students s ON s.id = a.student_id
            WHERE a.test_id = ?
            GROUP BY a.student_id, a.question_id
            ORDER BY s.class_label, s.name, a.question_id
            """,
            (test_id,),
        ).fetchall()
    qids = json.loads(test["question_ids"])
    # Pivot for the table: { student_id: {qid: {marks, max, attempts}}, name, class_label }
    table: dict[int, dict] = {}
    for r in rows:
        sid = r["student_id"]
        cell = table.setdefault(sid, {"name": r["name"], "class_label": r["class_label"], "qs": {}})
        cell["qs"][r["question_id"]] = {
            "marks": r["latest_marks"],
            "max": r["latest_max"],
            "attempts": r["attempts"],
        }
    return TEMPLATES.TemplateResponse(
        "eXam/teacher_test_detail.html",
        {
            "request": request,
            "test": dict(test),
            "qids": qids,
            "rows": list(table.values()),
        },
    )


class RegenBody(BaseModel):
    question_id: str
    kind: str


@router.post("/regenerate-helper", response_class=JSONResponse)
async def regenerate_helper(body: RegenBody, request: Request):
    _require_teacher(request)
    from eXam.pregenerate import KINDS, pregenerate_for_question

    if body.kind not in KINDS:
        raise HTTPException(status_code=400, detail="Bad kind")
    subject = body.question_id.split("::", 1)[0]
    try:
        content = pregenerate_for_question(
            {"question_id": body.question_id}, subject, body.kind, force=True,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "content": content}


@router.get("/export/{test_id}.xlsx")
async def export(test_id: str, request: Request):
    _require_teacher(request)
    try:
        data = export_test_xlsx(test_id)
    except ValueError:
        raise HTTPException(status_code=404)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="eXam_test_{test_id}.xlsx"'
        },
    )

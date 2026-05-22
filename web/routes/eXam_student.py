# -*- coding: utf-8 -*-
"""eXam student-facing routes: login, dashboard.

Take page / submit / helper endpoints are added in Phase C and Phase D.
"""

from __future__ import annotations

import datetime as _dt
import hmac
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from eXam import auth as eXam_auth
from eXam.db import connect
from eXam.roster import canonical_name
from eXam.runtime import (
    latest_attempts,
    mark_scheme_entry,
    pdf_path_for,
    question_metadata,
    question_order_for_student,
)

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

router = APIRouter(prefix="/eXam", tags=["eXam-student"])


class StudentLoginBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    pin: str = Field(..., min_length=4, max_length=4)


def _current_student(request: Request) -> dict | None:
    sid = eXam_auth.current_student_id(request)
    if sid is None:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT id, name, class_label FROM students WHERE id=?", (sid,)
        ).fetchone()
        return dict(row) if row else None


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    with connect() as conn:
        names = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM students ORDER BY class_label, name"
            )
        ]
    return TEMPLATES.TemplateResponse(
        "eXam/login_student.html", {"request": request, "names": names}
    )


@router.post("/api/login", response_class=JSONResponse)
async def api_login(body: StudentLoginBody, request: Request, response: Response):
    name = canonical_name(body.name)
    pin = body.pin.strip()
    if not pin.isdigit() or len(pin) != 4:
        raise HTTPException(status_code=400, detail="PIN must be 4 digits")
    with connect() as conn:
        row = conn.execute(
            "SELECT id, pin FROM students WHERE name=?", (name,)
        ).fetchone()
    if row is None or not hmac.compare_digest(row["pin"], pin):
        # Same error message either way — don't leak which side was wrong.
        raise HTTPException(status_code=401, detail="Name or PIN is incorrect")
    payload = JSONResponse({"ok": True, "redirect": "/eXam/"})
    eXam_auth.apply_cookie(payload, request, row["id"])
    return payload


@router.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    eXam_auth.clear_cookie(resp)
    return resp


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    student = _current_student(request)
    if student is None:
        return RedirectResponse(url="/eXam/login", status_code=303)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, subject, class_label, status, created_at
            FROM tests
            WHERE status='ready' AND (class_label = ? OR class_label IS NULL)
            ORDER BY created_at DESC
            """,
            (student["class_label"],),
        ).fetchall()
        tests = [dict(r) for r in rows]
    return TEMPLATES.TemplateResponse(
        "eXam/student_dashboard.html",
        {"request": request, "student": student, "tests": tests},
    )


def _question_status(latest: dict | None) -> str:
    if latest is None:
        return "unanswered"
    if latest["assigned_marks"] >= latest["max_marks"] and latest["max_marks"] > 0:
        return "correct"
    return "retry"


@router.get("/test/{test_id}", response_class=HTMLResponse)
async def test_view(request: Request, test_id: str, q: int | None = None):
    student = _current_student(request)
    if student is None:
        return RedirectResponse(url="/eXam/login", status_code=303)
    with connect() as conn:
        row = conn.execute(
            "SELECT id, title, subject, status FROM tests WHERE id=?", (test_id,)
        ).fetchone()
    if row is None or row["status"] != "ready":
        raise HTTPException(status_code=404, detail="Test not found")
    qids = question_order_for_student(test_id, student["id"])
    if not qids:
        raise HTTPException(status_code=500, detail="Test has no questions")
    attempts = latest_attempts(test_id, student["id"])
    if q is None:
        # Overview list.
        items = []
        for idx, qid in enumerate(qids):
            meta = question_metadata(qid)
            latest = attempts.get(qid)
            items.append(
                {
                    "idx": idx,
                    "question_id": qid,
                    "number": meta["number"] if meta else qid,
                    "status": _question_status(latest),
                    "attempts": latest["attempt_count"] if latest else 0,
                    "assigned_marks": latest["assigned_marks"] if latest else None,
                    "max_marks": latest["max_marks"] if latest else None,
                }
            )
        return TEMPLATES.TemplateResponse(
            "eXam/test_overview.html",
            {
                "request": request,
                "student": student,
                "test": dict(row),
                "items": items,
            },
        )
    # Single-question take page.
    if q < 0 or q >= len(qids):
        raise HTTPException(status_code=400, detail="Question index out of range")
    qid = qids[q]
    meta = question_metadata(qid)
    if meta is None:
        raise HTTPException(status_code=500, detail="Question metadata missing")
    latest = attempts.get(qid)
    correct_count = sum(
        1 for v in attempts.values()
        if v["assigned_marks"] >= v["max_marks"] and v["max_marks"] > 0
    )
    retry_count = sum(
        1 for v in attempts.values()
        if not (v["assigned_marks"] >= v["max_marks"] and v["max_marks"] > 0)
    )
    unanswered = len(qids) - correct_count - retry_count
    solution_unlocked = latest is not None and (
        latest["assigned_marks"] >= latest["max_marks"] and latest["max_marks"] > 0
    )
    return TEMPLATES.TemplateResponse(
        "eXam/test_take.html",
        {
            "request": request,
            "student": student,
            "test": dict(row),
            "meta": meta,
            "latest": latest,
            "idx": q,
            "total": len(qids),
            "prev_idx": q - 1 if q > 0 else None,
            "next_idx": q + 1 if q < len(qids) - 1 else None,
            "counters": {
                "correct": correct_count,
                "retry": retry_count,
                "unanswered": unanswered,
            },
            "solution_unlocked": solution_unlocked,
        },
    )


@router.get("/pdf/{question_id}")
async def serve_pdf(request: Request, question_id: str):
    student = _current_student(request)
    if student is None:
        raise HTTPException(status_code=401, detail="Login required")
    path = pdf_path_for(question_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Snippet not found")
    return FileResponse(
        str(path),
        media_type="application/pdf",
        headers={"Cache-Control": "private, max-age=3600"},
    )


class SubmitBody(BaseModel):
    test_id: str = Field(..., min_length=1, max_length=64)
    question_id: str = Field(..., min_length=1, max_length=256)
    submitted: str = Field(..., max_length=10000)
    hint_used: bool = False
    solution_revealed: bool = False
    example_used: bool = False
    kb_used: bool = False


@router.post("/api/submit", response_class=JSONResponse)
async def api_submit(body: SubmitBody, request: Request):
    student = _current_student(request)
    if student is None:
        raise HTTPException(status_code=401, detail="Login required")
    # Phase C stores submissions with zero marks; Phase E wires the marker in.
    try:
        from eXam.marker import mark as marker_mark
        verdict = marker_mark(student["id"], body.question_id, body.submitted)
    except ImportError:
        verdict = {"assigned_marks": 0.0, "max_marks": 0.0, "reasoning": ""}
    now = _dt.datetime.now(_dt.UTC).isoformat()
    with connect() as conn:
        # attempt_number = max + 1 per (student, test, question)
        cur = conn.execute(
            """
            SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n
            FROM attempts
            WHERE student_id=? AND test_id=? AND question_id=?
            """,
            (student["id"], body.test_id, body.question_id),
        ).fetchone()
        attempt_number = cur["n"]
        conn.execute(
            """
            INSERT INTO attempts (
                student_id, test_id, question_id, attempt_number, submitted,
                assigned_marks, max_marks, reasoning,
                hint_used, solution_revealed, example_used, kb_used,
                submitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                student["id"], body.test_id, body.question_id, attempt_number,
                body.submitted,
                float(verdict["assigned_marks"]), float(verdict["max_marks"]),
                verdict.get("reasoning"),
                1 if body.hint_used else 0,
                1 if body.solution_revealed else 0,
                1 if body.example_used else 0,
                1 if body.kb_used else 0,
                now,
            ),
        )
    return {
        "ok": True,
        "attempt_number": attempt_number,
        "assigned_marks": verdict["assigned_marks"],
        "max_marks": verdict["max_marks"],
        "reasoning": verdict.get("reasoning") or "",
    }


@router.post("/api/helper", response_class=JSONResponse)
async def api_helper(request: Request):
    student = _current_student(request)
    if student is None:
        raise HTTPException(status_code=401, detail="Login required")
    body = await request.json()
    qid = body.get("question_id")
    kind = body.get("kind")
    if not qid or kind not in {"hint", "solution", "example", "kb"}:
        raise HTTPException(status_code=400, detail="Bad helper request")
    with connect() as conn:
        row = conn.execute(
            "SELECT content FROM question_helpers WHERE question_id=? AND kind=?",
            (qid, kind),
        ).fetchone()
        if row is not None:
            return {"ok": True, "content": row["content"], "cache_hit": True}
    # Cache miss: try lazy generation (Phase D adds this). Belt-and-braces.
    try:
        from eXam.pregenerate import pregenerate_for_question
    except ImportError:
        raise HTTPException(status_code=503, detail="Helper unavailable; ask teacher to pregenerate")
    rec = {"question_id": qid}
    subject = qid.split("::", 1)[0]
    try:
        pregenerate_for_question(rec, subject, kind)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Helper generation failed: {e}")
    with connect() as conn:
        row = conn.execute(
            "SELECT content FROM question_helpers WHERE question_id=? AND kind=?",
            (qid, kind),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=503, detail="Helper generation failed")
    return {"ok": True, "content": row["content"], "cache_hit": False}

"""Per-call cost tracking + per-test aggregation queries for eXam.

eXam is web-driven (continuous operation), so unlike xscore/eXercise it can't
rely on a per-run report alone — costs accumulate across the test build, every
student submission, and every on-demand helper request. Each AI call is logged
to ``ai_calls`` (see :mod:`eXam.db`) tagged with operation + test/student/
question, then the teacher dashboard aggregates via SQL.

Public surface:

- :func:`track` — context manager opened around each AI-bearing block; pushes
  a phase onto the active CostRecorder (bootstrapping one with the DB sink if
  none is active in the current context). Nested calls update the same
  recorder's phase stack.
- :func:`costs_by_test`, :func:`cost_for_test`, :func:`cost_breakdown` — read
  helpers that aggregate the ``ai_calls`` rows for dashboard rendering.
"""

from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager
from typing import Any

from eXam.db import connect
from eXercise.cost_recorder import collect_run_cost, current_recorder
from eXercise.cost_report import compute_one


def db_call_sink(
    top_ctx: dict, model: str, in_t: int, out_t: int, think_t: int, dur: float
) -> None:
    """Insert one row into ``ai_calls`` using the active phase context.

    Failures are silent — observer faults must never break the calling
    pipeline (CostRecorder.__call__ already swallows on_call exceptions, but
    we belt-and-brace here for clarity).
    """
    try:
        cost = compute_one(model, in_t, out_t)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_calls
                    (called_at, operation, test_id, student_id, question_id,
                     model, input_tokens, output_tokens, thinking_tokens,
                     duration_s, cost_rmb)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _dt.datetime.now(_dt.UTC).isoformat(),
                    top_ctx.get("phase") or "unknown",
                    top_ctx.get("test_id"),
                    top_ctx.get("student_id"),
                    top_ctx.get("question_id"),
                    model,
                    int(in_t),
                    int(out_t),
                    int(think_t),
                    float(dur),
                    float(cost),
                ),
            )
    except Exception:
        pass


@contextmanager
def track(operation: str, **context):
    """Tag every AI call inside the block with *operation* + *context*.

    If a recorder is already active on this context (e.g., test_builder
    wrapping ``run_build`` opened one), reuse it and push the operation as a
    phase. Otherwise bootstrap a fresh recorder wired to :func:`db_call_sink`
    so on-demand callers (single submit, single helper) also persist.
    """
    rec = current_recorder()
    if rec.is_null:
        with collect_run_cost(on_call=db_call_sink) as rec2:
            with rec2.phase(operation, **context):
                yield rec2
    else:
        with rec.phase(operation, **context):
            yield rec


# ---------------------------------------------------------------------------
# Aggregation queries for the teacher dashboard
# ---------------------------------------------------------------------------

def costs_by_test() -> list[dict[str, Any]]:
    """One row per test with total cost + call count. Tests with no AI calls
    appear with zero totals so the dashboard column is never blank."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.title, t.subject, t.class_label, t.status,
                   t.created_at, t.ready_at,
                   COUNT(c.id)                          AS calls,
                   COALESCE(SUM(c.input_tokens),    0)  AS input_tokens,
                   COALESCE(SUM(c.output_tokens),   0)  AS output_tokens,
                   COALESCE(SUM(c.thinking_tokens), 0)  AS thinking_tokens,
                   COALESCE(SUM(c.cost_rmb),        0)  AS total_cost_rmb
            FROM tests t LEFT JOIN ai_calls c ON c.test_id = t.id
            GROUP BY t.id
            ORDER BY t.created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def cost_for_test(test_id: str) -> dict[str, Any]:
    """Per-test cost, broken down by model and by operation.

    Returns ``{total_cost_rmb, total_calls, ..., by_model: [...], by_operation: [...]}``
    where each list entry has the same shape (model/operation + tokens + calls
    + duration + cost).
    """
    with connect() as conn:
        agg = conn.execute(
            """
            SELECT COUNT(*)                            AS calls,
                   COALESCE(SUM(input_tokens),    0)   AS input_tokens,
                   COALESCE(SUM(output_tokens),   0)   AS output_tokens,
                   COALESCE(SUM(thinking_tokens), 0)   AS thinking_tokens,
                   COALESCE(SUM(duration_s),      0.0) AS total_duration_s,
                   COALESCE(SUM(cost_rmb),        0.0) AS total_cost_rmb
            FROM ai_calls WHERE test_id=?
            """,
            (test_id,),
        ).fetchone()
        by_model = conn.execute(
            """
            SELECT model,
                   COUNT(*)                            AS calls,
                   COALESCE(SUM(input_tokens),    0)   AS input_tokens,
                   COALESCE(SUM(output_tokens),   0)   AS output_tokens,
                   COALESCE(SUM(thinking_tokens), 0)   AS thinking_tokens,
                   COALESCE(SUM(duration_s),      0.0) AS total_duration_s,
                   COALESCE(SUM(cost_rmb),        0.0) AS total_cost_rmb
            FROM ai_calls WHERE test_id=?
            GROUP BY model
            ORDER BY total_cost_rmb DESC
            """,
            (test_id,),
        ).fetchall()
        by_operation = conn.execute(
            """
            SELECT operation, model,
                   COUNT(*)                            AS calls,
                   COALESCE(SUM(input_tokens),    0)   AS input_tokens,
                   COALESCE(SUM(output_tokens),   0)   AS output_tokens,
                   COALESCE(SUM(thinking_tokens), 0)   AS thinking_tokens,
                   COALESCE(SUM(duration_s),      0.0) AS total_duration_s,
                   COALESCE(SUM(cost_rmb),        0.0) AS total_cost_rmb
            FROM ai_calls WHERE test_id=?
            GROUP BY operation, model
            ORDER BY operation, total_cost_rmb DESC
            """,
            (test_id,),
        ).fetchall()
    return {
        "total_cost_rmb": float(agg["total_cost_rmb"] or 0.0),
        "total_calls": int(agg["calls"] or 0),
        "total_input_tokens": int(agg["input_tokens"] or 0),
        "total_output_tokens": int(agg["output_tokens"] or 0),
        "total_thinking_tokens": int(agg["thinking_tokens"] or 0),
        "total_duration_s": float(agg["total_duration_s"] or 0.0),
        "by_model": [dict(r) for r in by_model],
        "by_operation": [dict(r) for r in by_operation],
    }


def cost_breakdown(
    *, since: str | None = None, until: str | None = None
) -> dict[str, Any]:
    """Global aggregation, optionally filtered to ``[since, until)`` (ISO timestamps).

    Used by the global teacher costs view. Returns per-model + per-operation
    + per-test rollups for the time window.
    """
    where = []
    params: list[Any] = []
    if since:
        where.append("called_at >= ?"); params.append(since)
    if until:
        where.append("called_at < ?"); params.append(until)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        totals = conn.execute(
            f"""
            SELECT COUNT(*)                            AS calls,
                   COALESCE(SUM(input_tokens),    0)   AS input_tokens,
                   COALESCE(SUM(output_tokens),   0)   AS output_tokens,
                   COALESCE(SUM(thinking_tokens), 0)   AS thinking_tokens,
                   COALESCE(SUM(cost_rmb),        0.0) AS total_cost_rmb
            FROM ai_calls{where_sql}
            """,
            tuple(params),
        ).fetchone()
        by_model = conn.execute(
            f"""
            SELECT model,
                   COUNT(*)                            AS calls,
                   COALESCE(SUM(input_tokens),    0)   AS input_tokens,
                   COALESCE(SUM(output_tokens),   0)   AS output_tokens,
                   COALESCE(SUM(thinking_tokens), 0)   AS thinking_tokens,
                   COALESCE(SUM(cost_rmb),        0.0) AS total_cost_rmb
            FROM ai_calls{where_sql}
            GROUP BY model
            ORDER BY total_cost_rmb DESC
            """,
            tuple(params),
        ).fetchall()
        by_operation = conn.execute(
            f"""
            SELECT operation,
                   COUNT(*)                            AS calls,
                   COALESCE(SUM(input_tokens),    0)   AS input_tokens,
                   COALESCE(SUM(output_tokens),   0)   AS output_tokens,
                   COALESCE(SUM(thinking_tokens), 0)   AS thinking_tokens,
                   COALESCE(SUM(cost_rmb),        0.0) AS total_cost_rmb
            FROM ai_calls{where_sql}
            GROUP BY operation
            ORDER BY total_cost_rmb DESC
            """,
            tuple(params),
        ).fetchall()
        by_test = conn.execute(
            f"""
            SELECT c.test_id, t.title,
                   COUNT(c.id)                          AS calls,
                   COALESCE(SUM(c.input_tokens),    0)  AS input_tokens,
                   COALESCE(SUM(c.output_tokens),   0)  AS output_tokens,
                   COALESCE(SUM(c.thinking_tokens), 0)  AS thinking_tokens,
                   COALESCE(SUM(c.cost_rmb),        0.0) AS total_cost_rmb
            FROM ai_calls c LEFT JOIN tests t ON t.id = c.test_id
            {where_sql.replace("called_at", "c.called_at") if where_sql else ""}
            GROUP BY c.test_id
            ORDER BY total_cost_rmb DESC
            """,
            tuple(params),
        ).fetchall()
    return {
        "total_cost_rmb": float(totals["total_cost_rmb"] or 0.0),
        "total_calls": int(totals["calls"] or 0),
        "total_input_tokens": int(totals["input_tokens"] or 0),
        "total_output_tokens": int(totals["output_tokens"] or 0),
        "total_thinking_tokens": int(totals["thinking_tokens"] or 0),
        "by_model": [dict(r) for r in by_model],
        "by_operation": [dict(r) for r in by_operation],
        "by_test": [dict(r) for r in by_test],
    }

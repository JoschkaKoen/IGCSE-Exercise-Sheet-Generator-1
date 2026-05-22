"""Roster import: XLSX → students table + printable PIN PDF.

XLSX columns (row 1 = headers, case-insensitive): required ``name``; optional
``class_label``. Existing students (matched by canonical name) keep their PIN
unless ``regenerate_pins=True``; non-listed students are left untouched (the
plan's diff-preview semantics, applied non-interactively here).
"""

from __future__ import annotations

import datetime as _dt
import re
import secrets
from io import BytesIO
from pathlib import Path

import openpyxl

from .db import connect


def canonical_name(raw: str) -> str:
    """Trim + collapse internal whitespace; preserve case."""
    return re.sub(r"\s+", " ", (raw or "").strip())


def _new_pin() -> str:
    return f"{secrets.randbelow(10000):04d}"


def _read_rows(xlsx_bytes: bytes) -> list[dict[str, str]]:
    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        headers = [str(h or "").strip().lower() for h in next(rows, [])]
        if "name" not in headers:
            raise ValueError("Roster XLSX needs a 'name' column in row 1")
        name_idx = headers.index("name")
        class_idx = headers.index("class_label") if "class_label" in headers else None
        out = []
        for row in rows:
            if row is None:
                continue
            name = canonical_name(str(row[name_idx] or ""))
            if not name:
                continue
            class_label = None
            if class_idx is not None and class_idx < len(row):
                cl = str(row[class_idx] or "").strip()
                class_label = cl or None
            out.append({"name": name, "class_label": class_label})
        return out
    finally:
        wb.close()


def import_roster(
    xlsx_bytes: bytes,
    *,
    regenerate_pins: bool = False,
) -> dict[str, list[dict]]:
    """Apply the roster diff.

    Returns ``{"inserted": [...], "updated": [...], "untouched": [...]}``;
    each item is ``{"name", "pin"|None, "class_label"}``. PINs only appear on
    inserted students (always) and updated students when ``regenerate_pins``.
    """
    rows = _read_rows(xlsx_bytes)
    now = _dt.datetime.now(_dt.UTC).isoformat()
    inserted: list[dict] = []
    updated: list[dict] = []
    untouched: list[dict] = []
    with connect() as conn:
        existing = {
            r["name"]: dict(r)
            for r in conn.execute("SELECT id, name, pin, class_label FROM students")
        }
        seen = set()
        for r in rows:
            name = r["name"]
            seen.add(name)
            class_label = r["class_label"]
            if name in existing:
                row = existing[name]
                if regenerate_pins:
                    new_pin = _new_pin()
                    conn.execute(
                        "UPDATE students SET pin=?, class_label=? WHERE id=?",
                        (new_pin, class_label, row["id"]),
                    )
                    updated.append(
                        {"name": name, "pin": new_pin, "class_label": class_label}
                    )
                else:
                    conn.execute(
                        "UPDATE students SET class_label=? WHERE id=?",
                        (class_label, row["id"]),
                    )
                    updated.append(
                        {"name": name, "pin": None, "class_label": class_label}
                    )
            else:
                pin = _new_pin()
                conn.execute(
                    "INSERT INTO students (name, pin, class_label, created_at) VALUES (?, ?, ?, ?)",
                    (name, pin, class_label, now),
                )
                inserted.append({"name": name, "pin": pin, "class_label": class_label})
        for name, row in existing.items():
            if name not in seen:
                untouched.append(
                    {"name": name, "pin": None, "class_label": row["class_label"]}
                )
    return {"inserted": inserted, "updated": updated, "untouched": untouched}


def generate_pin_pdf(rows: list[dict], output_path: Path) -> Path:
    """Write a simple printable PIN list to a PDF. One line per student.

    Uses PyMuPDF (already a project dep) — no extra dependency needed.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 portrait, pts
    margin = 50.0
    y = margin
    page.insert_text((margin, y), "eXam student PINs", fontsize=18, fontname="helv")
    y += 32
    page.insert_text(
        (margin, y),
        f"Generated {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        fontsize=10,
        fontname="helv",
    )
    y += 24
    line_h = 18.0
    for r in rows:
        pin = r.get("pin")
        if not pin:
            continue
        if y > 800:
            page = doc.new_page(width=595, height=842)
            y = margin
        cls = r.get("class_label") or ""
        suffix = f"   ({cls})" if cls else ""
        page.insert_text(
            (margin, y),
            f"{r['name']:<40} {pin}{suffix}",
            fontsize=12,
            fontname="cour",
        )
        y += line_h
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()
    return output_path

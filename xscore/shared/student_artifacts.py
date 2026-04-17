"""Write student-list artifacts (JSON + Markdown) to the run artifact directory."""

from __future__ import annotations

import json
from pathlib import Path

from xscore.shared.exam_paths import (
    artifact_students_json_path,
    artifact_students_markdown_path,
)


def write_student_artifacts(artifact_dir: Path, students: list[str]) -> None:
    """Write ``3_students.json`` and ``3_students.md`` into *artifact_dir*.

    *students* is the list of name strings returned by :func:`read_student_list`.
    Both files are created (or overwritten); the parent directory must exist.
    """
    json_path = artifact_students_json_path(artifact_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    # JSON — plain array for easy machine parsing
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(students, f, indent=2, ensure_ascii=False)

    # Markdown — human-readable numbered list
    n = len(students)
    count_word = "1 student" if n == 1 else f"{n} students"
    lines: list[str] = [
        "# Student List",
        "",
        count_word,
        "",
    ]
    for i, name in enumerate(students, 1):
        lines.append(f"{i}. {name}")
    lines.append("")

    md_path = artifact_students_markdown_path(artifact_dir)
    md_path.write_text("\n".join(lines), encoding="utf-8")

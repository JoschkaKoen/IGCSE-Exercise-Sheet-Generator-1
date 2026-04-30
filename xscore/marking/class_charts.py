"""matplotlib chart renderers for the class report (step 30).

Three PNG figures are produced and embedded into the class-report PDF:

- :func:`render_grade_histogram` (called twice — once per ``kind``):
  10-bin histogram of student percentages with a class-average vertical
  line. ``kind="raw"`` renders raw %, ``kind="curved"`` renders curved %.
- :func:`render_question_difficulty`: horizontal bar chart of the hardest
  leaf questions sorted by class average percentage.

All functions return the output path on success, or ``None`` when the
figure should be skipped (single-student class, no marks, no curved
data). Callers should treat ``None`` as "omit this figure from the
LaTeX report" and not write the file.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # non-interactive backend; must precede pyplot import.

from pathlib import Path
from typing import Literal

import matplotlib.pyplot as plt


_HIST_STYLE = {
    "raw":    {"color": "#4477aa", "title": "Raw %"},
    "curved": {"color": "#ee7733", "title": "Curved %"},
}


def render_grade_histogram(
    student_summaries: list[dict],
    out_path: Path,
    *,
    kind: Literal["raw", "curved"] = "raw",
) -> Path | None:
    """Render a 10-bin histogram of either raw or curved percentages.

    Returns None when fewer than 2 students have a value of the requested
    kind, so the caller can skip embedding a near-empty chart.
    """
    field = "percentage" if kind == "raw" else "curved_pct"
    values = [s[field] for s in student_summaries if s.get(field) is not None]
    if len(values) < 2:
        return None

    bins = list(range(0, 101, 10))           # 0,10,20,…,100 → 10 bins
    bin_centers = [b + 5 for b in bins[:-1]] # 5,15,…,95
    counts = [0] * 10
    for v in values:
        idx = min(int(v) // 10, 9)
        counts[idx] += 1

    fig, ax = plt.subplots(figsize=(6, 4), dpi=144)
    style = _HIST_STYLE[kind]
    ax.bar(bin_centers, counts, width=9.0, color=style["color"], label=style["title"])

    class_avg = sum(values) / len(values)
    ax.axvline(class_avg, color="black", linestyle="--", linewidth=1,
               label=f"Class avg ({class_avg:.0f}%)")

    ax.set_xlabel("Percentage")
    ax.set_ylabel("Number of students")
    ax.set_title(style["title"])
    ax.set_xticks(bins)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, max(counts) + 1)
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(axis="y", linestyle=":", alpha=0.4)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def render_question_difficulty(
    per_question_pct: dict[str, int],
    per_question_max: dict[str, int],
    out_path: Path,
    *,
    top_n: int = 20,
) -> Path | None:
    """Render a horizontal bar chart of the ``top_n`` hardest questions.

    ``per_question_pct`` should contain leaf questions only (parent rollups
    would double-count). Returns None if there are no questions to plot.
    """
    if not per_question_pct:
        return None

    # Sort hardest first (lowest class avg %).
    items = sorted(
        per_question_pct.items(),
        key=lambda kv: (kv[1], kv[0]),
    )
    total = len(items)
    shown = items[:top_n]
    qnums  = [q.replace("_", ".") for q, _ in shown]
    values = [v for _, v in shown]

    height_in = max(3.0, 0.30 * len(shown) + 1.5)
    fig, ax = plt.subplots(figsize=(6, height_in), dpi=144)
    y_pos = list(range(len(shown)))
    ax.barh(y_pos, values, color="#cc6677")
    ax.set_yticks(y_pos)
    ax.set_yticklabels(qnums, fontsize=9)
    ax.invert_yaxis()  # hardest at top
    ax.set_xlabel("Class average (%)")
    ax.set_xlim(0, 100)
    ax.set_title("Question difficulty — hardest first")
    ax.grid(axis="x", linestyle=":", alpha=0.4)
    for y, v in zip(y_pos, values):
        max_marks = per_question_max.get(shown[y][0])
        suffix = f" ({max_marks})" if max_marks else ""
        ax.text(min(v + 1, 96), y, f"{v}%{suffix}", va="center", fontsize=8, color="#333333")

    if total > top_n:
        fig.text(
            0.5, 0.01,
            f"…and {total - top_n} more questions not shown",
            ha="center", fontsize=8, color="#666666",
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=(0.0, 0.04, 1.0, 1.0) if total > top_n else (0.0, 0.0, 1.0, 1.0))
    fig.savefig(out_path)
    plt.close(fig)
    return out_path

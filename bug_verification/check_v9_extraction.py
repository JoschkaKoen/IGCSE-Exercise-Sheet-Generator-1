"""Sanity-check the v9 step-20 output against the expected formatting rules.

Loads the s23_12 exam_questions.yaml, walks every question's text recursively,
and asserts:

  Forbidden tokens absent
    * \\dotfill                          (must be replaced by \\underline{...})
    * literal "Working space"            (must be replaced by \\textit{(working space)})

  Expected new tokens present (s23_12 has all of these)
    * \\underline{\\hspace{1.5em}}
    * \\textit{(working space)}
    * \\framebox{$\\square                (Q2bi register)
    * (1)~\\underline{\\hspace{1.5em}}    (numbered-slots pattern)

  Content tables preserved
    * "actuator & digital versatile disk" (Q1)
    * "compression & executable"          (Q6a)

Exit code 1 on any failure so the script can be wired into CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

YAML_PATH = Path("output/xscore/s23_12/2026-05-03_15-47-55/20_extract_exam_questions/exam_questions.yaml")


def collect_text(nodes: list[dict]) -> str:
    """Return all `text` and option-text fields concatenated, recursively."""
    out: list[str] = []
    for n in nodes:
        out.append(str(n.get("text") or ""))
        for opt in n.get("options") or []:
            out.append(str(opt.get("text") or ""))
        out.extend([collect_text(n.get("subquestions") or [])])
    return "\n".join(out)


def main() -> int:
    doc = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    corpus = collect_text(doc["questions"])

    forbidden = {
        r"\dotfill": 0,
        "Working space": 0,
    }
    expected = {
        r"\underline{\hspace{1.5em}}": 1,
        r"\textit{(working space)}": 1,
        r"\framebox{$\square": 1,
        r"(1)~\underline{\hspace{1.5em}}": 1,
    }
    content_tables = [
        "actuator & digital versatile disk",
        "compression & executable",
    ]

    failures: list[str] = []
    for token, _ in forbidden.items():
        n = corpus.count(token)
        status = "OK " if n == 0 else "FAIL"
        print(f"[{status}] forbidden  count={n}  {token!r}")
        if n != 0:
            failures.append(f"forbidden token {token!r} appears {n} times")

    for token, min_count in expected.items():
        n = corpus.count(token)
        status = "OK " if n >= min_count else "FAIL"
        print(f"[{status}] expected   count={n}  (need >= {min_count})  {token!r}")
        if n < min_count:
            failures.append(f"expected token {token!r} count={n} (need >= {min_count})")

    for sub in content_tables:
        n = corpus.count(sub)
        status = "OK " if n >= 1 else "FAIL"
        print(f"[{status}] content    count={n}  {sub!r}")
        if n < 1:
            failures.append(f"content-table substring {sub!r} missing")

    print()
    if failures:
        print(f"FAIL — {len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

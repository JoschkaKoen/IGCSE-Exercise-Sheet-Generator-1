"""Write handout meta.yaml after authoring an .md file.

Usage:
    .venv/bin/python scripts/write_handout_meta.py <subject> <topic>

Writes ``output/eXam/handouts/<subject>/<NN>.meta.yaml`` listing every
question id matched to the given topic — same shape the AI-pipeline would
have produced, but recording the human/Claude author instead of a model.
"""
from __future__ import annotations

import sys
import yaml

from web.handouts_collect import (
    collect_questions_for_topic,
    meta_path,
    now_iso,
    topic_for_number,
)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    subject, topic = sys.argv[1], sys.argv[2]
    t = topic_for_number(subject, topic)
    if t is None:
        print(f"no topic {topic} for {subject}", file=sys.stderr)
        return 1
    qs = collect_questions_for_topic(subject, topic)
    meta = {
        "topic_number": topic,
        "topic_title": t["title"],
        "subject_key": subject,
        "covered_question_ids": [
            {"paper": q.paper_stem, "qnum": q.qnum_leaf} for q in qs
        ],
        "generated_at": now_iso(),
        "author": "claude",
        "prompt_version": "v1",
    }
    path = meta_path(subject, topic)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(
            meta, f, sort_keys=False, allow_unicode=True, default_flow_style=False
        )
    print(f"{path} ({len(qs)} questions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

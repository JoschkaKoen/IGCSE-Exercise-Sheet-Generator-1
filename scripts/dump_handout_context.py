"""Dump syllabus + matched questions for a handout topic to stdout.

Usage:
    .venv/bin/python -m scripts.dump_handout_context <subject> <topic>

Convenient when authoring a handout: redirect to a tmpfile, read it,
then write the handout markdown to ``output/eXam/handouts/<subject>/<NN>.md``.
"""
from __future__ import annotations

import sys

from web.handouts_collect import (
    collect_questions_for_topic,
    load_syllabus_content_for_topic,
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
    print(f"=== TOPIC {t['number']}. {t['title']} ===")
    for s in t.get("subtopics") or []:
        print(f"  {s['number']} {s['title']}")
    print()
    print("=== SYLLABUS ===")
    print(load_syllabus_content_for_topic(subject, t))
    print()
    qs = collect_questions_for_topic(subject, topic)
    print(f"=== {len(qs)} QUESTIONS ===")
    print()
    for q in qs:
        print(q.format_for_prompt())
        print()
        print("---")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

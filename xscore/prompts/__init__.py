"""Prompt templates as data — load via :func:`xscore.prompts.loader.load_prompt`.

Each prompt lives in a sibling ``.md`` file with optional YAML front-matter:

    ---
    name: parse_exam_user
    version: v1
    description: Fallback user prompt for exam-PDF parsing (no layout known)
    ---

    Prompt body here. Placeholders use $name (string.Template syntax).

The marking system prompt remains assembled in ``xscore/marking/mark_page.py``
because its body is composed from format-adapter sections at runtime (XML / YAML
/ JSON variants). It can be migrated later by lifting each adapter section into
its own ``.md`` template.
"""

from xscore.prompts.loader import load_prompt

__all__ = ["load_prompt"]

"""Prompt templates as data — load via :func:`xscore.prompts.loader.load_prompt`.

Prompt files live in step-named subfolders (one folder per pipeline step in
``xscore/shared/pipeline_steps.py``), e.g.::

    xscore/prompts/
      parse_grading_instructions/parse_grading_instructions.md
      ai_marking/ai_marking_xml.md
      ai_marking/ai_marking_fragments.md  (multi-section: FIELD_RULES, GRID, …)

Each ``.md`` has optional YAML front-matter and a body. Files with multiple
roles use Markdown H2 section headers (``## SYSTEM`` / ``## USER`` /
``## FIELD_RULES`` etc.); pass ``section=`` to ``load_prompt`` to extract one::

    ---
    name: detect_exam_layout
    version: v1
    description: ...
    ---
    ## SYSTEM
    You are an expert at identifying exam paper printing layouts.

    ## USER
    Look at this exam page image. Determine ...

Substitution uses ``$name`` (``string.Template`` syntax) via
``safe_substitute`` — missing placeholders are kept literal. Lookup is by
bare filename stem (recursive across subfolders); stems must be globally unique.
"""

from xscore.prompts.loader import load_prompt

__all__ = ["load_prompt"]

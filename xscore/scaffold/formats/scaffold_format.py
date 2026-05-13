"""The :class:`ScaffoldFormat` class — YAML AI scaffold output.

Block scalars preserve LaTeX content in question text and criteria with no
format-level escaping. The class is the dispatch surface used by the scaffold
pipeline (steps 19–24); the YAML I/O utilities, prompt builders, and per-node
parse/serialize helpers live in sibling modules under this package.
"""

from __future__ import annotations

import yaml

from xscore.prompts.loader import load_prompt
from xscore.scaffold.formats._parsers import (
    _exam_q_to_yaml_dict,
    _parse_yaml_question,
    _parse_yaml_scaffold_node,
    _scaffold_node_to_yaml_dict,
)
from xscore.scaffold.formats._prompt_builders import (
    _build_user_exam_prompt_yaml,
    _build_user_question_numbers_prompt_yaml,
)
from xscore.scaffold.formats._yaml_io import (
    _ScaffoldDumper,
    _load_scheme_yaml_recovering,
)
from xscore.scaffold.pdf_parser.content import strip_exam_mark_indicators
from xscore.shared.response_parsing import strip_code_fences as _strip_fences


class ScaffoldFormat:

    def system_exam_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_exam_prompt
        return make_system_exam_prompt("parse_exam_pdf", is_cs=is_cs)

    def system_scheme_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_scheme_prompt
        return make_system_scheme_prompt("parse_mark_scheme", is_cs=is_cs)

    def system_question_numbers_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_question_numbers_prompt
        return make_system_question_numbers_prompt("extract_exam_question_numbers", is_cs=is_cs)

    def system_questions_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_questions_prompt
        return make_system_questions_prompt("extract_exam_questions", is_cs=is_cs)

    def build_exam_prompt(self, layout_result, is_split: bool, n_split_pages: int) -> str:
        return _build_user_exam_prompt_yaml(layout_result, is_split, n_split_pages)

    def build_scheme_user_msg(
        self, scaffold_str: str, pages: list[int], n_pages: int,
        input_label: str = "PDF",
    ) -> str:
        if len(pages) == 1:
            page_label = str(pages[0])
        elif pages == list(range(pages[0], pages[-1] + 1)):
            page_label = f"{pages[0]}–{pages[-1]}"
        else:
            page_label = ", ".join(str(p) for p in pages)
        page_note = (
            f"\n\n## Page context\n"
            f"The {input_label} you receive contains page(s) {page_label} of {n_pages} of the mark scheme.\n"
            f"Each scaffold entry tells you exactly which empty fields to fill — MCQ "
            f"entries have `correct_answer` + `explanation`; every other type has a "
            f"single `mark_scheme_answer`. Fill the fields that exist on each entry; "
            f"do not invent extra keys. "
            f"If a question's content spans multiple pages within this {input_label}, "
            f"assemble the COMPLETE content into a SINGLE entry — do NOT emit the same "
            f"question twice. For every scaffold question whose content does not appear "
            f"here, leave its empty field(s) as `''`. Keep every scaffold entry — do not remove any."
        )
        return load_prompt(
            "parse_mark_scheme", section="user", scaffold=scaffold_str,
        )[1] + page_note

    def build_scheme_scaffold(self, questions: list[dict]) -> str:
        """Build YAML scaffold from exam questions for the scheme AI.

        Field shape is type-driven so the AI knows exactly which slots to
        fill — no judgement call about "is this content the answer or a
        criterion":

        - **MCQ** entries carry ``correct_answer`` (the letter) and
          ``explanation`` (a bullet rationale).
        - **Non-MCQ** entries carry a single ``mark_scheme_answer`` slot
          for the entire printed mark-scheme cell.
        """
        entries = []

        def _visit(node: dict) -> None:
            qtype = str(node.get("question_type", ""))
            entry = {
                "number": str(node.get("number", "")),
                "type": qtype,
                "marks": int(node.get("marks", 0)),
            }
            if qtype == "multiple_choice":
                entry["correct_answer"] = ""
                entry["explanation"] = ""
            else:
                entry["mark_scheme_answer"] = ""
            entries.append(entry)
            for sub in (node.get("subquestions") or []):
                _visit(sub)

        for q in questions:
            _visit(q)

        doc = {"questions": entries}
        return yaml.dump(
            doc, Dumper=_ScaffoldDumper,
            allow_unicode=True, default_flow_style=False,
            sort_keys=False,
        )

    def extract_question_numbers(self, scaffold_str: str) -> list[str]:
        try:
            data = yaml.safe_load(scaffold_str)
            if not isinstance(data, dict):
                return []
            return [
                str(q.get("number", ""))
                for q in data.get("questions", [])
                if isinstance(q, dict) and q.get("number")
            ]
        except yaml.YAMLError:
            return []

    def parse_exam_response(self, raw: str) -> tuple[list[dict], dict]:
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Exam YAML parse error: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Exam YAML: expected a mapping, got {type(data).__name__}")
        layout = {
            "rows": int(data.get("rows", 1)),
            "cols": int(data.get("cols", 1)),
        }
        questions = [_parse_yaml_question(q) for q in data.get("questions", []) if isinstance(q, dict)]
        return questions, layout

    def parse_scheme_response(self, raw: str) -> dict:
        """Parse the AI's scheme response into a per-page intermediate shape.

        Type-driven schema (matches what ``build_scheme_scaffold`` asked for):

        - **MCQ** entries carry ``correct_answer`` (letter) + ``explanation``
          (rationale) + ``graphics``.
        - **Non-MCQ** entries carry ``mark_scheme_answer`` (single block) +
          ``graphics``.

        Also accepts the legacy ``correct_answer`` + ``criteria: [...]`` shape
        on input (mid-transition runs / older AI outputs) and folds it into
        the new shape: criteria are joined into ``mark_scheme_answer`` for
        non-MCQ, into ``explanation`` for MCQ.
        """
        data = _load_scheme_yaml_recovering(_strip_fences(raw))
        if not isinstance(data, dict):
            return {"questions": []}
        questions = []
        for q in data.get("questions", []):
            if not isinstance(q, dict):
                continue
            qtype = str(q.get("type", "") or q.get("question_type", "")).strip()
            number = str(q.get("number", ""))
            ca_raw = q.get("correct_answer")
            ca = str(ca_raw).strip() if ca_raw is not None else ""

            # Preferred new-shape fields.
            msa = q.get("mark_scheme_answer")
            msa = str(msa).strip() if msa is not None else ""
            explanation = q.get("explanation")
            explanation = str(explanation).strip() if explanation is not None else ""

            # Legacy shape: criteria as a list of {mark, criterion}. Fold into
            # the right new-shape field when the new field wasn't populated.
            legacy_criteria = q.get("criteria") or q.get("mark_scheme") or []
            legacy_text_parts: list[str] = []
            for c in legacy_criteria:
                if isinstance(c, dict):
                    ct = str(c.get("criterion", "")).strip()
                    if ct:
                        legacy_text_parts.append(ct)
            legacy_block = "\n".join(legacy_text_parts)

            if qtype == "multiple_choice":
                if not explanation and legacy_block:
                    explanation = legacy_block
                questions.append({
                    "number":         number,
                    "question_type":  "multiple_choice",
                    "correct_answer": ca or None,
                    "explanation":    explanation or None,
                    "graphics":       [],
                })
            else:
                # Non-MCQ. New shape: mark_scheme_answer carries everything.
                if not msa:
                    parts = [p for p in (ca, legacy_block) if p]
                    msa = "\n".join(parts)
                questions.append({
                    "number":             number,
                    "question_type":      qtype or None,
                    "mark_scheme_answer": msa or None,
                    "graphics":           [],
                })
        return {"questions": questions}

    def serialize_exam(self, questions: list[dict], layout: dict) -> str:
        doc = {
            "rows": layout.get("rows", 1),
            "cols": layout.get("cols", 1),
            "questions": [_exam_q_to_yaml_dict(q) for q in questions],
        }
        return yaml.dump(
            doc, Dumper=_ScaffoldDumper,
            allow_unicode=True, default_flow_style=False,
            sort_keys=False,
        )

    # ---- extract_exam_question_numbers — extract question numbers --------------------------------

    def build_question_numbers_user_msg(
        self, layout_result, is_split: bool, n_split_pages: int,
    ) -> str:
        return _build_user_question_numbers_prompt_yaml(layout_result, is_split, n_split_pages)

    def parse_question_numbers_response(self, raw: str) -> tuple[list[dict], dict]:
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Question-numbers YAML parse error: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Question-numbers YAML: expected a mapping, got {type(data).__name__}"
            )
        layout = {
            "rows": int(data.get("rows", 1)),
            "cols": int(data.get("cols", 1)),
        }
        nodes = [
            _parse_yaml_scaffold_node(q)
            for q in data.get("questions", [])
            if isinstance(q, dict)
        ]
        return nodes, layout

    def serialize_scaffold(self, nodes: list[dict], layout: dict) -> str:
        doc = {
            "rows": layout.get("rows", 1),
            "cols": layout.get("cols", 1),
            "questions": [_scaffold_node_to_yaml_dict(n) for n in nodes],
        }
        return yaml.dump(
            doc, Dumper=_ScaffoldDumper,
            allow_unicode=True, default_flow_style=False,
            sort_keys=False,
        )

    # ---- extract_exam_questions — extract question text + options -------------------------

    def build_questions_stub(self, filtered_nodes: list[dict]) -> str:
        lines = []
        for n in filtered_nodes:
            num = str(n.get("number", ""))
            qt = str(n.get("question_type", "short_answer"))
            lines.append(f'  - number: "{num}"')
            lines.append(f"    type: {qt}")
            lines.append("    text: \"\"")
        return "\n".join(lines)

    def build_questions_user_msg(
        self, stub_str: str, page_num: int, n_pages: int,
        expected_qnums: list[str], input_label: str = "PDF",
    ) -> str:
        qnums_str = ", ".join(f'"{q}"' for q in expected_qnums) or "(none)"
        page_note = (
            f"\n\n## Page context\n"
            f"The {input_label} you receive is page {page_num} of {n_pages} of the exam. "
            f"The expected question numbers on this page are: {qnums_str}.\n"
            f"Return exactly these entries, in this order, one per `number` listed above. "
            f"Do not add, remove, reorder, or rename any entry."
        )
        return load_prompt(
            "extract_exam_questions", section="user", question_stub=stub_str,
        )[1] + page_note

    def parse_questions_response(self, raw: str) -> list[dict]:
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Questions YAML parse error: {exc}") from exc
        if isinstance(data, dict):
            entries = data.get("questions") or []
        elif isinstance(data, list):
            entries = data
        else:
            return []
        out: list[dict] = []
        for q in entries:
            if not isinstance(q, dict):
                continue
            out.append({
                "number":  str(q.get("number", "")),
                "text":    strip_exam_mark_indicators(str(q.get("text", "")).strip()),
                "options": [
                    {
                        "letter": str(o.get("letter", "")),
                        "text":   strip_exam_mark_indicators(str(o.get("text", "")).strip()),
                    }
                    for o in (q.get("options") or [])
                    if isinstance(o, dict)
                ],
            })
        return out

    # ---- detect_mark_scheme_graphics (detect_mark_scheme_graphics) ------------------------------

    def parse_graphics_response(self, raw: str) -> list[dict]:
        """Parse one page's graphics-detection response.

        Returns ``[{"question_number": str, "bbox": [int,int,int,int],
        "description": str}, ...]``. Empty/garbled input → ``[]``; the call site
        treats parse failure as "no graphics on this page" rather than fatal.
        """
        cleaned = _strip_fences(raw or "").strip()
        if not cleaned:
            return []
        try:
            data = yaml.safe_load(cleaned)
        except yaml.YAMLError:
            return []
        if not isinstance(data, dict):
            return []
        out: list[dict] = []
        for g in data.get("graphics") or []:
            if not isinstance(g, dict):
                continue
            bbox_raw = g.get("bbox") or []
            try:
                bbox = [int(v) for v in bbox_raw]
            except (TypeError, ValueError):
                continue
            out.append({
                "question_number": str(g.get("question_number", "")).strip(),
                "bbox":            bbox,
                "description":     str(g.get("description", "")).strip(),
            })
        return out

    # ---- assign_scheme_questions (assign_scheme_questions) ----------------------------------

    def parse_assign_response(self, raw: str) -> list[str]:
        """Parse one page's question-assignment response.

        Returns the list of question-number strings the model emitted. Empty/
        garbled input → ``[]``; call sites filter against the allowed set.
        """
        cleaned = _strip_fences(raw or "").strip()
        if not cleaned:
            return []
        try:
            data = yaml.safe_load(cleaned)
        except yaml.YAMLError:
            return []
        if not isinstance(data, dict):
            return []
        return [str(q).strip() for q in (data.get("questions") or [])]

    def artifact_ext(self) -> str:
        return "yaml"

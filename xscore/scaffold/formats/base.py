"""Abstract base for AI scaffold output formats (XML / YAML / JSON)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ScaffoldFormat(ABC):

    @abstractmethod
    def build_exam_prompt(
        self, layout_result, is_split: bool, n_split_pages: int
    ) -> str:
        """Build the user prompt for the exam-extraction call."""

    @abstractmethod
    def build_scheme_user_msg(
        self, scaffold_str: str, page_num: int, n_pages: int,
        input_label: str = "PDF",
    ) -> str:
        """Build the per-page user message for the scheme-extraction call.

        Combines the base scaffold message and the page note.
        *input_label* is ``"image"`` for non-Gemini (OpenAI-compat) calls,
        ``"PDF"`` for Gemini native calls.
        """

    @abstractmethod
    def build_scheme_scaffold(self, questions: list[dict]) -> str:
        """Build the scaffold string sent to the scheme AI (analogous to _build_scheme_scaffold)."""

    @abstractmethod
    def extract_question_numbers(self, scaffold_str: str) -> list[str]:
        """Extract all question number strings from a scaffold string.

        Used by the graphics detector to generate hint prompts.
        """

    @abstractmethod
    def parse_exam_response(self, raw: str) -> tuple[list[dict], dict]:
        """Parse raw exam AI response → (questions_list, layout_dict)."""

    @abstractmethod
    def parse_scheme_response(self, raw: str) -> dict:
        """Parse raw scheme AI response → scheme dict {questions: [...]}."""

    @abstractmethod
    def serialize_exam(self, questions: list[dict], layout: dict) -> str:
        """Serialise post-remap question dicts for the step-10 artifact."""

    @abstractmethod
    def build_scaffold_user_msg(
        self, layout_result, is_split: bool, n_split_pages: int,
    ) -> str:
        """Build the user prompt for the detect-scaffold call (no text extracted)."""

    @abstractmethod
    def parse_scaffold_response(self, raw: str) -> tuple[list[dict], dict]:
        """Parse raw detect-scaffold response → (nodes, layout). Each node has
        empty ``text`` and ``answer_options`` — those are populated later by the
        fill phase."""

    @abstractmethod
    def serialize_scaffold(self, nodes: list[dict], layout: dict) -> str:
        """Serialise the intermediate detect-scaffold artifact (no text/options)."""

    @abstractmethod
    def build_fill_stub(self, filtered_nodes: list[dict]) -> str:
        """Build the per-page stub string injected as ``$scaffold`` in the fill prompt.

        *filtered_nodes* is a flat list (output of ``_filter_questions_by_qnums``).
        Each entry contributes its number+type to the stub; the model fills in
        text and options.
        """

    @abstractmethod
    def build_fill_user_msg(
        self, stub_str: str, page_num: int, n_pages: int,
        expected_qnums: list[str], input_label: str = "PDF",
    ) -> str:
        """Build the per-page fill user prompt. *expected_qnums* may be embedded
        in the page note; *input_label* is ``"image"`` for non-Gemini calls and
        ``"PDF"`` for Gemini native calls."""

    @abstractmethod
    def parse_fill_response(self, raw: str) -> list[dict]:
        """Parse raw fill response → flat list of ``{number, text, options}``."""

    def system_exam_prompt(self, is_cs: bool = False) -> str:
        """Return the system prompt for the exam-extraction call.

        ``is_cs`` triggers the conditional CODE_FORMATTING section append for
        Computer Science exams (gates ``\\texttt`` / ``\\begin{alltt}`` rules
        in question text and MCQ options).
        """
        from xscore.scaffold.scaffold_prompts import make_system_exam_prompt
        return make_system_exam_prompt("parse_exam_pdf_xml", is_cs=is_cs)

    def system_scheme_prompt(self, is_cs: bool = False) -> str:
        """Return the system prompt for the scheme-extraction call.

        ``is_cs`` triggers the conditional CODE_FORMATTING section append for
        Computer Science exams (gates ``\\texttt`` / ``\\begin{alltt}`` rules).
        """
        from xscore.scaffold.scaffold_prompts import make_system_scheme_prompt
        return make_system_scheme_prompt("parse_mark_scheme_xml", is_cs=is_cs)

    def system_scaffold_prompt(self, is_cs: bool = False) -> str:
        """System prompt for the detect-scaffold call (no text extraction)."""
        from xscore.scaffold.scaffold_prompts import make_system_scaffold_prompt
        return make_system_scaffold_prompt("detect_exam_scaffold_xml", is_cs=is_cs)

    def system_fill_prompt(self, is_cs: bool = False) -> str:
        """System prompt for the per-page fill-scaffold call.

        ``is_cs`` appends CODE_FORMATTING (same gate as ``system_exam_prompt``).
        """
        from xscore.scaffold.scaffold_prompts import make_system_fill_prompt
        return make_system_fill_prompt("fill_exam_scaffold_xml", is_cs=is_cs)

    def pydantic_schema_exam(self):
        """Return Pydantic class for Gemini response_schema (exam), or None."""
        return None

    def pydantic_schema_scheme(self):
        """Return Pydantic class for Gemini response_schema (scheme), or None."""
        return None

    def pydantic_schema_scaffold(self):
        """Return Pydantic class for Gemini response_schema (detect-scaffold), or None."""
        return None

    def pydantic_schema_fill(self):
        """Return Pydantic class for Gemini response_schema (fill-scaffold), or None."""
        return None

    def scheme_oa_extra_kwargs(self, model: str) -> dict:
        """Extra kwargs for non-Gemini (OpenAI-compat) scheme calls.

        ``model`` is the model id (e.g. "qwen3.6-flash") so subclasses can
        branch on provider — needed because some providers reject certain
        ``response_format`` shapes when a system message is present.
        """
        return {}

    def artifact_ext(self) -> str:
        """File extension for scaffold artifacts ('xml', 'yaml', or 'json')."""
        return "xml"

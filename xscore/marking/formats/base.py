"""Abstract base for AI marking output formats (XML / YAML / JSON)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class FormatParseError(ValueError):
    """Raised by parse_response() on malformed AI output.

    Callers catch this and ``break`` (no retry), identical to the current
    ``ET.ParseError`` behaviour.
    """


class MarkingFormat(ABC):
    # --- Blueprint construction (step 13) ---

    @abstractmethod
    def build_blueprint(self, page_num: int, layout, questions: list[dict]) -> str: ...

    def validate_blueprint(self, text: str) -> None:
        """Validate *text* as a well-formed blueprint. Raises RuntimeError if invalid."""

    # --- Prompt fragments ---

    @abstractmethod
    def section_A(self) -> str:
        """Role + task description — references the format by name."""

    @abstractmethod
    def criterion_ref(self) -> str:
        """Short phrase used in section B: '<criterion> elements' or 'criteria entries'."""

    @abstractmethod
    def section_C(self, rows: int, cols: int) -> str:
        """Return-format instructions appended after section B."""

    @abstractmethod
    def section_D(self) -> str:
        """Format-specific validity / escaping rules appended after section C."""

    @abstractmethod
    def subpage_ref(self) -> str:
        """Short phrase used in section E: '<subpage> elements' or 'subpage entries'."""

    @abstractmethod
    def build_user_text(self, blueprint_str: str) -> str:
        """Build the user message from the blueprint string."""

    # --- API enforcement ---

    @abstractmethod
    def api_extra_kwargs(self, model: str) -> dict:
        """Extra kwargs merged into the API call.

        Model-aware: gemini-* models get Gemini-native enforcement dict;
        other models get OpenAI-compatible enforcement dict.
        XML and YAML implementations return {}.
        """

    def prefer_stream(self) -> bool:
        """Return False to disable streaming (JSON format skips it)."""
        return True

    # --- Response parsing ---

    @abstractmethod
    def parse_response(self, raw: str) -> list[dict]:
        """Parse raw AI response → list of question dicts.

        Must raise :class:`FormatParseError` on malformed output.
        """

    # --- Serialisation ---

    @abstractmethod
    def serialize_filled(self, filled: dict) -> str:
        """Serialise a filled blueprint dict to a string for step-14 artifact."""

    @abstractmethod
    def deserialize_blueprint(self, text: str) -> dict:
        """Parse a blueprint string (step-13 or step-14 artifact) to a dict.

        Returns dict with keys: ``page``, ``layout``, ``questions``, and
        optionally ``student_name`` (present in filled step-14 artifacts).
        """

    def artifact_ext(self) -> str:
        """File extension for blueprint artifacts ('xml', 'yaml', or 'json')."""
        return "xml"

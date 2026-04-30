"""Provider protocol for vision LLM extraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


class Provider(Protocol):
    def extract(
        self,
        client: Any,
        image_bytes: bytes,
        prompt: str,
        schema: type[BaseModel],
        page_num: int,
        answer_fields: list[str],
        prompt_save_dir: Path | None = None,
    ) -> dict:
        """Return normalized extraction dict (MC fields coerced to A/B/C/D/?)."""
        ...

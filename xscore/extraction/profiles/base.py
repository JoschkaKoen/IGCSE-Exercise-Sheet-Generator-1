"""Exam profile: bundles prompt, structured-output schema, and graded answer fields."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class ExamProfile:
    name: str
    prompt: str
    schema: type[BaseModel]
    answer_fields: tuple[str, ...]

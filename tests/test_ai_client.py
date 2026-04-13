# -*- coding: utf-8 -*-
"""Smoke tests for eXercise.ai_client — pure logic, no network calls."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eXercise.ai_client import (
    build_thinking_kwargs,
    get_api_key_env_name,
    parse_model_effort,
    provider_for_model,
    strip_json_fences,
)


class TestProviderForModel:
    def test_gemini_prefix(self):
        assert provider_for_model("gemini-2.5-flash") == "gemini"

    def test_gemini_prefix_uppercase(self):
        # Should be case-insensitive
        assert provider_for_model("Gemini-2.0-flash") == "gemini"

    def test_grok_prefix_gives_xai(self):
        assert provider_for_model("grok-3") == "xai"

    def test_grok_long_model(self):
        assert provider_for_model("grok-4-1-fast-non-reasoning") == "xai"

    def test_qwen_prefix(self):
        assert provider_for_model("qwen3-32b") == "qwen"

    def test_unknown_model_falls_back_to_gemini(self):
        assert provider_for_model("unknown-model-xyz") == "gemini"

    def test_empty_string_falls_back_to_gemini(self):
        assert provider_for_model("") == "gemini"


class TestParseModelEffort:
    def test_model_only(self):
        model, effort = parse_model_effort("gemini-2.5-flash")
        assert model == "gemini-2.5-flash"
        assert effort is None

    def test_model_with_low_effort(self):
        model, effort = parse_model_effort("gemini-2.5-flash, low")
        assert model == "gemini-2.5-flash"
        assert effort == "low"

    def test_model_with_high_effort(self):
        model, effort = parse_model_effort("gemini-2.5-flash, high")
        assert model == "gemini-2.5-flash"
        assert effort == "high"

    def test_model_with_off_effort(self):
        model, effort = parse_model_effort("gemini-2.5-flash, off")
        assert model == "gemini-2.5-flash"
        assert effort == "off"

    def test_invalid_effort_becomes_none(self):
        model, effort = parse_model_effort("gemini-2.5-flash, medium")
        assert model == "gemini-2.5-flash"
        assert effort is None

    def test_whitespace_stripped(self):
        model, effort = parse_model_effort("  gemini-2.5-flash  ")
        assert model == "gemini-2.5-flash"
        assert effort is None

    def test_effort_whitespace_stripped(self):
        model, effort = parse_model_effort("gemini-2.5-flash,   high   ")
        assert model == "gemini-2.5-flash"
        assert effort == "high"

    def test_empty_effort_after_comma_is_none(self):
        model, effort = parse_model_effort("gemini-2.5-flash,")
        assert model == "gemini-2.5-flash"
        assert effort is None


class TestBuildThinkingKwargs:
    def test_gemini_default_streams(self):
        use_stream, kw = build_thinking_kwargs("gemini", None)
        assert use_stream is True
        assert kw == {}

    def test_gemini_low_effort_streams(self):
        use_stream, kw = build_thinking_kwargs("gemini", "low")
        assert use_stream is True
        assert kw == {"reasoning_effort": "low"}

    def test_gemini_high_effort_streams(self):
        use_stream, kw = build_thinking_kwargs("gemini", "high")
        assert use_stream is True
        assert kw == {"reasoning_effort": "high"}

    def test_gemini_off_effort_no_stream(self):
        use_stream, kw = build_thinking_kwargs("gemini", "off")
        assert use_stream is False
        assert kw == {"reasoning_effort": "none"}

    def test_qwen_default_streams_with_thinking(self):
        use_stream, kw = build_thinking_kwargs("qwen", None)
        assert use_stream is True
        assert kw == {"extra_body": {"enable_thinking": True}}

    def test_qwen_off_no_stream(self):
        use_stream, kw = build_thinking_kwargs("qwen", "off")
        assert use_stream is False
        assert kw == {"extra_body": {"enable_thinking": False}}

    def test_xai_no_stream_no_kwargs(self):
        use_stream, kw = build_thinking_kwargs("xai", None)
        assert use_stream is False
        assert kw == {}

    def test_unknown_provider_no_stream(self):
        use_stream, kw = build_thinking_kwargs("unknown", "high")
        assert use_stream is False
        assert kw == {}


class TestGetApiKeyEnvName:
    def test_gemini_provider(self):
        assert get_api_key_env_name("gemini") == "GOOGLE_API_KEY"

    def test_xai_provider(self):
        assert get_api_key_env_name("xai") == "XAI_API_KEY"

    def test_qwen_provider(self):
        assert get_api_key_env_name("qwen") == "DASHSCOPE_API_KEY"

    def test_unknown_provider_returns_first_registry_key(self):
        # Unknown providers fall back to the first registry entry (gemini)
        result = get_api_key_env_name("totally_unknown")
        assert result == "GOOGLE_API_KEY"


class TestStripJsonFences:
    def test_no_fence(self):
        assert strip_json_fences('{"a": 1}') == '{"a": 1}'

    def test_json_fence(self):
        raw = '```json\n{"a": 1}\n```'
        assert strip_json_fences(raw) == '{"a": 1}'

    def test_plain_fence(self):
        raw = '```\n{"a": 1}\n```'
        assert strip_json_fences(raw) == '{"a": 1}'

    def test_prose_surrounding_json(self):
        raw = 'Here is the JSON: {"a": 1} end'
        assert strip_json_fences(raw) == '{"a": 1}'

    def test_whitespace_stripped(self):
        assert strip_json_fences('  {"a": 1}  ') == '{"a": 1}'

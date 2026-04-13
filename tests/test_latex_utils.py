# -*- coding: utf-8 -*-
"""Smoke tests for eXercise.latex_utils — pure functions, no I/O."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eXercise.latex_utils import escape_question_text, latex_escape, sanitize_bullet


class TestLatexEscape:
    def test_plain_text_unchanged(self):
        assert latex_escape("hello world") == "hello world"

    def test_backslash_escaped(self):
        assert latex_escape("a\\b") == r"a\textbackslash{}b"

    def test_braces_escaped(self):
        assert latex_escape("{x}") == r"\{x\}"

    def test_dollar_escaped(self):
        assert latex_escape("$5") == r"\$5"

    def test_hash_escaped(self):
        assert latex_escape("#1") == r"\#1"

    def test_percent_escaped(self):
        assert latex_escape("50%") == r"50\%"

    def test_ampersand_escaped(self):
        assert latex_escape("A & B") == r"A \& B"

    def test_underscore_escaped(self):
        assert latex_escape("x_1") == r"x\_1"

    def test_tilde_escaped(self):
        assert latex_escape("~") == r"\textasciitilde{}"

    def test_caret_escaped(self):
        assert latex_escape("^") == r"\textasciicircum{}"

    def test_multiple_specials(self):
        result = latex_escape("100% & more")
        assert r"100\%" in result
        assert r"\&" in result

    def test_empty_string(self):
        assert latex_escape("") == ""


class TestSanitizeBullet:
    def test_plain_text_unchanged(self):
        assert sanitize_bullet("No special chars") == "No special chars"

    def test_em_dash_replaced(self):
        assert sanitize_bullet("a\u2014b") == "a---b"

    def test_en_dash_replaced(self):
        assert sanitize_bullet("a\u2013b") == "a--b"

    def test_left_single_quote_replaced(self):
        assert sanitize_bullet("\u2018hi") == "`hi"

    def test_right_single_quote_replaced(self):
        assert sanitize_bullet("it\u2019s") == "it's"

    def test_left_double_quote_replaced(self):
        assert sanitize_bullet("\u201chello") == "``hello"

    def test_right_double_quote_replaced(self):
        assert sanitize_bullet("bye\u201d") == "bye''"

    def test_multiplication_sign(self):
        assert sanitize_bullet("3\u00d74") == r"3$\times$4"

    def test_degree_sign(self):
        assert sanitize_bullet("45\u00b0") == r"45$^{\circ}$"

    def test_alpha(self):
        assert sanitize_bullet("\u03b1") == r"$\alpha$"

    def test_beta(self):
        assert sanitize_bullet("\u03b2") == r"$\beta$"

    def test_superscript_2(self):
        assert sanitize_bullet("m\u00b2") == r"m$^{2}$"

    def test_infinity(self):
        assert sanitize_bullet("\u221e") == r"$\infty$"

    def test_sqrt(self):
        assert sanitize_bullet("\u221a4") == r"$\sqrt{}$4"

    def test_approx(self):
        assert sanitize_bullet("\u2248") == r"$\approx$"

    def test_leq(self):
        assert sanitize_bullet("x\u2264y") == r"x$\leq$y"

    def test_geq(self):
        assert sanitize_bullet("x\u2265y") == r"x$\geq$y"

    def test_pm(self):
        assert sanitize_bullet("1\u00b1") == r"1$\pm$"

    def test_one_half(self):
        assert sanitize_bullet("\u00bd") == r"$\frac{1}{2}$"

    def test_empty_string(self):
        assert sanitize_bullet("") == ""


class TestEscapeQuestionText:
    def test_plain_text_escaped(self):
        # Special chars outside math are escaped
        assert escape_question_text("50% of") == r"50\% of"

    def test_inline_math_preserved(self):
        # $...$ regions must pass through unchanged
        result = escape_question_text("The force $F = ma$ is used.")
        assert "$F = ma$" in result

    def test_display_math_preserved(self):
        result = escape_question_text("$$E = mc^2$$")
        assert "$$E = mc^2$$" in result

    def test_mixed_math_and_text(self):
        result = escape_question_text("50% efficiency, $\\eta = 0.5$")
        # The % outside math should be escaped
        assert r"50\%" in result
        # The math region must survive intact
        assert r"$\eta = 0.5$" in result

    def test_no_math_no_specials(self):
        assert escape_question_text("hello world") == "hello world"

    def test_multiple_inline_math(self):
        result = escape_question_text("$a$ & $b$")
        assert "$a$" in result
        assert "$b$" in result
        assert r"\&" in result

    def test_empty_string(self):
        assert escape_question_text("") == ""

# -*- coding: utf-8 -*-
"""Function helper (call tip): enclosing-call detection, argument counting,
and the current-argument highlight."""

import unittest

from _rtl_plugin import rtl_autocomplete as ac

Controller = ac.CustomAutocompleteController


class EnclosingFunctionTests(unittest.TestCase):
    def test_finds_the_innermost_open_call(self):
        text = "outer(inner(a, b"
        name, open_index = Controller._enclosing_function(text, len(text))
        self.assertEqual(name, "inner")
        self.assertEqual(text[open_index], "(")

    def test_returns_nothing_once_the_call_is_closed(self):
        text = "color_cmyk(1, 2, 3, 4)"
        name, open_index = Controller._enclosing_function(text, len(text))
        self.assertEqual(name, "")
        self.assertEqual(open_index, -1)

    def test_returns_nothing_when_the_caret_is_not_inside_any_call(self):
        name, open_index = Controller._enclosing_function("1 + 2", 5)
        self.assertEqual(name, "")
        self.assertEqual(open_index, -1)

    def test_reopens_for_an_outer_call_after_a_nested_one_closes(self):
        text = "outer(inner(a, b), "
        name, open_index = Controller._enclosing_function(text, len(text))
        self.assertEqual(name, "outer")


class ArgumentIndexTests(unittest.TestCase):
    def test_counts_top_level_commas_only(self):
        text = "buffer(geometry(a, b), 5, "
        open_index = text.index("(")
        # The comma inside geometry(a, b) is nested - the whole call is
        # buffer's first argument, so the caret sits in argument index 2.
        self.assertEqual(Controller._argument_index(text, open_index, len(text)), 2)

    def test_ignores_a_comma_inside_a_quoted_string(self):
        text = "concat('a, b', "
        open_index = text.index("(")
        self.assertEqual(Controller._argument_index(text, open_index, len(text)), 1)

    def test_zero_for_the_first_argument(self):
        text = "lower("
        open_index = text.index("(")
        self.assertEqual(Controller._argument_index(text, open_index, len(text)), 0)


class SignatureHtmlTests(unittest.TestCase):
    def test_highlights_only_the_current_argument(self):
        html = Controller._signature_html("buffer", ["geometry", "distance", "segments"], 1)
        self.assertIn(">distance</span>", html)
        self.assertNotIn(">geometry</span>", html)
        self.assertNotIn(">segments</span>", html)
        self.assertIn("<b>buffer</b>", html)

    def test_uses_the_configured_highlight_colour(self):
        html = Controller._signature_html("lower", ["string"], 0)
        self.assertIn(Controller._CURRENT_ARG_COLOR, html)

    def test_a_function_with_no_parameters(self):
        html = Controller._signature_html("$geometry", [], 0)
        self.assertEqual(html, "<b>$geometry</b>()")


if __name__ == "__main__":
    unittest.main()

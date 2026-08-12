# -*- coding: utf-8 -*-
"""Selecting a word highlights other whole-word, case-insensitive matches -
never a substring - with the selected word shown more strongly than the rest.
"""

import unittest

from qgis.PyQt.QtGui import QTextCursor
from qgis.PyQt.QtWidgets import QPlainTextEdit

from src import rtl_editor as ed


class OccurrenceHighlighterTests(unittest.TestCase):
    def setUp(self):
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(
            '"NAME" = \'x\' OR "F_NAME_2" = \'y\' OR "name" = \'z\''
        )
        self.highlighter = ed.OccurrenceHighlighter(self.editor)

    def tearDown(self):
        self.highlighter.teardown()

    def _select(self, needle: str, occurrence: int = 0) -> None:
        content = self.editor.toPlainText()
        start = -1
        for _ in range(occurrence + 1):
            start = content.index(needle, start + 1)
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + len(needle), QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)

    def _highlighted_texts(self):
        return [sel.cursor.selectedText() for sel in self.editor.extraSelections()]

    def test_matches_are_whole_word_and_case_insensitive_never_a_substring(self):
        self._select("NAME")
        matched = self._highlighted_texts()
        # Exactly the two whole-word hits ("NAME" and "name") - never the
        # "NAME" that is a substring of "F_NAME_2".
        self.assertEqual(sorted(t.lower() for t in matched), ["name", "name"])

    def test_the_selected_occurrence_is_drawn_more_strongly_than_the_others(self):
        self._select("NAME", occurrence=0)
        selections = list(self.editor.extraSelections())
        self.assertGreaterEqual(len(selections), 2)
        backgrounds = [sel.format.background().color().alpha() for sel in selections]
        # One strong tint for the selection itself, weaker ones for the rest.
        self.assertEqual(len(set(backgrounds)), 2)
        self.assertEqual(max(backgrounds), backgrounds[0])

    def test_a_selection_with_only_one_match_highlights_nothing_extra(self):
        self.editor.setPlainText('"UNIQUE_WORD" = 1')
        self._select("UNIQUE_WORD")
        self.assertEqual(self.editor.extraSelections(), [])

    def test_a_short_or_multi_word_selection_is_ignored(self):
        self._select('"')  # single character
        self.assertEqual(self.editor.extraSelections(), [])


if __name__ == "__main__":
    unittest.main()

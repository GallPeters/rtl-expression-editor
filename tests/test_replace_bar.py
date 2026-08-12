# -*- coding: utf-8 -*-
"""Ctrl+H replace bar: Enter clicks Replace, Shift+Enter clicks Replace All -
identified from live sibling widgets, not a hard-coded layout."""

import unittest

from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtGui import QKeyEvent
from qgis.PyQt.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QPushButton

from src import rtl_editor as ed


def _build_bar():
    """A container standing in for QGIS's own find/replace bar."""
    container = QDialog()
    layout = QHBoxLayout(container)
    line_edit = QLineEdit(container)
    replace_btn = QPushButton(container)
    replace_all_btn = QPushButton(container)
    layout.addWidget(line_edit)
    layout.addWidget(replace_btn)
    layout.addWidget(replace_all_btn)
    return container, line_edit, replace_btn, replace_all_btn


class FindReplaceButtonLookupTests(unittest.TestCase):
    def test_finds_the_buttons_by_visible_text(self):
        _container, line_edit, replace_btn, replace_all_btn = _build_bar()
        replace_btn.setText("Replace")
        replace_all_btn.setText("Replace All")

        found_replace, found_all = ed.ReplaceBarShortcuts._find_replace_buttons(line_edit)
        self.assertIs(found_replace, replace_btn)
        self.assertIs(found_all, replace_all_btn)

    def test_finds_the_buttons_by_tooltip_when_icon_only(self):
        _container, line_edit, replace_btn, replace_all_btn = _build_bar()
        replace_btn.setToolTip("Replace")
        replace_all_btn.setToolTip("Replace all occurrences")

        found_replace, found_all = ed.ReplaceBarShortcuts._find_replace_buttons(line_edit)
        self.assertIs(found_replace, replace_btn)
        self.assertIs(found_all, replace_all_btn)

    def test_a_field_with_no_recognisable_buttons_matches_nothing(self):
        container = QDialog()
        line_edit = QLineEdit(container)
        found_replace, found_all = ed.ReplaceBarShortcuts._find_replace_buttons(line_edit)
        self.assertIsNone(found_replace)
        self.assertIsNone(found_all)


class EnterKeyDispatchTests(unittest.TestCase):
    def setUp(self):
        self.container, self.line_edit, self.replace_btn, self.replace_all_btn = _build_bar()
        self.replace_btn.setText("Replace")
        self.replace_all_btn.setText("Replace All")
        self.clicks = {"replace": 0, "all": 0}
        self.replace_btn.clicked.connect(lambda: self.clicks.__setitem__("replace", self.clicks["replace"] + 1))
        self.replace_all_btn.clicked.connect(lambda: self.clicks.__setitem__("all", self.clicks["all"] + 1))

        self.shortcuts = ed.ReplaceBarShortcuts(self.container)
        self.shortcuts._replace_btn = self.replace_btn
        self.shortcuts._replace_all_btn = self.replace_all_btn
        self.shortcuts._watched = self.line_edit

    def tearDown(self):
        self.shortcuts.teardown()

    def test_plain_enter_clicks_replace_only(self):
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        consumed = self.shortcuts.eventFilter(self.line_edit, event)
        self.assertTrue(consumed)
        self.assertEqual(self.clicks, {"replace": 1, "all": 0})

    def test_shift_enter_clicks_replace_all_only(self):
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        consumed = self.shortcuts.eventFilter(self.line_edit, event)
        self.assertTrue(consumed)
        self.assertEqual(self.clicks, {"replace": 0, "all": 1})

    def test_enter_is_always_consumed_even_without_a_matched_button(self):
        # Regression: this used to fall through to Qt's own "Enter clicks the
        # dialog's default button" behaviour whenever the lookup came up
        # empty, which could trigger the wrong button (Replace All).
        self.shortcuts._replace_btn = None
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        consumed = self.shortcuts.eventFilter(self.line_edit, event)
        self.assertTrue(consumed)
        self.assertEqual(self.clicks, {"replace": 0, "all": 0})

    def test_other_keys_are_left_alone(self):
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier)
        consumed = self.shortcuts.eventFilter(self.line_edit, event)
        self.assertFalse(consumed)
        self.assertEqual(self.clicks, {"replace": 0, "all": 0})


if __name__ == "__main__":
    unittest.main()

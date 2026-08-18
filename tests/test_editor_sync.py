# -*- coding: utf-8 -*-
"""The overlay editor: covers the real Scintilla widget completely and stays
bidirectionally in sync with it, for every editor class QGIS opens the
plugin's targeted dialogs with - QgsCodeEditorExpression stands in for the
Expression Builder and the Field Calculator's expression tab,
QgsCodeEditorSQL for the Layer Filter's SQL box.
"""

import unittest

from qgis.gui import QgsCodeEditorExpression, QgsCodeEditorSQL
from qgis.PyQt.QtWidgets import QApplication

from _rtl_plugin import rtl_editor as ed

from .utils import host_in_dialog


class _OverlaySyncCases:
    """Mixin: shared checks, run once per targeted editor class below."""

    editor_class = None

    def setUp(self):
        self.sci = self.editor_class()
        self.dialog = host_in_dialog(self.sci)
        self.dialog.show()
        QApplication.processEvents()
        self.overlay = ed.RtlOverlayEditor(self.sci)

    def tearDown(self):
        self.overlay.detach()
        self.dialog.close()

    def test_overlay_covers_the_editor_completely(self):
        self.assertEqual(self.overlay.geometry(), self.sci.rect())

    def test_scintilla_to_overlay_sync(self):
        self.sci.setText("hello")
        QApplication.processEvents()
        self.assertEqual(self.overlay.toPlainText(), "hello")

    def test_overlay_to_scintilla_sync(self):
        self.overlay.setPlainText("world")
        QApplication.processEvents()
        self.assertEqual(self.sci.text(), "world")

    def test_rtl_text_round_trips_unchanged(self):
        # The whole point of the plugin: mixed RTL/LTR text must survive the
        # round trip byte-for-byte, even though only its on-screen rendering
        # differs between the two widgets.
        text = 'שלום "COUNTRY" = \'ישראל\''
        self.overlay.setPlainText(text)
        QApplication.processEvents()
        self.assertEqual(self.sci.text(), text)
        self.sci.setText("")
        QApplication.processEvents()
        self.sci.setText(text)
        QApplication.processEvents()
        self.assertEqual(self.overlay.toPlainText(), text)

    def test_cursor_position_follows_from_scintilla_to_overlay(self):
        self.sci.setText("0123456789")
        QApplication.processEvents()
        self.sci.setCursorPosition(0, 4)
        QApplication.processEvents()
        self.assertEqual(self.overlay.textCursor().position(), 4)

    def test_detach_is_idempotent_and_restores_the_editor(self):
        self.overlay.detach()
        self.overlay.detach()  # must not raise the second time
        self.assertTrue(self.sci.isVisible() or not self.dialog.isVisible())


class ExpressionEditorSyncTests(_OverlaySyncCases, unittest.TestCase):
    """Expression Builder / Field Calculator expression tab."""

    editor_class = QgsCodeEditorExpression


class SqlEditorSyncTests(_OverlaySyncCases, unittest.TestCase):
    """Layer Filter's SQL box."""

    editor_class = QgsCodeEditorSQL


class CodeEditorWatcherTests(unittest.TestCase):
    """The application-wide detector that attaches an overlay the moment a
    targeted editor becomes visible - the actual mechanism QGIS's own dialogs
    trigger, as opposed to constructing RtlOverlayEditor by hand above."""

    def setUp(self):
        self.watcher = ed.CodeEditorWatcher()
        self.watcher.install()

    def tearDown(self):
        self.watcher.uninstall()

    def test_showing_a_targeted_editor_attaches_an_overlay(self):
        # overlay_count()/rescan() are console-diagnostic helpers that read
        # the single process-wide watcher (module-level _WATCHER, set by
        # RtlBidiEditorPlugin.initGui) - a watcher created directly here, as
        # the real plugin never does, is not that one. Its own _overlays
        # dict is the thing to check instead.
        before = len(self.watcher._overlays)
        sci = QgsCodeEditorExpression()
        dialog = host_in_dialog(sci)
        try:
            dialog.show()
            QApplication.processEvents()
            self.assertEqual(len(self.watcher._overlays), before + 1)
        finally:
            dialog.close()

    def test_uninstall_stops_attaching_new_overlays(self):
        self.watcher.uninstall()
        before = len(self.watcher._overlays)
        sci = QgsCodeEditorExpression()
        dialog = host_in_dialog(sci)
        try:
            dialog.show()
            QApplication.processEvents()
            self.assertEqual(len(self.watcher._overlays), before)
        finally:
            dialog.close()


class HideExpressionIdentityLineTests(unittest.TestCase):
    """RtlOverlayEditor.hide_expression_identity_line() - collapses the
    hidden expression-identity comment's own QTextBlock so it is not
    rendered, without touching the underlying text at all - still exactly
    what gets pushed to Scintilla and saved (see
    CustomAutocompleteController._ensure_eid())."""

    def setUp(self):
        self.sci = QgsCodeEditorExpression()
        self.dialog = host_in_dialog(self.sci)
        self.dialog.show()
        QApplication.processEvents()
        self.overlay = ed.RtlOverlayEditor(self.sci)

    def tearDown(self):
        self.overlay.detach()
        self.dialog.close()

    def test_the_id_comments_own_block_is_hidden(self):
        from _rtl_plugin.rtl_readmode import make_eid_comment

        text = make_eid_comment("0123456789abcdef") + '"F_ATT" = 610'
        self.overlay.setPlainText(text)
        QApplication.processEvents()
        self.overlay.hide_expression_identity_line()

        self.assertFalse(self.overlay.document().firstBlock().isVisible())
        # The text itself is completely untouched - still exactly what is
        # pushed to Scintilla and saved.
        self.assertEqual(self.overlay.toPlainText(), text)
        self.assertEqual(self.sci.text(), text)

    def test_plain_text_with_no_id_comment_is_left_fully_visible(self):
        self.overlay.setPlainText('"F_ATT" = 610')
        QApplication.processEvents()
        self.overlay.hide_expression_identity_line()

        self.assertTrue(self.overlay.document().firstBlock().isVisible())

    def test_reapplies_automatically_after_a_full_resync_from_scintilla(self):
        """A full document replace (any Scintilla -> overlay push) resets
        every QTextBlock's own visibility - _pull_text_from_sci() must
        re-hide the id line every time, not just once at attach."""
        from _rtl_plugin.rtl_readmode import make_eid_comment

        text = make_eid_comment("0123456789abcdef") + '"F_ATT" = 610'
        self.sci.setText(text)
        QApplication.processEvents()

        self.assertFalse(self.overlay.document().firstBlock().isVisible())


if __name__ == "__main__":
    unittest.main()

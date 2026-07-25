# -*- coding: utf-8 -*-

"""
Companion editor dialog.

This is the floating Qt editor window. It intentionally uses only native Qt
widgets and does not use QScintilla.

Important Qt6 note:
QPlainTextEdit does not have setDragDropMode(). Drag and drop is enabled
through QWidget/QAbstractScrollArea acceptDrops behavior.
"""

from PyQt6.QtCore import Qt, QSettings, QTimer, pyqtSignal
from PyQt6.QtGui import QTextCursor, QTextOption
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QVBoxLayout,
    QApplication,
)

from .utils import is_deleted, log_message


class CompanionEditorDialog(QDialog):
    """
    Floating companion editor.

    Signals:
        applyRequested:
            The user clicked Apply.
        okRequested:
            The user clicked OK.
        cancelRequested:
            The user clicked Cancel.
        closed:
            The window was closed. The argument is a close reason string:
            "ok", "cancel", "original", "shutdown".
    """

    applyRequested = pyqtSignal()
    okRequested = pyqtSignal()
    cancelRequested = pyqtSignal()
    closed = pyqtSignal(str)

    def __init__(self, parent, title, settings_key):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        try:
            flags |= Qt.WindowType.WindowMinMaxButtonsHint
        except AttributeError:
            pass

        # If the original QGIS dialog is modal, a non-modal child tool window
        # can sometimes be hidden behind it or handled poorly by the WM.
        # Use top-most only in that case.
        try:
            modal_parent = False

            if parent is not None and not is_deleted(parent):
                modal_parent = (
                    bool(parent.isModal())
                    or parent.windowModality() != Qt.WindowModality.NonModal
                )

            if modal_parent:
                flags |= Qt.WindowType.WindowStaysOnTopHint
        except Exception:
            pass

        super().__init__(parent, flags)

        # Used by the monitor to avoid attaching companions to companions.
        self.setProperty("rtl_editor_companion", True)
        self.setObjectName("RtlEditorCompanionDialog")

        self._settings_key = settings_key
        self._close_reason = "cancel"

        self.setWindowTitle(f"RTL Editor - {title}")

        # We manage deletion explicitly.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # Do not steal focus when the companion appears.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(self)
        layout.setObjectName("RtlEditorCompanionLayout")

        self.editor = QPlainTextEdit(self)
        self.editor.setObjectName("RtlCompanionPlainTextEdit")

        self.editor.setUndoRedoEnabled(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setTabChangesFocus(False)
        self.editor.setCenterOnScroll(True)
        self.editor.setPlaceholderText(
            "Edit QGIS expression/filter text here.\n"
            "Hebrew, Arabic, Persian, Urdu and mixed RTL/LTR text should render correctly."
        )

        # Enable drag and drop correctly for QPlainTextEdit.
        # Do NOT call setDragDropMode(); it does not exist on QPlainTextEdit.
        self.editor.setAcceptDrops(True)

        try:
            self.editor.viewport().setAcceptDrops(True)
        except Exception:
            pass

        try:
            self.editor.setTabStopDistance(
                self.editor.fontMetrics().horizontalAdvance(" ") * 4
            )
        except Exception:
            pass

        self._configure_bidi()

        layout.addWidget(self.editor, 1)

        self.button_box = QDialogButtonBox(self)
        self.button_box.setObjectName("RtlCompanionButtonBox")
        self.button_box.setStandardButtons(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.clicked.connect(self._button_clicked)

        layout.addWidget(self.button_box, 0)

        self.resize(720, 420)
        self._restore_geometry()

    def _configure_bidi(self):
        """
        Enable automatic bidirectional text handling.

        We do not force RTL globally. Qt's text layout engine performs Unicode
        BiDi processing. Setting LayoutDirectionAuto allows paragraph direction
        to be inferred from the text.
        """
        try:
            document = self.editor.document()
            option = document.defaultTextOption()

            try:
                option.setTextDirection(Qt.LayoutDirection.LayoutDirectionAuto)
            except AttributeError:
                # If LayoutDirectionAuto is unavailable, leave Qt defaults.
                pass

            try:
                option.setWrapMode(
                    QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
                )
            except AttributeError:
                pass

            document.setDefaultTextOption(option)
        except Exception:
            # Never fail because of optional text-option behavior.
            pass

    def set_text(self, text):
        """Replace companion text without preserving cursor position."""
        self.editor.setPlainText(text or "")

    def text(self):
        """Return companion text."""
        return self.editor.toPlainText()

    def set_text_preserve_cursor(self, text):
        """
        Replace companion text while preserving cursor position as much as
        possible.
        """
        if self.text() == text:
            return

        cursor = self.editor.textCursor()
        position = cursor.position()
        anchor = cursor.anchor()

        self.editor.setPlainText(text or "")

        length = len(self.text())
        position = min(position, length)
        anchor = min(anchor, length)

        new_cursor = self.editor.textCursor()
        new_cursor.setPosition(anchor)
        new_cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)

        self.editor.setTextCursor(new_cursor)

    def set_close_reason(self, reason):
        """Set the reason that will be emitted by closed()."""
        self._close_reason = reason

    def _button_clicked(self, button):
        """Translate QDialogButtonBox clicks into semantic signals."""
        role = self.button_box.buttonRole(button)

        if role == QDialogButtonBox.ButtonRole.ApplyRole:
            self._close_reason = "apply"
            self.applyRequested.emit()
        elif role == QDialogButtonBox.ButtonRole.AcceptRole:
            self._close_reason = "ok"
            self.okRequested.emit()
        elif role == QDialogButtonBox.ButtonRole.RejectRole:
            self._close_reason = "cancel"
            self.cancelRequested.emit()

    def showEvent(self, event):
        """
        Ensure the companion becomes visible.

        raise_() is used instead of activateWindow() so we do not steal
        keyboard focus unnecessarily.
        """
        super().showEvent(event)

        try:
            QTimer.singleShot(
                0,
                lambda: self.raise_() if not is_deleted(self) else None,
            )
        except Exception:
            pass

        try:
            log_message(f"Companion editor shown: {self.windowTitle()}")
        except Exception:
            pass

    def closeEvent(self, event):
        """Save geometry and notify the controller."""
        self._save_geometry()
        self.closed.emit(self._close_reason)
        super().closeEvent(event)

    def _settings(self):
        return QSettings("QGIS RTL Editor", "Companion Editor")

    def _restore_geometry(self):
        try:
            settings = self._settings()
            data = settings.value(f"geometry/{self._settings_key}")
            if data is not None:
                self.restoreGeometry(data)

            if not self._is_on_screen():
                self.resize(720, 420)

                app = QApplication.instance()
                if app is not None:
                    screens = app.screens()
                    if screens:
                        available = screens[0].availableGeometry()
                        self.move(available.x() + 80, available.y() + 80)
        except Exception:
            pass

    def _save_geometry(self):
        try:
            settings = self._settings()
            settings.setValue(f"geometry/{self._settings_key}", self.saveGeometry())
        except Exception:
            pass

    def _is_on_screen(self):
        """
        Return True when the current window center is inside any available
        screen geometry.
        """
        app = QApplication.instance()
        if app is None:
            return True

        try:
            screens = app.screens()
        except Exception:
            return True

        if not screens:
            return True

        try:
            center = self.frameGeometry().center()
        except Exception:
            return True

        for screen in screens:
            try:
                if screen.availableGeometry().contains(center):
                    return True
            except Exception:
                pass

        return False
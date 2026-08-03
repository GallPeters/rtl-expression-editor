# -*- coding: utf-8 -*-
"""
RTL / BiDi Code Editor for QGIS 4 (Qt6)
=======================================

Problem
-------
QGIS embeds QScintilla (Scintilla) in its code editors, most visibly in the
Expression Builder (``QgsCodeEditorExpression``) and the Layer Filter / Query
Builder (``QgsCodeEditorSQL``).  Scintilla's text layout engine has no real
Unicode bidirectional support, so a mixed Hebrew/English or Arabic/English
expression is rendered with overlapping glyphs and a caret that jumps to the
wrong visual place.  The expression itself is stored correctly - only the
*rendering* is broken - which is exactly why an overlay is a valid fix.

Solution
--------
Leave QGIS completely untouched.  Whenever a targeted ``QgsCodeEditor`` becomes
visible, create a ``QPlainTextEdit`` **as a child of that editor**, sized to the
editor's ``rect()``.  Qt's text engine (HarfBuzz + the Unicode BiDi algorithm)
renders mixed-direction text correctly out of the box.  The two widgets are
kept in two-way synchronisation, so from QGIS's point of view nothing changed.

Why a *child* widget rather than a floating overlay window
----------------------------------------------------------
Because a child widget's geometry is expressed in its parent's coordinate
system, the overlay automatically follows the original editor when the dialog
is moved, resized, re-laid-out, docked, moved to another screen or rescaled by
a DPI change.  The only thing we have to react to is the parent's own
``Resize`` event.  No timers, no polling, no global coordinate maths, and
nothing can ever drift out of alignment.

Why overlay rather than replace
-------------------------------
The C++ side of QGIS keeps pointers to the Scintilla editor and connects
signals to it (validation, preview, "insert field/function/operator", ...).
Removing it from the layout risks dangling pointers, and merely hiding it would
drop it from the layout and collapse the space we need to cover.  Keeping it
alive, fully functional and simply *covered* is both safer and simpler.

Architecture
------------
``RtlBidiEditorPlugin``   - plugin lifecycle; installs/removes the watcher.
``CodeEditorWatcher``     - one application-wide event filter; detects targeted
                            editors on ``QEvent.Show`` and attaches overlays.
``RtlOverlayEditor``      - the overlay widget: geometry, appearance, two-way
                            synchronisation, completion.
``BidiSyntaxHighlighter`` - QSyntaxHighlighter replicating QScintilla's colours.

Author: Your Name
License: GPL-2.0-or-later
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QEvent, QObject, QStringListModel, Qt, QTimer
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
)
from qgis.PyQt.QtWidgets import QApplication, QCompleter, QPlainTextEdit, QWidget

from qgis.core import Qgis, QgsExpression, QgsMessageLog

# --------------------------------------------------------------------------- #
# Optional / defensive imports
# --------------------------------------------------------------------------- #
try:
    from qgis.gui import QgsCodeEditor
except ImportError:  # pragma: no cover - should never happen in a QGIS runtime
    QgsCodeEditor = None

try:
    from qgis.gui import QgsCodeEditorColorScheme
except ImportError:  # pragma: no cover
    QgsCodeEditorColorScheme = None

try:
    # Used only as a fallback detector, so that we still find the editor even
    # if QGIS renames its QgsCodeEditor* subclasses.
    from qgis.PyQt.Qsci import QsciScintillaBase
except ImportError:  # pragma: no cover
    QsciScintillaBase = None


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

LOG_TAG = "RTL BiDi Editor"

#: Editor classes we take over, matched against the C++ QMetaObject
#: inheritance chain (see CodeEditorWatcher._meta_class_names for why the
#: Python type cannot be used).
TARGET_EDITOR_CLASSES = frozenset(
    """
    QgsCodeEditorExpression
    QgsCodeEditorSQL
    """.split()
)

#: Editors we must never take over, even though they are QgsCodeEditor
#: subclasses: their content is not an expression, and Scintilla's own
#: behaviour there is either irrelevant or actively wanted.
EXCLUDED_EDITOR_CLASSES = frozenset(
    """
    QgsCodeEditorPython
    QgsCodeEditorHTML
    QgsCodeEditorCSS
    QgsCodeEditorJavascript
    QgsCodeEditorJson
    QgsCodeEditorR
    QgsCodeEditorShell
    """.split()
)

#: Object names that identify non-expression editors.  Used by the generic
#: fallback, which only sees "QsciScintilla" and must discriminate some other
#: way.  ``txtPython`` is the Function Editor's Python pane and
#: ``mFunctionBuilderHelp`` is its read-only help view - both live in the Field
#: Calculator alongside the real expression editor.
EXCLUDED_OBJECT_NAMES = frozenset(
    """
    txtPython
    mFunctionBuilderHelp
    mHelpText
    txtHelpText
    txtPreview
    """.split()
)

#: Windows whose editors are off-limits, matched as substrings against the
#: window's class name and object name.
EXCLUDED_WINDOW_TOKENS = ("PythonConsole", "ScriptEditor", "QgsPythonScript")

#: Fallback detection.  If a *window* whose class name appears here is shown,
#: every Scintilla-based widget inside it is taken over, regardless of its own
#: class name.  This makes the plugin survive a QGIS-side class rename, and it
#: catches editors that are wrapped in a container (QGIS >= 3.38 wraps the
#: expression editor in a QgsCodeEditorWidget that adds a search bar).
#:
#: Declared as one whitespace-separated string rather than a set of literals:
#: these are class *names* compared as text, never resolved as symbols, and a
#: single string cannot be broken by a lost quote during copy/paste.
TARGET_DIALOG_CLASSES = frozenset(
    """
    QgsExpressionBuilderDialog
    QgsExpressionSelectionDialog
    QgsQueryBuilder
    QgsQueryBuilderDialog
    QgsSubsetStringEditorDialog
    QgsFieldCalculator
    """.split()
)

#: Last-resort detection: attach to any editable Scintilla widget that lives in
#: a dialog and is not excluded above, even when no QGIS class name can be
#: resolved at all.  Set to False to require a positive class-name match.
ENABLE_GENERIC_FALLBACK = True

#: Object name of the overlay.  Also used as an idempotency marker so a plugin
#: reload can never attach two overlays to the same editor.
OVERLAY_OBJECT_NAME = "rtlBidiOverlayEditor"

#: Verbose logging.  Leave on until the plugin is confirmed working on your
#: build; it makes every detection decision visible in the Log Messages panel.
DEBUG = True

ENABLE_HIGHLIGHTING = True
ENABLE_COMPLETER = True

#: Minimum prefix length before the completion popup appears.
COMPLETER_MIN_PREFIX = 2


def _log(message: str, level=Qgis.MessageLevel.Warning) -> None:
    """Log to the QGIS message log; never raise from within a log call."""
    try:
        QgsMessageLog.logMessage(message, LOG_TAG, level)
    except Exception:  # pragma: no cover
        pass


def _dbg(message: str) -> None:
    """Log only when DEBUG is enabled."""
    if DEBUG:
        _log(message, Qgis.MessageLevel.Info)


# --------------------------------------------------------------------------- #
# Text position helpers
#
# QScintilla addresses text as (line, index) pairs.  QPlainTextEdit uses a flat
# character offset.  QsciScintilla's getCursorPosition() / setCursorPosition() /
# getSelection() / setSelection() take *character* indices (QScintilla converts
# to and from Scintilla's UTF-8 byte offsets internally via SCI_POSITIONAFTER),
# so no manual byte arithmetic is required here.  That matters a lot for
# Hebrew/Arabic, where every character is two UTF-8 bytes.
# --------------------------------------------------------------------------- #


def _offset_from_line_index(text: str, line: int, index: int) -> int:
    """Convert a QScintilla (line, index) pair into a flat character offset."""
    if line <= 0:
        return max(0, min(index, len(text)))
    pos = -1
    for _ in range(line):
        nxt = text.find("\n", pos + 1)
        if nxt == -1:
            # Fewer lines than requested (can happen transiently mid-edit).
            return len(text)
        pos = nxt
    return max(0, min(pos + 1 + index, len(text)))


def _line_index_from_offset(text: str, offset: int) -> Tuple[int, int]:
    """Convert a flat character offset into a QScintilla (line, index) pair."""
    offset = max(0, min(offset, len(text)))
    head = text[:offset]
    line = head.count("\n")
    index = offset - (head.rfind("\n") + 1)
    return line, index


def _normalise_eol(text: str) -> str:
    """Collapse CRLF / CR to LF.

    QPlainTextEdit always stores ``\\n``.  Scintilla may use the platform EOL
    mode, so we normalise on the way in and write ``\\n`` on the way out (the
    overlay also forces Unix EOL mode on the editor, so in practice this is
    just a belt-and-braces measure).
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------- #
# Colour scheme bridge
# --------------------------------------------------------------------------- #


def _scheme_color(role_name: str, fallback: str) -> QColor:
    """Return a colour from the user's active QGIS code-editor colour scheme.

    Falls back to a sane hard-coded value when the role does not exist in this
    QGIS version, so the plugin never breaks on an enum rename.
    """
    if QgsCodeEditor is not None and QgsCodeEditorColorScheme is not None:
        try:
            roles = getattr(QgsCodeEditorColorScheme, "ColorRole", QgsCodeEditorColorScheme)
            role = getattr(roles, role_name, None)
            if role is not None:
                color = QgsCodeEditor.color(role)
                if isinstance(color, QColor) and color.isValid():
                    return color
        except Exception:
            pass
    return QColor(fallback)


# --------------------------------------------------------------------------- #
# Syntax highlighting
# --------------------------------------------------------------------------- #


class BidiSyntaxHighlighter(QSyntaxHighlighter):
    """Lightweight highlighter mirroring QScintilla's expression / SQL lexers.

    Rules are applied in order and later rules overwrite earlier ones, which is
    how strings and comments end up winning over keywords that appear inside
    them.  Multi-line ``/* ... */`` comments use the block state machine.
    """

    _EXPRESSION_KEYWORDS = [
        "AND", "OR", "NOT", "IN", "LIKE", "ILIKE", "IS", "NULL", "BETWEEN",
        "CASE", "WHEN", "THEN", "ELSE", "END", "TRUE", "FALSE",
    ]

    _SQL_KEYWORDS = _EXPRESSION_KEYWORDS + [
        "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "LIMIT",
        "OFFSET", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "ON",
        "AS", "DISTINCT", "UNION", "ALL", "EXISTS", "ASC", "DESC", "WITH",
    ]

    def __init__(self, document, dialect: str = "expression"):
        super().__init__(document)
        self._dialect = dialect
        self._rules: List[Tuple[re.Pattern, QTextCharFormat]] = []
        self._block_comment_format = QTextCharFormat()
        self.rebuild()

    # -- construction ------------------------------------------------------ #

    @staticmethod
    def _fmt(color: QColor, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        if italic:
            fmt.setFontItalic(True)
        return fmt

    def rebuild(self) -> None:
        """(Re)build the rule table from the current colour scheme."""
        kw_fmt = self._fmt(_scheme_color("Keyword", "#8959a8"), bold=True)
        fn_fmt = self._fmt(_scheme_color("Method", "#4271ae"))
        num_fmt = self._fmt(_scheme_color("Number", "#f5871f"))
        str_fmt = self._fmt(_scheme_color("SingleQuote", "#718c00"))
        ident_fmt = self._fmt(_scheme_color("DoubleQuote", "#c82829"))
        var_fmt = self._fmt(_scheme_color("Decoration", "#3e999f"))
        cmt_fmt = self._fmt(_scheme_color("Comment", "#8e908c"), italic=True)
        self._block_comment_format = cmt_fmt

        keywords = (
            self._SQL_KEYWORDS if self._dialect == "sql" else self._EXPRESSION_KEYWORDS
        )
        kw_re = r"\b(?:%s)\b" % "|".join(sorted(keywords, key=len, reverse=True))

        rules: List[Tuple[str, QTextCharFormat]] = [
            # Numbers (integer, decimal, scientific).
            (r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", num_fmt),
            # Keywords / logical operators (case-insensitive, handled below).
            (kw_re, kw_fmt),
            # Function calls: an identifier immediately followed by '('.
            (r"\b[A-Za-z_][A-Za-z0-9_]*(?=\s*\()", fn_fmt),
            # Expression variables (@atlas_feature) and legacy $-variables.
            (r"[@$][A-Za-z_][A-Za-z0-9_]*", var_fmt),
            # Double-quoted identifiers = field names.
            (r'"(?:[^"\\]|\\.)*"?', ident_fmt),
            # Single-quoted string literals.
            (r"'(?:[^'\\]|\\.)*'?", str_fmt),
            # Single-line comments.
            (r"--[^\n]*", cmt_fmt),
        ]

        self._rules = [
            (re.compile(pattern, re.IGNORECASE), fmt) for pattern, fmt in rules
        ]

    # -- QSyntaxHighlighter ------------------------------------------------ #

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt override)
        try:
            for pattern, fmt in self._rules:
                for match in pattern.finditer(text):
                    if match.end() > match.start():
                        self.setFormat(match.start(), match.end() - match.start(), fmt)
            self._highlight_block_comments(text)
        except Exception as exc:  # highlighting must never break editing
            _log(f"Highlighting failed: {exc}", Qgis.MessageLevel.Info)

    def _highlight_block_comments(self, text: str) -> None:
        """Handle ``/* ... */`` comments spanning multiple blocks."""
        start = 0
        in_comment = self.previousBlockState() == 1
        self.setCurrentBlockState(0)

        while start <= len(text):
            if in_comment:
                end = text.find("*/", start)
                if end == -1:
                    self.setFormat(start, len(text) - start, self._block_comment_format)
                    self.setCurrentBlockState(1)
                    return
                self.setFormat(start, end + 2 - start, self._block_comment_format)
                start = end + 2
                in_comment = False
            else:
                begin = text.find("/*", start)
                if begin == -1:
                    return
                start = begin
                in_comment = True


# --------------------------------------------------------------------------- #
# The overlay editor
# --------------------------------------------------------------------------- #


class RtlOverlayEditor(QPlainTextEdit):
    """A bidi-capable editor that covers - and mirrors - a ``QgsCodeEditor``.

    ``QPlainTextEdit`` is used rather than ``QTextEdit`` because the content is
    plain text: it has a much cheaper document layout, no rich-text paste
    surprises, and the same full BiDi support (both use ``QTextDocument`` /
    ``QTextLayout`` underneath).

    Synchronisation contract
    ------------------------
    * A single ``_syncing`` re-entrancy guard protects both directions, which
      is what prevents infinite update loops.
    * Overlay -> Scintilla: full text push plus cursor/selection push.  The
      cursor push is essential: every QGIS "insert field / function / operator"
      action inserts at *Scintilla's* caret, so the caret must always mirror
      the one the user sees.
    * Scintilla -> Overlay: the text is replaced inside a single
      ``QTextCursor`` edit block (rather than ``setPlainText``) so the overlay's
      undo history survives, then the caret is read back from Scintilla.  This
      one path transparently covers *every* insertion mechanism QGIS has, now
      or in the future, because all of them ultimately mutate the Scintilla
      document and emit ``textChanged``.
    """

    def __init__(self, sci: QWidget):
        super().__init__(sci)
        self._sci = sci
        self._syncing = False
        self._detached = False
        self._highlighter: Optional[BidiSyntaxHighlighter] = None
        self._completer: Optional[QCompleter] = None

        cls_names = {klass.__name__ for klass in type(sci).__mro__}
        self._dialect = "sql" if "QgsCodeEditorSQL" in cls_names else "expression"

        self.setObjectName(OVERLAY_OBJECT_NAME)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setUndoRedoEnabled(True)
        # Qt applies the Unicode BiDi algorithm per paragraph and picks the
        # paragraph direction from its first strong character - exactly the
        # behaviour users expect from a mixed Hebrew/English expression.
        # Qt's built-in context menu also offers the "Insert Unicode control
        # character" submenu (RLM/LRM/RLE/PDF...), which is genuinely useful
        # here, so we keep the standard menu.
        option = self.document().defaultTextOption()
        option.setFlags(option.flags() | QTextOption.Flag.IncludeTrailingSpaces)
        self.document().setDefaultTextOption(option)

        self._apply_appearance()
        self._install_highlighter()
        self._install_completer()

        # Normalise Scintilla's EOL handling so both documents agree.
        try:
            eol_unix = getattr(type(sci), "EolUnix", None)
            if eol_unix is None:
                eol_mode = getattr(type(sci), "EolMode", None)
                eol_unix = getattr(eol_mode, "EolUnix", None) if eol_mode else None
            if eol_unix is not None:
                sci.setEolMode(eol_unix)
        except Exception:
            pass  # purely cosmetic; the normaliser below still protects us

        self._pull_text_from_sci(initial=True)
        self._connect_signals()

        # Forward programmatic focus (QGIS calls setFocus() on the editor).
        try:
            sci.setFocusProxy(self)
        except Exception as exc:
            _log(f"Could not install focus proxy: {exc}", Qgis.MessageLevel.Info)

        sci.installEventFilter(self)
        self._sync_geometry()
        self.show()
        self.raise_()

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    def _connect_signals(self) -> None:
        sci = self._sci
        # Scintilla -> overlay
        sci.textChanged.connect(self._on_sci_text_changed)
        sci.cursorPositionChanged.connect(self._on_sci_cursor_changed)
        sci.selectionChanged.connect(self._on_sci_cursor_changed)
        # Overlay -> Scintilla
        self.textChanged.connect(self._on_overlay_text_changed)
        self.cursorPositionChanged.connect(self._on_overlay_cursor_changed)
        self.selectionChanged.connect(self._on_overlay_cursor_changed)

    def _disconnect_signals(self) -> None:
        for signal, slot in (
            (self._sci.textChanged, self._on_sci_text_changed),
            (self._sci.cursorPositionChanged, self._on_sci_cursor_changed),
            (self._sci.selectionChanged, self._on_sci_cursor_changed),
            (self.textChanged, self._on_overlay_text_changed),
            (self.cursorPositionChanged, self._on_overlay_cursor_changed),
            (self.selectionChanged, self._on_overlay_cursor_changed),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _apply_appearance(self) -> None:
        """Copy font, frame, colours and tab width from the original editor."""
        try:
            font: Optional[QFont] = None
            lexer = self._sci.lexer() if hasattr(self._sci, "lexer") else None
            if lexer is not None:
                try:
                    font = lexer.defaultFont()
                except Exception:
                    font = None
            if font is None or not font.family():
                font = self._sci.font()
            if (font is None or not font.family()) and QgsCodeEditor is not None:
                font = QgsCodeEditor.getMonospaceFont()
            if font is not None:
                self.setFont(font)
                self.setTabStopDistance(
                    4.0 * QFontMetricsF(font).horizontalAdvance(" ")
                )

            # Match the frame so the swap is visually invisible.
            self.setFrameStyle(self._sci.frameStyle())

            palette = self.palette()
            palette.setColor(
                QPalette.ColorRole.Base, _scheme_color("Background", "#ffffff")
            )
            palette.setColor(QPalette.ColorRole.Text, _scheme_color("Default", "#000000"))
            palette.setColor(
                QPalette.ColorRole.Highlight,
                _scheme_color("SelectionBackground", "#308cc6"),
            )
            palette.setColor(
                QPalette.ColorRole.HighlightedText,
                _scheme_color("SelectionForeground", "#ffffff"),
            )
            self.setPalette(palette)

            self.setReadOnly(bool(self._sci.isReadOnly()))
            self.setEnabled(self._sci.isEnabled())
        except Exception as exc:
            _log(f"Could not fully mirror editor appearance: {exc}", Qgis.MessageLevel.Info)

    def _install_highlighter(self) -> None:
        if not ENABLE_HIGHLIGHTING:
            return
        try:
            self._highlighter = BidiSyntaxHighlighter(self.document(), self._dialect)
        except Exception as exc:
            self._highlighter = None
            _log(f"Syntax highlighting disabled: {exc}", Qgis.MessageLevel.Info)

    def _install_completer(self) -> None:
        """Provide word completion (QScintilla's own popup cannot be reused).

        Sources, all best-effort and individually guarded:
          * every registered QgsExpression function name;
          * the field names of the layer owned by an ancestor widget that
            exposes a ``layer()`` accessor (the Expression Builder does).
        """
        if not ENABLE_COMPLETER:
            return
        try:
            words = set()

            try:
                for function in QgsExpression.Functions():
                    name = function.name()
                    if name and not name.startswith("_"):
                        words.add(name)
            except Exception:
                pass

            for field_name in self._harvest_field_names():
                words.add(f'"{field_name}"')

            if not words:
                return

            completer = QCompleter(self)
            completer.setModel(QStringListModel(sorted(words), completer))
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setWidget(self)
            completer.activated[str].connect(self._insert_completion)
            self._completer = completer
        except Exception as exc:
            self._completer = None
            _log(f"Completion disabled: {exc}", Qgis.MessageLevel.Info)

    def _harvest_field_names(self) -> List[str]:
        """Walk up the parent chain looking for a widget exposing a layer."""
        names: List[str] = []
        widget = self._sci.parentWidget()
        depth = 0
        while widget is not None and depth < 8:
            layer_getter = getattr(widget, "layer", None)
            if callable(layer_getter):
                try:
                    layer = layer_getter()
                    if layer is not None and hasattr(layer, "fields"):
                        names = [field.name() for field in layer.fields()]
                        break
                except Exception:
                    pass
            widget = widget.parentWidget()
            depth += 1
        return names

    # ------------------------------------------------------------------ #
    # Geometry
    # ------------------------------------------------------------------ #

    def _sync_geometry(self) -> None:
        """Cover the parent editor completely.

        Because we are a child widget, ``rect()`` is all we need - moves, DPI
        changes and screen changes are handled by Qt's parent/child geometry
        propagation.
        """
        if self._detached or self._sci is None:
            return
        try:
            self.setGeometry(self._sci.rect())
            self.raise_()
        except RuntimeError:
            # Underlying C++ object already deleted.
            self._detached = True

    # ------------------------------------------------------------------ #
    # Synchronisation: Scintilla -> overlay
    # ------------------------------------------------------------------ #

    def _on_sci_text_changed(self) -> None:
        if self._syncing or self._detached:
            return
        self._pull_text_from_sci()

    def _on_sci_cursor_changed(self, *_args) -> None:
        if self._syncing or self._detached:
            return
        self._syncing = True
        try:
            self._pull_cursor_from_sci()
        except Exception as exc:
            _log(f"Cursor pull failed: {exc}", Qgis.MessageLevel.Info)
        finally:
            self._syncing = False

    def _pull_text_from_sci(self, initial: bool = False) -> None:
        self._syncing = True
        try:
            new_text = _normalise_eol(self._sci.text())
            if new_text != self.toPlainText():
                cursor = self.textCursor()
                cursor.beginEditBlock()
                cursor.select(QTextCursor.SelectionType.Document)
                cursor.insertText(new_text)
                cursor.endEditBlock()
                if initial:
                    # A fresh document should not start with a pending undo.
                    self.document().clearUndoRedoStacks()
            self._pull_cursor_from_sci()
        except RuntimeError:
            self._detached = True
        except Exception as exc:
            _log(f"Text pull failed: {exc}", Qgis.MessageLevel.Warning)
        finally:
            self._syncing = False

    def _pull_cursor_from_sci(self) -> None:
        """Mirror Scintilla's caret/selection into the overlay.

        This is what makes button-driven insertions land in the right visual
        place: QGIS inserts at the Scintilla caret and then advances it; we
        simply follow.
        """
        text = self.toPlainText()
        cursor = self.textCursor()

        line_from, index_from, line_to, index_to = self._sci.getSelection()
        if line_from == -1:
            line, index = self._sci.getCursorPosition()
            cursor.setPosition(_offset_from_line_index(text, line, index))
        else:
            cursor.setPosition(_offset_from_line_index(text, line_from, index_from))
            cursor.setPosition(
                _offset_from_line_index(text, line_to, index_to),
                QTextCursor.MoveMode.KeepAnchor,
            )
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    # ------------------------------------------------------------------ #
    # Synchronisation: overlay -> Scintilla
    # ------------------------------------------------------------------ #

    def _on_overlay_text_changed(self) -> None:
        if self._syncing or self._detached:
            return
        self._syncing = True
        try:
            text = self.toPlainText()
            if _normalise_eol(self._sci.text()) != text:
                # setText() emits Scintilla's textChanged, which is what drives
                # QGIS validation / preview / the OK button state.  Our guard
                # stops the echo from coming back to us.
                self._sci.setText(text)
            self._push_cursor_to_sci()
        except RuntimeError:
            self._detached = True
        except Exception as exc:
            _log(f"Text push failed: {exc}", Qgis.MessageLevel.Warning)
        finally:
            self._syncing = False

    def _on_overlay_cursor_changed(self) -> None:
        if self._syncing or self._detached:
            return
        self._syncing = True
        try:
            self._push_cursor_to_sci()
        except RuntimeError:
            self._detached = True
        except Exception as exc:
            _log(f"Cursor push failed: {exc}", Qgis.MessageLevel.Info)
        finally:
            self._syncing = False

    def _push_cursor_to_sci(self) -> None:
        text = self.toPlainText()
        cursor = self.textCursor()
        if cursor.hasSelection():
            line_from, index_from = _line_index_from_offset(text, cursor.selectionStart())
            line_to, index_to = _line_index_from_offset(text, cursor.selectionEnd())
            self._sci.setSelection(line_from, index_from, line_to, index_to)
        else:
            line, index = _line_index_from_offset(text, cursor.position())
            self._sci.setCursorPosition(line, index)

    # ------------------------------------------------------------------ #
    # Completion
    # ------------------------------------------------------------------ #

    _WORD_CHARS = re.compile(r'[\w@$"]+', re.UNICODE)

    def _current_prefix(self) -> str:
        cursor = self.textCursor()
        block_text = cursor.block().text()
        pos_in_block = cursor.positionInBlock()
        start = pos_in_block
        while start > 0 and self._WORD_CHARS.fullmatch(block_text[start - 1]):
            start -= 1
        return block_text[start:pos_in_block]

    def _insert_completion(self, completion: str) -> None:
        if self._completer is None:
            return
        prefix_len = len(self._completer.completionPrefix())
        cursor = self.textCursor()
        cursor.movePosition(
            QTextCursor.MoveOperation.Left,
            QTextCursor.MoveMode.KeepAnchor,
            prefix_len,
        )
        cursor.insertText(completion)
        self.setTextCursor(cursor)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        popup_visible = (
            self._completer is not None and self._completer.popup().isVisible()
        )
        if popup_visible and event.key() in (
            Qt.Key.Key_Enter,
            Qt.Key.Key_Return,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
            Qt.Key.Key_Escape,
        ):
            # Let the popup consume navigation/acceptance keys.
            event.ignore()
            return

        super().keyPressEvent(event)

        if self._completer is None or self.isReadOnly():
            return
        try:
            prefix = self._current_prefix()
            if len(prefix) < COMPLETER_MIN_PREFIX or event.text() in ("", " "):
                self._completer.popup().hide()
                return
            if prefix != self._completer.completionPrefix():
                self._completer.setCompletionPrefix(prefix)
                self._completer.popup().setCurrentIndex(
                    self._completer.completionModel().index(0, 0)
                )
            if self._completer.completionCount() == 0:
                self._completer.popup().hide()
                return
            rect = self.cursorRect()
            rect.setWidth(
                self._completer.popup().sizeHintForColumn(0)
                + self._completer.popup().verticalScrollBar().sizeHint().width()
                + 8
            )
            self._completer.complete(rect)
        except Exception as exc:
            _log(f"Completion popup failed: {exc}", Qgis.MessageLevel.Info)

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        """React to changes of the covered editor. Never consumes events."""
        if obj is self._sci and not self._detached:
            event_type = event.type()
            if event_type in (
                QEvent.Type.Resize,
                QEvent.Type.Show,
                QEvent.Type.LayoutRequest,
                QEvent.Type.ZOrderChange,
            ):
                self._sync_geometry()
            elif event_type == QEvent.Type.EnabledChange:
                try:
                    self.setEnabled(self._sci.isEnabled())
                    self.setReadOnly(bool(self._sci.isReadOnly()))
                except Exception:
                    pass
            elif event_type in (
                QEvent.Type.PaletteChange,
                QEvent.Type.StyleChange,
                QEvent.Type.ApplicationPaletteChange,
                QEvent.Type.FontChange,
            ):
                self._apply_appearance()
                if self._highlighter is not None:
                    self._highlighter.rebuild()
                    self._highlighter.rehighlight()
        return False

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._sync_geometry()

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #

    def detach(self) -> None:
        """Cleanly remove the overlay and restore the original editor."""
        if self._detached:
            return
        self._detached = True
        try:
            self._disconnect_signals()
        except Exception:
            pass
        try:
            # A dangling focus proxy pointer would crash Qt - always clear it.
            if self._sci is not None:
                self._sci.setFocusProxy(None)
                self._sci.removeEventFilter(self)
        except Exception:
            pass
        try:
            self.setParent(None)
            self.deleteLater()
        except Exception:
            pass
        self._sci = None


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


class CodeEditorWatcher(QObject):
    """Application-wide watcher that attaches overlays to targeted editors.

    A single event filter on ``QApplication`` is installed.  It is invoked for
    every event in the application, so the very first thing it does is an
    integer comparison on the event type; only ``Show`` events go any further.
    That keeps the cost effectively immeasurable.

    ``QEvent.Show`` is the right trigger: it fires once the widget has been
    created, parented and laid out, and it also fires for editors that live on
    an inactive tab and only become visible later.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._overlays: Dict[int, RtlOverlayEditor] = {}
        self._focus_connected = False
        #: ids of widgets already reported as skipped, to keep the log readable
        self._reported: set = set()

    # -- public API -------------------------------------------------------- #

    def install(self) -> None:
        """Install both detection mechanisms.

        Two independent paths, because either one alone has failure modes:

        * **Event filter on QApplication** - sees ``QEvent.Show`` for every
          object. Fast and immediate, but it is a single point of failure: if
          the events do not reach us for any reason, detection is dead.
        * **QApplication.focusChanged** - a plain Qt signal, emitted whenever
          keyboard focus moves. Opening a dialog always moves focus into it, so
          this catches the dialog even if no Show event was observed. It is a
          signal, not a timer, so it costs nothing when the user is idle.

        Both funnel into the same idempotent ``attach()``, so a dialog caught by
        both paths still gets exactly one overlay.
        """
        app = QApplication.instance()
        if app is None:
            return
        app.installEventFilter(self)
        try:
            app.focusChanged.connect(self._on_focus_changed)
            self._focus_connected = True
        except Exception as exc:
            _log(f"focusChanged hook unavailable: {exc}", Qgis.MessageLevel.Info)
        self.scan_existing()

    def uninstall(self) -> None:
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass
            if self._focus_connected:
                try:
                    app.focusChanged.disconnect(self._on_focus_changed)
                except Exception:
                    pass
                self._focus_connected = False
        self.detach_all()

    def _on_focus_changed(self, _old, new) -> None:
        """Detection path 2: focus moved - scan the window it moved into."""
        try:
            if new is None or not isinstance(new, QWidget):
                return
            window = new.window()
            if window is None:
                return
            key = id(window)
            # Only scan a given window once per focus arrival burst; scanning is
            # cheap but focus can change several times while a dialog builds.
            self.scan_widget(window)
            del key
        except RuntimeError:
            pass  # widget destroyed mid-signal
        except Exception as exc:
            _log(f"focus scan failed: {exc}", Qgis.MessageLevel.Info)

    def scan_existing(self) -> None:
        """Attach to editors that are already open when the plugin loads."""
        try:
            for window in QApplication.topLevelWidgets():
                if window.isVisible():
                    self.scan_widget(window)
        except Exception as exc:
            _log(f"Initial scan failed: {exc}", Qgis.MessageLevel.Info)

    def detach_all(self) -> None:
        for overlay in list(self._overlays.values()):
            try:
                overlay.detach()
            except Exception:
                pass
        self._overlays.clear()
        self._reported.clear()

    # -- Qt ---------------------------------------------------------------- #

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        try:
            if event.type() != QEvent.Type.Show:
                return False
            if not isinstance(obj, QWidget):
                return False

            # Path 1: the editor itself was shown.
            if self._is_target_editor(obj):
                self.attach(obj)
                return False

            # Path 2: ANY top-level window was shown -> scan its subtree.
            #
            # Deliberately not filtered by class name.  Relying on a list of
            # dialog class names proved fragile: one rename upstream and
            # detection silently does nothing.  Scanning instead means the only
            # thing that has to be right is what an editor looks like, not what
            # its dialog is called.  findChildren() on a dialog is a cheap
            # in-memory walk and windows are shown rarely, so the cost is
            # irrelevant.
            if obj.isWindow():
                self.scan_widget(obj)
                # Re-scan once the event loop has settled, so we also catch
                # editors created lazily during the dialog's first paint.
                # Single-shot, not polling.
                QTimer.singleShot(0, lambda w=obj: self._safe_scan(w))
        except Exception as exc:
            _log(f"Watcher error: {exc}", Qgis.MessageLevel.Warning)
        return False  # never consume - we only observe

    # -- internals --------------------------------------------------------- #

    def _safe_scan(self, widget) -> None:
        try:
            if widget is not None and widget.isVisible():
                self.scan_widget(widget)
        except RuntimeError:
            pass  # dialog already destroyed
        except Exception as exc:
            _log(f"Deferred scan failed: {exc}", Qgis.MessageLevel.Info)

    def scan_widget(self, root: QWidget) -> None:
        """Attach to every targeted editor inside ``root``."""
        for child in root.findChildren(QWidget):
            if self._is_target_editor(child) or self._is_generic_candidate(child):
                self.attach(child)
            elif DEBUG:
                # Report near-misses once each, so a class-name change is
                # visible in the log instead of failing silently.
                names = self._meta_class_names(child)
                if not any("Scintilla" in name or "CodeEditor" in name for name in names):
                    continue
                key = id(child)
                if key in self._reported or key in self._overlays:
                    continue
                self._reported.add(key)
                _dbg(
                    f"SKIPPED editor: cpp={names[0] if names else '?'} "
                    f"objectName={child.objectName()!r} "
                    f"window={self._meta_class_names(child.window())[:1]} "
                    f"chain={names[:4]}"
                )

    @staticmethod
    def _meta_class_names(obj) -> List[str]:
        """Return the object's real C++ class chain, most-derived first.

        This is the crux of reliable detection.  ``type(obj).__name__`` is the
        *Python wrapper* class, and for a widget created in C++ that Python has
        never touched, PyQt picks that wrapper by walking the QMetaObject chain
        until it finds a sip type it can resolve.  ``QgsCodeEditorSQL`` lives in
        the ``qgis._gui`` sip module while its base ``QsciScintilla`` lives in
        ``PyQt6.Qsci``; the cross-module lookup fails and the wrapper degrades
        to plain ``QsciScintilla``.  Its dialog degrades to plain ``QDialog``
        for the same reason.

        ``QObject::metaObject()`` does not have this problem: it is resolved
        from the C++ vtable, so ``metaObject().className()`` reports
        ``QgsCodeEditorSQL`` regardless of how Python wrapped the object.
        Walking ``superClass()`` gives the true inheritance chain, which is the
        proper replacement for ``__mro__`` here.
        """
        names: List[str] = []
        try:
            meta = obj.metaObject()
            while meta is not None and len(names) < 32:
                names.append(meta.className())
                meta = meta.superClass()
        except Exception:
            pass
        # Union with the Python MRO: harmless, and it keeps working for widgets
        # that really were created from Python.
        try:
            names.extend(klass.__name__ for klass in type(obj).__mro__)
        except Exception:
            pass
        return names

    @classmethod
    def _window_is_excluded(cls, widget) -> bool:
        """True for the Python Console, script editor and the main window."""
        try:
            window = widget.window()
            if window is None:
                return True
            names = cls._meta_class_names(window)
            identity = " ".join(names) + " " + window.objectName()
            if any(token in identity for token in EXCLUDED_WINDOW_TOKENS):
                return True
            # The main window is not a dialog; expression editors never live
            # directly in it, and skipping it avoids scanning a huge tree.
            if "QgsLayerTreeView" in names:
                return True
            return False
        except Exception:
            return True

    @classmethod
    def _is_target_editor(cls, obj) -> bool:
        """Positive match on the real C++ class name."""
        if not isinstance(obj, QWidget):
            return False
        names = set(cls._meta_class_names(obj))
        if names & EXCLUDED_EDITOR_CLASSES:
            return False
        return bool(names & TARGET_EDITOR_CLASSES)

    @classmethod
    def _is_generic_candidate(cls, obj) -> bool:
        """Last-resort match when no QGIS class name can be resolved at all.

        Accepts an editable Scintilla widget inside a dialog, minus the known
        non-expression panes.  The read-only test is what excludes help views
        such as ``mFunctionBuilderHelp`` without needing to name them.
        """
        if not ENABLE_GENERIC_FALLBACK or not isinstance(obj, QWidget):
            return False

        names = set(cls._meta_class_names(obj))
        if names & EXCLUDED_EDITOR_CLASSES:
            return False

        is_scintilla = (
            QsciScintillaBase is not None and isinstance(obj, QsciScintillaBase)
        ) or any("Scintilla" in name for name in names)
        if not is_scintilla:
            return False

        if obj.objectName() in EXCLUDED_OBJECT_NAMES:
            return False
        if cls._window_is_excluded(obj):
            return False

        try:
            if hasattr(obj, "isReadOnly") and obj.isReadOnly():
                return False
        except Exception:
            pass

        return True

    def attach(self, sci: QWidget) -> None:
        """Create an overlay for ``sci`` unless one already exists."""
        key = id(sci)
        if key in self._overlays:
            self._overlays[key]._sync_geometry()
            return
        # Idempotency across plugin reloads: an overlay left behind by a
        # previous instance is found by object name rather than by our dict.
        if sci.findChild(QPlainTextEdit, OVERLAY_OBJECT_NAME) is not None:
            return

        try:
            overlay = RtlOverlayEditor(sci)
        except Exception as exc:
            import traceback

            _log(
                f"Failed to attach overlay to {type(sci).__name__}: {exc}\n"
                f"{traceback.format_exc()}",
                Qgis.MessageLevel.Critical,
            )
            return

        self._overlays[key] = overlay
        names = self._meta_class_names(sci)
        _log(
            f"overlay attached: cpp={names[0] if names else '?'} "
            f"objectName={sci.objectName()!r} "
            f"window={self._meta_class_names(sci.window())[:1]} "
            f"size={sci.width()}x{sci.height()}",
            Qgis.MessageLevel.Success,
        )

        # At QEvent.Show the parent's final layout geometry is not always
        # assigned yet, which would leave the overlay at a stale or zero size.
        # A single zero-delay callback re-syncs it after the event loop has
        # applied the layout.  One shot per overlay - not a polling timer.
        QTimer.singleShot(0, overlay._sync_geometry)
        # The overlay is a child of the editor, so Qt destroys it with the
        # dialog automatically; we only need to drop our bookkeeping entry.
        sci.destroyed.connect(lambda *_a, k=key: self._overlays.pop(k, None))


# --------------------------------------------------------------------------- #
# Console entry points
#
# Exposed so the plugin can be inspected and re-triggered from the QGIS Python
# Console without restarting QGIS:
#
#     from rtl_bidi_editor import rtl_bidi_editor as m
#     m.rescan()
# --------------------------------------------------------------------------- #

#: Set by RtlBidiEditorPlugin.initGui(); None while the plugin is unloaded.
_WATCHER: Optional["CodeEditorWatcher"] = None


def rescan() -> int:
    """Force a scan of all visible windows. Returns the live overlay count."""
    if _WATCHER is None:
        _log("Plugin is not loaded.", Qgis.MessageLevel.Warning)
        return 0
    _WATCHER.scan_existing()
    count = len(_WATCHER._overlays)
    _log(f"Rescan complete - {count} overlay(s) active.", Qgis.MessageLevel.Info)
    return count


def overlay_count() -> int:
    """Number of currently attached overlays."""
    return 0 if _WATCHER is None else len(_WATCHER._overlays)


# --------------------------------------------------------------------------- #
# Plugin
# --------------------------------------------------------------------------- #


class RtlBidiEditorPlugin:
    """QGIS plugin lifecycle.

    Deliberately headless: there is no menu entry, no toolbar button and no
    dialog.  The requirement is that the user never has to enable or disable
    anything, so the plugin is active for as long as it is installed.
    """

    def __init__(self, iface):
        self.iface = iface
        self._watcher: Optional[CodeEditorWatcher] = None

    def initGui(self) -> None:  # noqa: N802 (QGIS plugin API)
        global _WATCHER
        try:
            self._watcher = CodeEditorWatcher()
            _WATCHER = self._watcher
            self._watcher.install()
            _log("RTL / BiDi editor active.", Qgis.MessageLevel.Success)
            _dbg(
                f"watching editor classes {sorted(TARGET_EDITOR_CLASSES)}; "
                f"detection = Show-event filter + focusChanged signal"
            )
        except Exception as exc:
            _log(f"Startup failed: {exc}", Qgis.MessageLevel.Critical)

    def unload(self) -> None:
        global _WATCHER
        _WATCHER = None
        if self._watcher is None:
            return
        try:
            self._watcher.uninstall()
        except Exception as exc:
            _log(f"Unload error: {exc}", Qgis.MessageLevel.Warning)
        finally:
            self._watcher = None

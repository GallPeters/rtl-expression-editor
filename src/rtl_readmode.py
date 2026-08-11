# -*- coding: utf-8 -*-
"""
Read mode for the RTL / BiDi editor: show descriptions instead of codes.

Purely additive.  Nothing here modifies the overlay's synchronisation, its
geometry handling, the bracket matcher or the syntax highlighter.

The one safety-critical rule
----------------------------
The substituted text is a **display artefact only**.  It must never reach the
QScintilla editor, or the expression QGIS saves would contain descriptions
instead of codes.  Every write to the overlay's document is therefore wrapped
in ``editor.blockSignals(True)``, so ``textChanged`` never fires and the
existing overlay -> Scintilla push is never triggered.  The original text is
kept and restored verbatim when read mode is switched off, and the authoritative
text always remains the one held by Scintilla.

Three pieces:

``SlideSwitch``          a small pill-shaped toggle drawn with QPainter.
``DescriptionResolver``  builds and caches the code -> description mapping.
``ReadModeController``   owns the switch, performs the swap, restores on exit.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QEvent, QObject, QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPainterPath
from qgis.PyQt.QtWidgets import QAbstractButton

from qgis.core import Qgis, QgsExpression, QgsFeatureRequest, QgsMessageLog

from .rtl_settings import BUS, Settings

LOG_TAG = "RTL Expression Editor"

#: Object name of the overlay, excluded from the context chain.
OVERLAY_HINT = "rtlBidiOverlayEditor"

#: Height reserved at the bottom of the editor so the switch never sits on text.
SWITCH_STRIP_HEIGHT = 26

#: Ceiling on rows loaded for the description map.
MAX_MAPPING_ROWS = 20000


def _log(message: str, level=Qgis.MessageLevel.Info) -> None:
    try:
        QgsMessageLog.logMessage(message, LOG_TAG, level)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #


class SlideSwitch(QAbstractButton):
    """A small pill toggle, drawn rather than themed.

    Drawn with QPainter so it looks the same on every platform and needs no
    stylesheet that could clash with the user's QGIS theme.  Kept deliberately
    small and low-contrast: it lives inside the editor, so it must read as a
    control without competing with the text.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(38, 18)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # never steal caret focus
        self.setToolTip(
            "Show descriptions instead of codes (read-only preview).\n"
            "The saved expression always keeps the original codes."
        )

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        radius = rect.height() / 2.0

        on = self.isChecked()
        track = QColor("#4a90d9") if on else QColor("#b8b8b8")
        if not self.isEnabled():
            track = QColor("#d0d0d0")

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, track)

        knob_d = rect.height() - 4.0
        knob_x = rect.right() - knob_d - 2.0 if on else rect.left() + 2.0
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(knob_x + knob_d / 2.0, rect.center().y()), knob_d / 2.0, knob_d / 2.0)
        painter.end()


# --------------------------------------------------------------------------- #
# Code -> description mapping
# --------------------------------------------------------------------------- #


def _connect_project_invalidation(callback) -> bool:
    """Call ``callback`` whenever the current project is replaced.

    Both caches in this module hold project-scoped data, so neither may survive
    a project change. Without this, opening project B after using project A
    would serve A's cached data - and worse, ``ChoiceMemory.remember()`` writes
    the whole merged dictionary back, so A's entries would be saved into B.

    ``QgsProject.instance()`` is a singleton that persists across project
    changes, so connecting once is enough. Returns True if at least one signal
    was connected, so callers only mark themselves hooked on success.
    """
    connected = False
    try:
        from qgis.core import QgsProject

        project = QgsProject.instance()
        for signal_name in ("cleared", "readProject"):
            try:
                getattr(project, signal_name).connect(callback)
                connected = True
            except Exception:
                pass
    except Exception as exc:
        _log(f"Could not hook project change: {exc}", Qgis.MessageLevel.Warning)
    return connected


class DescriptionResolver:
    """Builds ``field -> {code: [descriptions]}`` for one table context.

    Cached per table context and dropped whenever settings change, so switching
    to read mode repeatedly costs one query at most.
    """

    _cache: Dict[str, Dict[str, Dict[str, List[str]]]] = {}
    _hooked = False

    @classmethod
    def _ensure_hook(cls) -> None:
        if cls._hooked:
            return
        try:
            BUS.changed.connect(cls.invalidate)
        except Exception:
            pass
        _connect_project_invalidation(cls.invalidate)
        cls._hooked = True

    @classmethod
    def invalidate(cls) -> None:
        """Drop the mapping cache. Safe to call from a signal."""
        cls._cache.clear()

    @classmethod
    def mapping(cls, table_candidates: List[str]) -> Dict[str, Dict[str, List[str]]]:
        cls._ensure_hook()
        key = "|".join(sorted(t.lower() for t in table_candidates))
        if key in cls._cache:
            return cls._cache[key]
        result = cls._load(table_candidates)
        cls._cache[key] = result
        return result

    @classmethod
    def _load(cls, table_candidates: List[str]) -> Dict[str, Dict[str, List[str]]]:
        mapping: Dict[str, Dict[str, List[str]]] = {}
        layer = Settings.autocomplete_layer()
        if layer is None:
            return mapping

        f_names = Settings.field("field_names")
        f_value = Settings.field("value")
        f_desc = Settings.field("description")
        f_table = Settings.field("table")
        if not (f_names and f_value and f_desc):
            return mapping  # nothing to substitute without a description field

        request = QgsFeatureRequest()
        if f_table and table_candidates:
            column = QgsExpression.quotedColumnRef(f_table)
            ors = " OR ".join(
                f"lower(trim({column})) = {QgsExpression.quotedString(t.lower())}"
                for t in table_candidates
            )
            request.setFilterExpression(ors)
        request.setLimit(MAX_MAPPING_ROWS)
        try:
            request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        except Exception:
            pass

        try:
            for feature in layer.getFeatures(request):
                field = cls._text(feature, f_names).lower()
                code = cls._text(feature, f_value)
                description = cls._text(feature, f_desc)
                if not (field and code and description):
                    continue
                # Key by both spellings so a table storing codes with or
                # without quotes both resolve.
                for variant in {code, normalize_code(code)}:
                    if not variant:
                        continue
                    bucket = mapping.setdefault(field, {}).setdefault(variant, [])
                    if description not in bucket:
                        bucket.append(description)
        except Exception as exc:
            _log(f"Read-mode mapping failed: {exc}", Qgis.MessageLevel.Warning)
        return mapping

    @staticmethod
    def _text(feature, field_name: str) -> str:
        try:
            raw = feature[field_name]
        except Exception:
            return ""
        if raw is None:
            return ""
        try:
            if hasattr(raw, "isNull") and raw.isNull():
                return ""
        except Exception:
            pass
        return str(raw).strip()


class ChoiceMemory:
    """Remembers which meaning the user picked for an ambiguous code.

    Stores *intent*, not text: ``(table, field, code) -> description``. That is
    the key design decision. Mirroring the whole expression would require the
    plugin to observe every edit, and any change made outside the overlay - a
    script setting subsetString, another QGIS install, a colleague's machine -
    would leave the mirror stale and read mode confidently wrong. A recorded
    choice cannot go stale: it stays true however the expression is later
    rewritten, and is simply unused if the code stops appearing.

    Persisted with ``QgsProject.writeEntry()``, the idiomatic place for plugin
    data in a .qgs/.qgz. Project attachments are meant for bundled files.
    """

    SCOPE = "rtl_bidi_editor"
    KEY = "value_choices"

    _cache: Optional[Dict[str, str]] = None
    _hooked = False

    @classmethod
    def _ensure_hook(cls) -> None:
        """Drop the cache when the project changes.

        Choices are stored per project, so a cache that outlived a project
        switch would both read the wrong values and write one project's choices
        into another.
        """
        if cls._hooked:
            return
        try:
            BUS.changed.connect(cls.invalidate)
        except Exception:
            pass
        _connect_project_invalidation(cls.invalidate)
        cls._hooked = True

    @classmethod
    def _key(
        cls, table: str, field: str, code: str, occurrence: int, context: str = ""
    ) -> str:
        """Key a choice to a specific *occurrence* of the code.

        Keying on (table, field, code) alone made every instance of a code
        resolve to the last description picked - including instances the user
        typed by hand, which should show all possible meanings instead. The
        occurrence index is what separates "the 610 I chose from the list" from
        "some other 610 in this expression".
        """
        return (
            f"{context or ''}\x1e{(table or '').lower()}\x1f{(field or '').lower()}"
            f"\x1f{normalize_code(code)}\x1f{int(occurrence)}"
        )

    @classmethod
    def _load(cls) -> Dict[str, str]:
        cls._ensure_hook()
        if cls._cache is not None:
            return cls._cache
        data: Dict[str, str] = {}
        try:
            import json

            from qgis.core import QgsProject

            raw, ok = QgsProject.instance().readEntry(cls.SCOPE, cls.KEY, "")
            if ok and raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = {str(k): str(v) for k, v in parsed.items()}
        except Exception as exc:
            _log(f"Could not read remembered choices: {exc}")
        cls._cache = data
        return data

    @classmethod
    def remember(
        cls,
        table: str,
        field: str,
        code: str,
        description: str,
        occurrence: int = 0,
        context: str = "",
    ) -> None:
        """Record a choice for one occurrence and persist it into the project."""
        if not (field and code and description):
            return
        cls._ensure_hook()
        data = cls._load()
        key = cls._key(table, field, code, occurrence, context)
        if data.get(key) == description:
            return
        data[key] = description
        try:
            import json

            from qgis.core import QgsProject

            QgsProject.instance().writeEntry(cls.SCOPE, cls.KEY, json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            _log(f"Could not persist remembered choice: {exc}")

    @classmethod
    def recall(
        cls, table: str, field: str, code: str, occurrence: int = 0, context: str = ""
    ) -> str:
        """Description chosen for this occurrence, or "" if it was not chosen.

        Deliberately does NOT fall back to a different occurrence: a miss must
        mean "the user did not pick this one", so read mode shows every meaning.
        """
        return cls._load().get(cls._key(table, field, code, occurrence, context), "")

    @classmethod
    def invalidate(cls) -> None:
        cls._cache = None


#: Quoted field reference, single-quoted literal, or a bare number - scanned in
#: one pass so each literal can be attributed to the field that precedes it.
_SCAN_RE = re.compile(
    r'"(?P<field>[^"\n]+)"'
    r"|'(?P<quoted>(?:[^'\\]|\\.)*)'"
    r"|(?P<bare>\b\d+(?:\.\d+)?\b)"
)


def normalize_code(text: str) -> str:
    """Strip one layer of matching outer quotes from a code value.

    Needed because a lookup table may store codes with the quotes included, e.g.
    the literal seven characters ``'farm'`` rather than ``farm``. That form is
    convenient - double-clicking inserts a ready-made SQL string - but it breaks
    matching: _SCAN_RE captures a quoted literal's *contents*, so the expression
    yields ``farm`` while the mapping is keyed by ``'farm'``.

    Only the lookup keys are normalised. The value inserted into the expression
    is never touched, so a code stored with quotes still produces valid SQL.
    """
    value = (text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def expression_context_key(widget) -> str:
    """Identify WHICH expression slot this editor belongs to.

    Remembered choices were previously keyed only by (table, field, code,
    occurrence), so two expressions on the same layer - a fill-colour override
    and a stroke-colour override - shared one key and overwrote each other.

    The identifying information is **outside** the dialog, not inside it. Every
    data-defined expression opens the same QgsExpressionBuilderDialogBase with
    the same title and the same internal widget tree, so nothing within it says
    which property is being edited. What differs is the object that created the
    dialog: the property-override button, e.g. ``mFillColorDDBtn`` versus
    ``mStrokeColorDDBtn``. So the chain is walked outwards from the dialog.

    ``parent()`` is used rather than ``parentWidget()`` because the chain can run
    through non-widget QObjects, which ``parentWidget()`` would skip.

    Known limit: two symbol layers of the same type in one renderer expose the
    same button object name, so their choices still share a key.
    """
    parts: List[str] = []
    window = None
    try:
        window = widget.window()
    except Exception as exc:
        _log(f"Context key: no window ({exc})")

    if window is not None:
        try:
            parts.append(window.objectName() or "")
            parts.append(window.windowTitle() or "")
        except Exception as exc:
            _log(f"Context key: window identity unavailable ({exc})")

        # The outward chain - this is what separates one slot from another.
        try:
            chain: List[str] = []
            node = window.parent()
            depth = 0
            while node is not None and depth < 12:
                name = node.objectName()
                if name:
                    chain.append(name)
                node = node.parent()
                depth += 1
            if chain:
                parts.append(">".join(chain))
        except Exception as exc:
            _log(f"Context key: parent chain unavailable ({exc})")

    # Keep the same slot on two different layers separate. Failures are logged
    # rather than swallowed - a silent miss here is what dropped the layer id
    # from the key previously.
    try:
        from .rtl_autocomplete import _find_context_layer

        layer = _find_context_layer(widget)
        if layer is not None:
            parts.append(layer.id())
        else:
            _log("Context key: no context layer resolved")
    except Exception as exc:
        _log(f"Context key: layer id unavailable ({exc})")

    return "|".join(part for part in parts if part)


def occurrence_index(text_before: str, field: str, code: str) -> int:
    """How many earlier literals in ``text_before`` are this field's ``code``.

    Used at two moments that must agree exactly: when a choice is recorded (the
    caller passes the text up to the insertion point) and when read mode scans
    the finished expression. Both count the same way, so the index identifies
    the same occurrence in both directions.
    """
    field = (field or "").strip().lower()
    code = normalize_code(code)
    if not field or not code:
        return 0

    count = 0
    current_field = ""
    for match in _SCAN_RE.finditer(text_before):
        found_field = match.group("field")
        if found_field is not None:
            current_field = found_field.strip().lower()
            continue
        literal = match.group("quoted")
        if literal is None:
            literal = match.group("bare")
        if literal is None:
            continue
        if current_field == field and normalize_code(literal) == code:
            count += 1
    return count


def substitute_descriptions(
    text: str,
    mapping: Dict[str, Dict[str, List[str]]],
    table: str = "",
    context: str = "",
) -> str:
    """Replace value codes with descriptions, for display only.

    Each literal is attributed to the nearest **preceding** quoted field, which
    is how the expression reads (``"F_ATT" IN ('610', '607')``). That is what
    keeps ``"OTHER" = '610'`` untouched.

    When a code has several meanings under the same field, the choice the user
    made in the completion popup decides. See ``_pick_label``.
    """
    if not mapping:
        return text

    out: List[str] = []
    last_end = 0
    current_field = ""
    counters: Dict[tuple, int] = {}

    for match in _SCAN_RE.finditer(text):
        field = match.group("field")
        if field is not None:
            current_field = field.strip().lower()
            continue

        literal = match.group("quoted")
        if literal is None:
            literal = match.group("bare")
        if literal is None:
            continue

        code = normalize_code(literal)
        candidates = mapping.get(current_field, {}).get(code)
        if not candidates:
            candidates = mapping.get(current_field, {}).get(literal.strip())
        if not candidates:
            continue

        # Which occurrence of this field/code pair is this? Counted the same way
        # as occurrence_index() counts it when the choice was recorded.
        seen_key = (current_field, code)
        index = counters.get(seen_key, 0)
        counters[seen_key] = index + 1

        label = _pick_label(candidates, table, current_field, code, index, context)
        if not label:
            continue

        # Replace the WHOLE literal, quotes included, with the bare label.
        #
        # Re-wrapping in quotes produced nonsense whenever the description was
        # not a string: a code of 'house' with description 648 rendered as '648'.
        # Read mode is a human-readable rendering rather than valid SQL - the
        # real expression is untouched underneath - so quoting adds nothing and
        # misleads when the description is numeric.
        out.append(text[last_end:match.start()])
        out.append(label)
        last_end = match.end()

    if not out:
        return text
    out.append(text[last_end:])
    return "".join(out)


def _pick_label(
    candidates: List[str],
    table: str,
    field: str,
    code: str,
    occurrence: int = 0,
    context: str = "",
) -> str:
    """Choose among competing descriptions for one code.

    Exactly two rules, in order:

    1. **The remembered choice** - what the user picked from the popup for this
       (table, field, code).
    2. **Show every meaning**, joined by ``/``.

    Inferring the meaning from a group code mentioned elsewhere in the
    expression was considered and deliberately rejected. It reads correctly for
    ``"F_CODE" = '2301' AND "F_ATT" = '610'`` but silently misreads negation,
    OR and nesting: in ``"F_CODE" = '2301' AND "F_ATT" != '610'`` the group term
    does not determine which 610 is being excluded. A rule that is right most of
    the time invites trust it has not earned, and two interacting mechanisms are
    harder to reason about than one. Remembering is the only mechanism.
    """
    # Descriptions are normalised for DISPLAY only. A table that stores values
    # with their quotes included, e.g. the literal ``'farm'``, would otherwise
    # render as ``''farm''`` once the substitution re-wraps a quoted literal.
    # Matching against remembered choices uses the raw stored form, so the two
    # stay consistent.
    if len(candidates) == 1:
        return normalize_code(candidates[0])

    remembered = ChoiceMemory.recall(table, field, code, occurrence, context)
    if remembered and remembered in candidates:
        return normalize_code(remembered)

    return " / ".join(normalize_code(candidate) for candidate in candidates)


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #


class ReadModeController(QObject):
    """Owns the switch and performs the display swap.

    Attached to one overlay editor. Does nothing at all unless a lookup layer
    with a description field is configured, so users who have not set one up see
    no change whatsoever.
    """

    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor
        self._switch: Optional[SlideSwitch] = None
        self._original: Optional[str] = None
        self._active = False
        self._was_read_only = False

        if not self._feature_available():
            return

        try:
            self._switch = SlideSwitch(editor)
            self._switch.toggled.connect(self._on_toggled)
            self._switch.show()
            self._switch.raise_()
            editor.installEventFilter(self)
            self._reserve_strip(True)
            self._reposition()
            # Apply the configured default mode once the dialog has settled.
            if Settings.default_read_mode():
                from qgis.PyQt.QtCore import QTimer

                QTimer.singleShot(0, lambda: self._switch and self._switch.setChecked(True))
        except Exception as exc:
            _log(f"Read mode unavailable: {exc}", Qgis.MessageLevel.Warning)
            self._switch = None

    # -- availability ------------------------------------------------------ #

    @staticmethod
    def _feature_available() -> bool:
        """Only offer read mode when there is something to substitute."""
        try:
            usable, _ = Settings.autocomplete_is_usable()
            return bool(usable and Settings.field("description"))
        except Exception:
            return False

    # -- layout ------------------------------------------------------------ #

    def _reserve_strip(self, reserve: bool) -> None:
        """Keep a clear strip at the bottom so the switch never covers text."""
        try:
            self._editor.setViewportMargins(0, 0, 0, SWITCH_STRIP_HEIGHT if reserve else 0)
        except Exception:
            pass  # protected in some bindings; the switch simply overlays instead

    def _reposition(self) -> None:
        if self._switch is None or self._editor is None:
            return
        try:
            margin = 4
            self._switch.move(
                margin,
                self._editor.height() - self._switch.height() - margin,
            )
            self._switch.raise_()
        except RuntimeError:
            pass

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        if obj is self._editor and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self._reposition()
        return False

    # -- mode switching ---------------------------------------------------- #

    def _on_toggled(self, checked: bool) -> None:
        try:
            if checked:
                self._enter_read_mode()
            else:
                self._leave_read_mode()
        except Exception as exc:
            _log(f"Read mode toggle failed: {exc}", Qgis.MessageLevel.Warning)

    def _set_text_silently(self, text: str) -> None:
        """Write to the document without emitting textChanged.

        This is the mechanism that keeps the substituted text out of Scintilla.
        Blocking the widget's signals means the existing overlay -> Scintilla
        push never runs, so the stored expression is untouched. The existing
        synchronisation code is not modified in any way - it is simply not
        triggered.
        """
        editor = self._editor
        blocked = editor.blockSignals(True)
        try:
            cursor = editor.textCursor()
            position = cursor.position()
            cursor.beginEditBlock()
            cursor.select(cursor.SelectionType.Document)
            cursor.insertText(text)
            cursor.endEditBlock()
            cursor.setPosition(min(position, len(text)))
            editor.setTextCursor(cursor)
        finally:
            editor.blockSignals(blocked)

    def _enter_read_mode(self) -> None:
        editor = self._editor
        if editor is None or self._active:
            return

        # Take the authoritative text from Scintilla when available, so the
        # preview reflects what QGIS will actually save.
        sci = getattr(editor, "_sci", None)
        try:
            self._original = sci.text() if sci is not None else editor.toPlainText()
            self._original = self._original.replace("\r\n", "\n").replace("\r", "\n")
        except Exception:
            self._original = editor.toPlainText()

        from .rtl_autocomplete import resolve_table_candidates

        tables = resolve_table_candidates(sci if sci is not None else editor)
        mapping = DescriptionResolver.mapping(tables)
        preview = substitute_descriptions(
            self._original,
            mapping,
            tables[0] if tables else "",
            expression_context_key(sci if sci is not None else editor),
        )

        self._was_read_only = editor.isReadOnly()
        self._active = True
        self._set_text_silently(preview)
        editor.setReadOnly(True)

    def _leave_read_mode(self) -> None:
        editor = self._editor
        if editor is None or not self._active:
            return
        self._active = False

        # Restore from Scintilla, which never saw the preview and is therefore
        # authoritative even if QGIS changed the expression meanwhile.
        sci = getattr(editor, "_sci", None)
        text = self._original or ""
        try:
            if sci is not None:
                current = sci.text().replace("\r\n", "\n").replace("\r", "\n")
                if current:
                    text = current
        except Exception:
            pass

        self._set_text_silently(text)
        editor.setReadOnly(self._was_read_only)
        self._original = None

    # -- teardown ---------------------------------------------------------- #

    def teardown(self) -> None:
        """Restore edit mode and remove the switch; safe to call twice."""
        try:
            if self._active:
                self._leave_read_mode()
        except Exception:
            pass
        try:
            if self._editor is not None:
                self._editor.removeEventFilter(self)
                self._reserve_strip(False)
        except Exception:
            pass
        if self._switch is not None:
            try:
                self._switch.deleteLater()
            except Exception:
                pass
            self._switch = None
        self._editor = None

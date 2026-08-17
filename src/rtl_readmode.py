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
from typing import Dict, List, Optional, Tuple

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


#: One track colour per mode, in order: edit (grey), read (blue), and - only
#: when a third mode is enabled - alternative read (purple). Distinct hues
#: rather than just knob position, so which mode is active is legible even
#: at this widget's small size.
_MODE_COLORS = (QColor("#b8b8b8"), QColor("#4a90d9"), QColor("#8a5fd1"))

#: One label per mode, used both for the tooltip and to build it.
_MODE_LABELS = ("Edit mode (show codes)", "Read mode (show descriptions)", "Alternative read mode")


class SlideSwitch(QAbstractButton):
    """A small pill toggle, drawn rather than themed.

    Drawn with QPainter so it looks the same on every platform and needs no
    stylesheet that could clash with the user's QGIS theme.  Kept deliberately
    small and low-contrast: it lives inside the editor, so it must read as a
    control without competing with the text.

    Has ``mode_count`` positions (2: edit/read, or 3: edit/read/alternative
    read - see ``ReadModeController``), and behaves like a genuine slider
    rather than a click-to-advance toggle: pressing anywhere on the track
    jumps the knob to the nearest position, and dragging follows the mouse
    continuously, snapping to whichever position is nearest on release - so
    edit -> read -> edit -> alternative (skipping read entirely) all work in
    one motion each, not just a fixed forward cycle.
    """

    #: Emitted with the new mode index (0 = edit) whenever it changes, either
    #: by a click/drag or by ``setMode()``.
    modeChanged = pyqtSignal(int)

    def __init__(self, parent=None, mode_count: int = 2):
        super().__init__(parent)
        self.setCheckable(False)
        self._mode_count = max(2, min(3, int(mode_count)))
        self._mode = 0
        #: True between mousePressEvent and mouseReleaseEvent - while true,
        #: paintEvent renders the knob at the live drag position instead of
        #: snapped to ``_mode``.
        self._dragging = False
        self._drag_fraction = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(38, 18)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # never steal caret focus
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        current = _MODE_LABELS[self._mode]
        self.setToolTip(
            f"{current}. Click or drag to switch modes.\n"
            "The saved expression always keeps the original codes."
        )

    def mode(self) -> int:
        return self._mode

    def setMode(self, mode: int) -> None:  # noqa: N802 (matches Qt widget-property naming)
        mode = max(0, min(self._mode_count - 1, int(mode)))
        if mode == self._mode:
            return
        self._mode = mode
        self._update_tooltip()
        self.update()
        self.modeChanged.emit(self._mode)

    def setModeCount(self, count: int) -> None:  # noqa: N802
        """Switch between 2-position (edit/read) and 3-position (+ alternative
        read) - called whenever an alternative description column is added,
        changed or removed in Settings."""
        count = max(2, min(3, int(count)))
        if count == self._mode_count:
            return
        self._mode_count = count
        if self._mode >= count:
            self.setMode(count - 1)
        else:
            self._update_tooltip()
        self.update()

    # -- position <-> mode mapping ------------------------------------------ #

    def _fraction_for_mode(self, mode: int) -> float:
        return mode / (self._mode_count - 1) if self._mode_count > 1 else 0.0

    def _mode_for_fraction(self, fraction: float) -> int:
        if self._mode_count <= 1:
            return 0
        fraction = max(0.0, min(1.0, fraction))
        return int(round(fraction * (self._mode_count - 1)))

    def _fraction_for_x(self, x: float) -> float:
        """Where along the track ``x`` (widget-local) falls, as 0..1."""
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        knob_d = rect.height() - 4.0
        travel = rect.width() - knob_d - 4.0
        if travel <= 0:
            return 0.0
        fraction = (x - rect.left() - 2.0 - knob_d / 2.0) / travel
        return max(0.0, min(1.0, fraction))

    @staticmethod
    def _event_x(event) -> float:
        """The event's widget-local X - PyQt6's QPointF-based position(),
        falling back to the PyQt5 QPoint-based x() on older bindings."""
        try:
            return event.position().x()
        except AttributeError:
            return float(event.x())

    # -- mouse-driven sliding ------------------------------------------------ #

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().mousePressEvent(event)
        self._dragging = True
        self._drag_fraction = self._fraction_for_x(self._event_x(event))
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().mouseMoveEvent(event)
        if not self._dragging:
            return
        self._drag_fraction = self._fraction_for_x(self._event_x(event))
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().mouseReleaseEvent(event)
        if not self._dragging:
            return
        self._dragging = False
        # Snaps to the nearest position, whether this was a quick tap (jump
        # straight to wherever was clicked) or a drag (follow, then settle).
        self.setMode(self._mode_for_fraction(self._drag_fraction))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        radius = rect.height() / 2.0

        if self._dragging:
            fraction = max(0.0, min(1.0, self._drag_fraction))
            preview_mode = self._mode_for_fraction(fraction)
        else:
            fraction = self._fraction_for_mode(self._mode)
            preview_mode = self._mode

        track = _MODE_COLORS[min(preview_mode, len(_MODE_COLORS) - 1)]
        if not self.isEnabled():
            track = QColor("#d0d0d0")

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, track)

        knob_d = rect.height() - 4.0
        travel = rect.width() - knob_d - 4.0
        knob_x = rect.left() + 2.0 + fraction * travel
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


#: What DescriptionResolver.mapping() returns for one table context:
#:
#: * ``values``  - ``field -> {code: [description, ...]}``, unchanged in
#:   shape from before the alternative-description column existed, so
#:   ``_pick_label()``'s primary-mode logic needed no changes at all.
#: * ``alt_values`` - ``field -> {code: {description: alt_description}}`` -
#:   for whichever primary description ends up chosen (by
#:   ``_pick_label()``), the alternative sitting next to *that same row*.
#:   Looked up by the description's own text, not stored anywhere per choice
#:   - see ChoiceMemory and _pick_label() for why that is what makes the
#:   alternative description available immediately for already-remembered
#:   choices, with no migration needed, as soon as the column is configured.
#: * ``field_descriptions`` - ``field -> field_description``, one flat
#:   description per field name, for the read-mode field-name annotation.
ValueMapping = Dict[str, Dict[str, List[str]]]
AltValueMapping = Dict[str, Dict[str, Dict[str, str]]]
FieldDescriptions = Dict[str, str]
ReadModeMapping = Tuple[ValueMapping, AltValueMapping, FieldDescriptions]

#: An empty result, returned whenever nothing is configured or found -
#: named so every early-return in _load() states its shape the same way.
_EMPTY_MAPPING: ReadModeMapping = ({}, {}, {})


class DescriptionResolver:
    """Builds the read-mode mapping (see ``ReadModeMapping``) for one table
    context.

    Cached per table context and dropped whenever settings change or the
    lookup layer's own data changes, so switching to read mode repeatedly
    costs one query at most.
    """

    _cache: Dict[str, ReadModeMapping] = {}
    _hooked = False
    _watched_layer_id: str = ""
    _watched_layer = None

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
    def _sync_layer_hooks(cls) -> None:
        """Keep edit-signal connections pointed at the configured layer.

        Mirrors ``AutocompleteCache._sync_layer_hooks()``: without this, an
        edit to the lookup table's own data (a changed description, or a
        newly filled-in alternative-description column) would not be
        reflected until something else happened to invalidate the cache -
        settings being resaved, or the project reloading.
        """
        layer = Settings.autocomplete_layer()
        layer_id = layer.id() if layer is not None else ""
        if layer_id == cls._watched_layer_id:
            return

        if cls._watched_layer is not None:
            for signal_name in (
                "dataChanged",
                "featureAdded",
                "featuresDeleted",
                "attributeValueChanged",
                "willBeDeleted",
            ):
                try:
                    getattr(cls._watched_layer, signal_name).disconnect(cls._on_layer_touched)
                except Exception:
                    pass

        cls._watched_layer = layer
        cls._watched_layer_id = layer_id

        if layer is None:
            return
        for signal_name in (
            "dataChanged",
            "featureAdded",
            "featuresDeleted",
            "attributeValueChanged",
            "willBeDeleted",
        ):
            try:
                getattr(layer, signal_name).connect(cls._on_layer_touched)
            except Exception:
                pass  # not every provider exposes every signal

    @classmethod
    def _on_layer_touched(cls, *_args) -> None:
        cls._cache.clear()

    @classmethod
    def invalidate(cls) -> None:
        """Drop the mapping cache. Safe to call from a signal."""
        cls._cache.clear()

    @classmethod
    def mapping(cls, table_candidates: List[str]) -> ReadModeMapping:
        cls._ensure_hook()
        cls._sync_layer_hooks()
        key = "|".join(sorted(t.lower() for t in table_candidates))
        if key in cls._cache:
            return cls._cache[key]
        result = cls._load(table_candidates)
        cls._cache[key] = result
        return result

    @classmethod
    def _load(cls, table_candidates: List[str]) -> ReadModeMapping:
        values: ValueMapping = {}
        alt_values: AltValueMapping = {}
        field_descriptions: FieldDescriptions = {}

        layer = Settings.autocomplete_layer()
        if layer is None:
            return _EMPTY_MAPPING

        f_names = Settings.field("field_names")
        f_value = Settings.field("value")
        f_desc = Settings.field("description")
        f_alt_desc = Settings.field("alt_description")
        f_field_desc = Settings.field("field_description")
        f_table = Settings.field("table")

        have_values = bool(f_names and f_value and f_desc)
        have_field_desc = bool(f_names and f_field_desc)
        if not (have_values or have_field_desc):
            return _EMPTY_MAPPING  # nothing configured to substitute at all

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
                if not field:
                    continue

                if have_field_desc and field not in field_descriptions:
                    fdesc = cls._text(feature, f_field_desc)
                    if fdesc:
                        field_descriptions[field] = fdesc

                if not have_values:
                    continue
                code = cls._text(feature, f_value)
                description = cls._text(feature, f_desc)
                if not (code and description):
                    continue
                alt_description = cls._text(feature, f_alt_desc) if f_alt_desc else ""
                # Key by both spellings so a table storing codes with or
                # without quotes both resolve.
                for variant in {code, normalize_code(code)}:
                    if not variant:
                        continue
                    bucket = values.setdefault(field, {}).setdefault(variant, [])
                    if description not in bucket:
                        bucket.append(description)
                    if alt_description:
                        alt_values.setdefault(field, {}).setdefault(variant, {})[
                            description
                        ] = alt_description
        except Exception as exc:
            _log(f"Read-mode mapping failed: {exc}", Qgis.MessageLevel.Warning)
        return values, alt_values, field_descriptions

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

    #: Appended to an occurrence's ordinary key to store its alternative
    #: description alongside the already-remembered primary one, in the
    #: SAME JSON blob under the SAME project entry - see remember_alt().
    #:
    #: This is deliberately NOT a nested structure (e.g.
    #: {key: {"description": ..., "alt": ...}}) - _key() is what an older
    #: install (v1.4, with no notion of an alternative description at all)
    #: still reconstructs when it calls recall(), and that older code's
    #: _load() blindly does ``str(v) for v in parsed.values()``. A nested
    #: value would stringify into Python's dict repr instead of the plain
    #: description text, breaking that install's own read mode. Keeping the
    #: alternative under its own SIBLING key - a plain string like every
    #: other entry - means an older install simply never looks for it (it
    #: only ever reconstructs the un-suffixed key) while still carrying it
    #: through untouched the next time it loads-merges-and-rewrites the
    #: whole dict (as remember() always does): the alternative "waits" in
    #: the project file for whichever install next understands it, without
    #: ever disturbing the primary description any install already reads.
    _ALT_SUFFIX = "\x1f\x02alt"

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
    def remember_alt(
        cls,
        table: str,
        field: str,
        code: str,
        occurrence: int,
        context: str,
        alt_description: str,
    ) -> None:
        """Persist an occurrence's alternative description alongside its
        already-remembered primary one (see ``_ALT_SUFFIX``).

        Called automatically by ``_pick_label()`` the first time alternative
        mode actually renders a value the user already picked a (primary)
        description for - so an already-selected value/description pair
        never needs reselecting once an alternative-description column is
        configured, and the alternative becomes part of the saved project
        the next time it is saved, for any colleague's install to pick up.

        A no-op without a primary choice already recorded for the same
        occurrence: there is nothing to attach an alternative to otherwise -
        this only ever enriches an existing pair, never invents one.
        """
        if not alt_description:
            return
        cls._ensure_hook()
        data = cls._load()
        primary_key = cls._key(table, field, code, occurrence, context)
        if primary_key not in data:
            return
        alt_key = primary_key + cls._ALT_SUFFIX
        if data.get(alt_key) == alt_description:
            return
        data[alt_key] = alt_description
        try:
            import json

            from qgis.core import QgsProject

            QgsProject.instance().writeEntry(cls.SCOPE, cls.KEY, json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            _log(f"Could not persist alternative description: {exc}")

    @classmethod
    def recall_alt(
        cls, table: str, field: str, code: str, occurrence: int = 0, context: str = ""
    ) -> str:
        """The persisted alternative description for this occurrence, or ""
        if none has been recorded yet. Rendering itself always uses the
        live lookup-table value (see ``_pick_label``) rather than this -
        the persisted copy exists for portability across installs, not as a
        competing source of truth.
        """
        key = cls._key(table, field, code, occurrence, context) + cls._ALT_SUFFIX
        return cls._load().get(key, "")

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
    mode: str = "primary",
    alt_mapping: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
    field_descriptions: Optional[Dict[str, str]] = None,
) -> str:
    """Replace value codes with descriptions, for display only.

    Each literal is attributed to the nearest **preceding** quoted field, which
    is how the expression reads (``"F_ATT" IN ('610', '607')``). That is what
    keeps ``"OTHER" = '610'`` untouched.

    When a code has several meanings under the same field, the choice the user
    made in the completion popup decides. See ``_pick_label``.

    ``mode="alt"`` renders each value's alternative description instead of its
    primary one - see ``_pick_label``. ``field_descriptions``, when given,
    additionally replaces each quoted field reference itself with its own
    description, e.g. ``"TYPE"`` becomes ``type's description`` - the same
    relationship a value has to its description, not an annotation alongside
    it - independent of ``mode``, since a field's own description is not a
    primary/alternative pair, just one label.
    """
    field_descriptions = field_descriptions or {}
    if not mapping and not field_descriptions:
        return text

    out: List[str] = []
    last_end = 0
    current_field = ""
    counters: Dict[tuple, int] = {}

    for match in _SCAN_RE.finditer(text):
        field = match.group("field")
        if field is not None:
            current_field = field.strip().lower()
            field_desc = field_descriptions.get(current_field)
            if field_desc:
                # Replaces the whole "FIELDNAME", quotes included - exactly
                # like a value's code disappears in favour of its
                # description, not shown alongside it. No parentheses:
                # this is the field's name AS the read-mode text, the same
                # relationship a value has to its description.
                out.append(text[last_end:match.start()])
                out.append(field_desc)
                last_end = match.end()
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

        alt_for_code = (alt_mapping or {}).get(current_field, {}).get(code, {})
        label = _pick_label(candidates, table, current_field, code, index, context, mode, alt_for_code)
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
    mode: str = "primary",
    alt_for_code: Optional[Dict[str, str]] = None,
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

    ``mode="alt"`` renders whichever description ends up chosen through its
    own alternative text instead (``alt_for_code``, keyed by the primary
    description) - falling back to the primary text when that particular row
    has no alternative of its own, so alternative mode never looks broken for
    an entry the alternative column simply has nothing to say about.

    This is deliberately what makes a remembered choice made before an
    alternative-description column even existed "just work" once one is
    added: the remembered value is always the PRIMARY description text (see
    ChoiceMemory), and the alternative is looked up fresh, by that same text,
    every time - never stored alongside the choice itself for RENDERING.
    Nothing needs migrating; the very next read of alternative mode already
    reflects it. It is, however, additionally persisted (see
    ChoiceMemory.remember_alt()) the moment it is resolved for a
    already-remembered occurrence - not to decide what is shown, only so the
    alternative travels with the project file itself from then on, for a
    colleague on a different install (or version) of the plugin.
    """
    alt_for_code = alt_for_code or {}

    def _render(description: str, persist: bool) -> str:
        rendered = normalize_code(description)
        if mode != "alt":
            return rendered
        alt = alt_for_code.get(description, "")
        if not alt:
            return rendered
        if persist:
            try:
                ChoiceMemory.remember_alt(table, field, code, occurrence, context, alt)
            except Exception:
                pass
        return normalize_code(alt)

    # Checked BEFORE the single-candidate shortcut, not just for an
    # ambiguous code: a value the user picked from the popup is remembered
    # regardless of whether it was ambiguous at the time, and only a
    # genuinely remembered occurrence is eligible for the automatic
    # alternative-description persistence above.
    remembered = ChoiceMemory.recall(table, field, code, occurrence, context)
    if remembered and remembered in candidates:
        return _render(remembered, persist=True)

    if len(candidates) == 1:
        return _render(candidates[0], persist=False)

    if mode == "alt":
        # dict.fromkeys(): de-duplicated, order-preserving - two distinct
        # primary descriptions can share one alternative, or both fall back
        # to their own (different) primary text.
        return " / ".join(dict.fromkeys(_render(candidate, persist=False) for candidate in candidates))
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
            self._switch = SlideSwitch(editor, mode_count=self._mode_count())
            self._switch.modeChanged.connect(self._on_mode_changed)
            self._switch.show()
            self._switch.raise_()
            editor.installEventFilter(self)
            self._reserve_strip(True)
            # The switch sits in the bottom-left corner of the *whole* editor
            # widget, not just its viewport - exactly where a horizontal
            # scrollbar is drawn once one appears (narrowing the dialog, or a
            # long unwrapped line). rangeChanged is what actually flips a
            # scrollbar between hidden and shown, so it - not just resizes -
            # is what needs to trigger a reposition.
            try:
                hbar = editor.horizontalScrollBar()
                if hbar is not None:
                    hbar.rangeChanged.connect(self._reposition)
            except Exception:
                pass
            self._reposition()
            # Apply the configured default mode once the dialog has settled.
            # Only ever the primary read mode (mode 1) - there is no separate
            # setting for defaulting straight into alternative mode.
            if Settings.default_read_mode():
                from qgis.PyQt.QtCore import QTimer

                QTimer.singleShot(0, lambda: self._switch and self._switch.setMode(1))
        except Exception as exc:
            _log(f"Read mode unavailable: {exc}", Qgis.MessageLevel.Warning)
            self._switch = None

    # -- availability ------------------------------------------------------ #

    @staticmethod
    def _feature_available() -> bool:
        """Only offer read mode when there is something to substitute -
        either value descriptions, or field-name descriptions on their own."""
        try:
            usable, _ = Settings.autocomplete_is_usable()
            return bool(usable and (Settings.field("description") or Settings.field("field_description")))
        except Exception:
            return False

    @staticmethod
    def _mode_count() -> int:
        """2 (edit/read), or 3 once an alternative value description column
        is configured - see SlideSwitch."""
        try:
            return 3 if Settings.field("alt_description") else 2
        except Exception:
            return 2

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
            # A horizontal scrollbar - the overlay's own; the underlying
            # editor's native one is suppressed in RtlOverlayEditor - is drawn
            # below the viewport, inside the same bottom-left corner the
            # switch occupies. Shift the switch up by its height so the two
            # never overlap, instead of leaving the switch sitting on top of
            # (or under) the scrollbar.
            clearance = 0
            hbar = self._editor.horizontalScrollBar()
            if hbar is not None and hbar.isVisible():
                clearance = hbar.height()
            self._switch.move(
                margin,
                self._editor.height() - self._switch.height() - margin - clearance,
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

    def _on_mode_changed(self, mode: int) -> None:
        try:
            if mode == 0:
                self._leave_read_mode()
            else:
                self._enter_read_mode(alt=(mode == 2))
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

    def _enter_read_mode(self, alt: bool = False) -> None:
        editor = self._editor
        if editor is None:
            return

        # Only capture the authoritative text and read-only state on the
        # actual edit -> read transition. Switching directly between read and
        # alternative read (mode 1 <-> mode 2, without passing back through
        # edit mode) must recompute the preview under the new mode without
        # touching either - re-capturing here would take the PREVIEW itself
        # as the "original" text, corrupting it.
        if not self._active:
            sci = getattr(editor, "_sci", None)
            try:
                self._original = sci.text() if sci is not None else editor.toPlainText()
                self._original = self._original.replace("\r\n", "\n").replace("\r", "\n")
            except Exception:
                self._original = editor.toPlainText()
            self._was_read_only = editor.isReadOnly()

        sci = getattr(editor, "_sci", None)
        from .rtl_autocomplete import resolve_table_candidates

        tables = resolve_table_candidates(sci if sci is not None else editor)
        values, alt_values, field_descriptions = DescriptionResolver.mapping(tables)
        preview = substitute_descriptions(
            self._original or "",
            values,
            tables[0] if tables else "",
            expression_context_key(sci if sci is not None else editor),
            mode="alt" if alt else "primary",
            alt_mapping=alt_values,
            field_descriptions=field_descriptions,
        )

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
                hbar = self._editor.horizontalScrollBar()
                if hbar is not None:
                    hbar.rangeChanged.disconnect(self._reposition)
        except Exception:
            pass
        if self._switch is not None:
            try:
                self._switch.deleteLater()
            except Exception:
                pass
            self._switch = None
        self._editor = None

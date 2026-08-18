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

import difflib
import re
from pathlib import Path
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


#: Marks the plugin's own hidden expression-identity comment, distinguishing
#: it from any comment the user might have written themselves. Block-comment
#: syntax ("/* */"), not "--": it works inside a single-line expression
#: without needing to run to the end of the line, and is accepted both by
#: QGIS's own expression grammar and by the SQL dialects most providers
#: evaluate a layer filter as. Contains no character XML needs to escape
#: ("<", ">", "&", quotes), so it round-trips through a saved project file
#: verbatim - no encoding mismatch to worry about when searching for it.
_EID_TAG = "rtl-eid"
_EID_COMMENT_RE = re.compile(r"^/\*" + re.escape(_EID_TAG) + r":([0-9a-f]{16})\*/[ \t]*\n?")


def new_eid() -> str:
    """A short, effectively-unique id for one expression instance."""
    import uuid

    return uuid.uuid4().hex[:16]


def make_eid_comment(eid: str) -> str:
    """The literal text inserted as an expression's first line."""
    return f"/*{_EID_TAG}:{eid}*/\n"


def extract_eid(text: str) -> str:
    """The expression-identity id at the very start of ``text``, or "" if
    it does not start with one of this plugin's own id comments."""
    match = _EID_COMMENT_RE.match(text)
    return match.group(1) if match else ""


def _eid_marker(eid: str) -> str:
    """What to search a saved project's text for - see
    ``_read_project_text_for_scan()``."""
    return f"{_EID_TAG}:{eid}"


def _read_project_text_for_scan() -> Optional[str]:
    """The current project's saved file content, as plain text.

    Used only to check whether a specific expression-identity comment (see
    ``make_eid_comment()``) still appears anywhere in the project - never
    to parse or rely on any particular structure, which is what keeps this
    unaffected by QGIS's internal XML schema changing between versions.

    Saves the project first, so the text reflects the CURRENT, on-screen
    state rather than whatever was last on disk before this call.

    Returns ``None`` when there is nothing usable to read - the project has
    never been saved to a file yet, or writing/reading it failed - callers
    MUST treat that as "cannot verify right now", never as "not found".
    """
    try:
        from qgis.core import QgsProject

        project = QgsProject.instance()
        path = project.fileName()
        if not path:
            return None
        if not project.write():
            return None
        location = Path(path)
        if location.suffix.lower() == ".qgz":
            import zipfile

            with zipfile.ZipFile(location) as archive:
                for name in archive.namelist():
                    if name.lower().endswith(".qgs"):
                        return archive.read(name).decode("utf-8", errors="replace")
            return None
        return location.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        _log(f"Could not read the project file for Clear & Scan: {exc}", Qgis.MessageLevel.Warning)
        return None


def _humanize_dd_button_name(name: str) -> str:
    """``"mFillColorDDBtn"`` -> ``"Fill Color"`` - strips the "m" Qt naming
    convention prefix and the "DDBtn" (data-defined button) suffix, then
    splits the remaining CamelCase into words. Best-effort, for a Clear &
    Scan failure message only - never used to verify anything."""
    core = name
    if core.endswith("DDBtn"):
        core = core[: -len("DDBtn")]
    if len(core) > 1 and core[0] == "m" and core[1].isupper():
        core = core[1:]
    words = re.findall(r"[A-Z][a-z0-9]*|[A-Z]+(?![a-z])|[a-z0-9]+", core)
    return " ".join(words) if words else name


def _describe_context(context: str) -> str:
    """A best-effort, human-readable description of where a remembered
    choice came from, for Clear & Scan's failure messages only.

    Built entirely from the context string already recorded live when the
    choice was made (see ``expression_context_key()``) - this never tries
    to re-derive or verify anything, just to make an existing opaque
    identifier readable.
    """
    if not context:
        return "an unknown location"
    parts = context.split("|")

    layer_label = ""
    layer_id = parts[-1] if parts else ""
    if layer_id:
        try:
            from qgis.core import QgsProject

            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None:
                layer_label = layer.name()
        except Exception:
            pass
        if not layer_label:
            layer_label = layer_id

    # The most specific hint available: a data-defined override button's
    # own object name, e.g. "mFillColorDDBtn" -> "Fill Color". Falls back
    # to the window title (e.g. "Query Builder" for a layer filter, still
    # meaningful on its own) when no such button is found in the chain.
    slot_label = ""
    for piece in parts[:-1]:
        for chunk in piece.split(">"):
            if chunk.endswith("DDBtn"):
                slot_label = _humanize_dd_button_name(chunk)
                break
        if slot_label:
            break
    if not slot_label and len(parts) >= 2 and parts[1]:
        slot_label = parts[1]

    if layer_label and slot_label:
        return f'layer "{layer_label}", {slot_label}'
    if layer_label:
        return f'layer "{layer_label}"'
    return slot_label or "an unknown location"


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

    #: Same sibling-key trick as _ALT_SUFFIX, this time carrying the
    #: expression-identity id (see make_eid_comment()) an occurrence's own
    #: expression was tagged with, when it was. An older install (or an
    #: entry recorded before this existed) simply never has this sibling
    #: key at all - that absence is exactly what clear_and_scan() uses to
    #: tell "can be verified precisely" apart from "falls back to the
    #: older, coarser check".
    _EID_SUFFIX = "\x1f\x03eid"

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
        eid: str = "",
    ) -> None:
        """Record a choice for one occurrence and persist it into the project.

        ``eid`` - the expression-identity id its expression was tagged with
        (see ``make_eid_comment()``), when there is one - is stored as its
        own sibling key (see ``_EID_SUFFIX``), the same non-destructive
        pattern already used for the alternative description: an older
        install simply never looks for it and carries it through untouched.
        """
        if not (field and code and description):
            return
        cls._ensure_hook()
        data = cls._load()
        key = cls._key(table, field, code, occurrence, context)
        eid_key = key + cls._EID_SUFFIX
        description_changed = data.get(key) != description
        eid_changed = bool(eid) and data.get(eid_key) != eid
        if not description_changed and not eid_changed:
            return
        data[key] = description
        if eid:
            data[eid_key] = eid
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
    def recall_eid(
        cls, table: str, field: str, code: str, occurrence: int = 0, context: str = ""
    ) -> str:
        """The expression-identity id this occurrence's expression was
        tagged with, or "" if none was ever recorded (an entry from before
        this existed, or one whose expression was never re-selected since).
        """
        key = cls._key(table, field, code, occurrence, context) + cls._EID_SUFFIX
        return cls._load().get(key, "")

    @classmethod
    def forget(cls, table: str, field: str, code: str, occurrence: int, context: str = "") -> None:
        """Remove one occurrence's entries (primary, alternative and
        expression-identity id) entirely - used by ``reconcile_choices()``
        to clear a slot before rewriting it, and to drop one that no
        longer corresponds to anything in the current expression at all."""
        cls._ensure_hook()
        data = cls._load()
        primary_key = cls._key(table, field, code, occurrence, context)
        changed = False
        for key in (primary_key, primary_key + cls._ALT_SUFFIX, primary_key + cls._EID_SUFFIX):
            if key in data:
                del data[key]
                changed = True
        if not changed:
            return
        try:
            import json

            from qgis.core import QgsProject

            QgsProject.instance().writeEntry(cls.SCOPE, cls.KEY, json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            _log(f"Could not forget a stale choice: {exc}")

    @classmethod
    def purge_for_layer(cls, layer_id: str) -> int:
        """Remove every entry whose context references ``layer_id`` -
        called when a layer is removed from the project (see
        ``RtlBidiEditorPlugin``'s ``layersWillBeRemoved`` hook), so its
        remembered choices do not sit in the project file forever with no
        layer left that could ever reconcile them away.

        ``reconcile_choices()`` only ever runs when a specific expression's
        own dialog is accepted - it has no way to notice a layer (and every
        expression slot on it) disappearing entirely. This is the
        complementary cleanup for that case, precise rather than a general
        heuristic sweep: ``expression_context_key()`` always appends the
        layer's id as the LAST part of the context before the field/value
        portion (see its own docstring), so a substring check against a
        known-real, just-removed layer id can only ever match an entry
        that genuinely belongs to that layer - never a false positive from
        guessing at an opaque context string's structure.

        Returns how many keys were removed.
        """
        if not layer_id:
            return 0
        cls._ensure_hook()
        data = cls._load()
        to_remove = [key for key in data if layer_id in key]
        if not to_remove:
            return 0
        for key in to_remove:
            del data[key]
        try:
            import json

            from qgis.core import QgsProject

            QgsProject.instance().writeEntry(cls.SCOPE, cls.KEY, json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            _log(f"Could not purge choices for a removed layer: {exc}")
        return len(to_remove)

    @classmethod
    def _parse_key(cls, key: str) -> Optional[Tuple[str, str, str, str, int]]:
        """The inverse of ``_key()``: ``(context, table, field, code,
        occurrence)``, or ``None`` for anything that is not a plain primary
        key - an alternative-description or expression-identity shadow
        entry, or something not in this shape at all."""
        if key.endswith(cls._ALT_SUFFIX) or key.endswith(cls._EID_SUFFIX):
            return None
        context, sep, rest = key.partition("\x1e")
        if not sep:
            return None
        parts = rest.split("\x1f")
        if len(parts) != 4:
            return None
        table, field, code, occurrence_text = parts
        try:
            occurrence = int(occurrence_text)
        except ValueError:
            return None
        return context, table, field, code, occurrence

    @classmethod
    def _delete_conceptual_entry(
        cls, data: Dict[str, str], context: str, table: str, field: str, code: str, occurrence: int
    ) -> None:
        """Remove one occurrence's primary, alternative and
        expression-identity keys from an already-loaded ``data`` dict, in
        place - the batch counterpart to ``forget()``, which reads/writes
        the project once per call and would be wasteful called once per
        entry from ``clear_and_scan()``."""
        primary_key = cls._key(table, field, code, occurrence, context)
        data.pop(primary_key, None)
        data.pop(primary_key + cls._ALT_SUFFIX, None)
        data.pop(primary_key + cls._EID_SUFFIX, None)

    #: The window's own objectName - not its (possibly translated)
    #: windowTitle - that identifies a layer-FILTER expression context (the
    #: one place clear_and_scan() can independently re-check without a live
    #: dialog open, since a layer's current filter text is always just
    #: layer.subsetString()). objectNames are set in code, so this does not
    #: vary by the user's QGIS locale the way windowTitle would. Best-effort
    #: across QGIS versions - an unrecognised context is simply never
    #: treated as verifiable, never incorrectly treated as unused.
    _LAYER_FILTER_CONTEXT_MARKERS = ("QgsQueryBuilderBase", "QgsQueryBuilder", "QgsQueryBuilderDialog")

    @classmethod
    def clear_and_scan(cls) -> Tuple[int, int, List[str]]:
        """The Settings dialog's Clear & Scan action.

        Two independent passes over every remembered choice in the project:

        1. DELETE anything provably no longer relevant. Two ways an entry
           can be proven dead:

           * **It carries an expression-identity id** (see
             ``make_eid_comment()`` - every expression a choice is recorded
             for is tagged with one, from now on) - deleted if no
             expression anywhere in the saved project still carries that
             exact id. This is precise regardless of what KIND of
             expression it was - a filter, a data-defined override, a
             labeling rule filter - because the id travels with the
             expression itself, not with which dialog it happened to be
             edited through.
           * **It has no id at all** - an entry recorded before this
             existed. Falls back to the older, coarser checks: the layer it
             belonged to is no longer in the project at all, or it is a
             layer FILTER choice specifically (the one context re-checkable
             without an id - see ``_LAYER_FILTER_CONTEXT_MARKERS``) and that
             exact occurrence no longer exists in the layer's current
             filter text. Anything else without an id is left alone -
             unverifiable is never treated as unused.

        2. For everything NOT deleted, compare its field/code/description
           (and alternative description, if one was recorded) against what
           the configured lookup layer says RIGHT NOW, and report anything
           that no longer matches as a warning, without touching it - this
           is what surfaces a database-backed lookup table having changed
           since a choice was made, which nothing else in the plugin would
           ever notice on its own. Each failure names the layer and, where
           known, which specific slot (e.g. "Fill Color") it came from -
           never the id itself, which is purely internal bookkeeping.

        Returns ``(deleted_count, total_count, failures)``. Counts are of
        whole choices - a primary entry plus its optional alternative and
        id counts as one - not raw JSON keys.
        """
        cls._ensure_hook()
        data = cls._load()

        from qgis.core import QgsProject

        project = QgsProject.instance()

        entries: List[Tuple[str, str, str, str, int]] = []
        seen = set()
        for key in list(data):
            parsed = cls._parse_key(key)
            if parsed is not None and parsed not in seen:
                seen.add(parsed)
                entries.append(parsed)

        total = len(entries)
        deleted = 0
        failures: List[str] = []
        mapping_cache: Dict[str, ReadModeMapping] = {}
        # Read (and save) once for the whole run, not once per entry - and
        # only if at least one entry actually has an id to look for, so a
        # project full of only legacy (no-id) entries never pays for a
        # save it has no use for.
        project_text = (
            _read_project_text_for_scan()
            if any(data.get(cls._key(t, f, c, o, ctx) + cls._EID_SUFFIX) for ctx, t, f, c, o in entries)
            else None
        )

        def _mapping_for(table_name: str) -> ReadModeMapping:
            cache_key = table_name.lower()
            if cache_key not in mapping_cache:
                mapping_cache[cache_key] = DescriptionResolver.mapping([table_name] if table_name else [])
            return mapping_cache[cache_key]

        for context, table, field, code, occurrence in entries:
            primary_key = cls._key(table, field, code, occurrence, context)
            eid = data.get(primary_key + cls._EID_SUFFIX, "")

            if eid:
                # Precise path: does an expression carrying this exact id
                # still exist anywhere in the saved project?
                if project_text is not None and _eid_marker(eid) not in project_text:
                    cls._delete_conceptual_entry(data, context, table, field, code, occurrence)
                    deleted += 1
                    continue
                # project_text is None: never saved yet, or the save/read
                # failed this run - cannot verify, so fall through to the
                # lookup-table check below without deleting anything.
            else:
                # No id recorded - predates this feature. Same coarser
                # checks Clear & Scan has always used.
                layer_id = context.rsplit("|", 1)[-1] if context else ""
                layer = project.mapLayer(layer_id) if layer_id else None

                if layer_id and layer is None:
                    cls._delete_conceptual_entry(data, context, table, field, code, occurrence)
                    deleted += 1
                    continue

                if layer is not None and context.split("|", 1)[0] in cls._LAYER_FILTER_CONTEXT_MARKERS:
                    live_matches = _scan_value_literals(layer.subsetString() or "")
                    still_present = any(
                        f == field and c == code and occ == occurrence
                        for f, c, _start, _end, occ in live_matches
                    )
                    if not still_present:
                        cls._delete_conceptual_entry(data, context, table, field, code, occurrence)
                        deleted += 1
                        continue

            # Kept - either confirmed still in use, or simply unverifiable
            # from here - either way, check it against the lookup table.
            description = data.get(primary_key, "")
            values, alt_values, _field_descriptions = _mapping_for(table)
            field_key = field.lower()
            table_label = f"table \"{table}\"" if table else "the configured lookup table"
            where = _describe_context(context)

            field_values = values.get(field_key)
            if field_values is None:
                failures.append(f'{where}: field "{field}" no longer exists in {table_label}')
            elif code not in field_values:
                failures.append(
                    f'{where}: value {code} no longer exists for field "{field}" in {table_label}'
                )
            elif description and description not in field_values[code]:
                failures.append(
                    f'{where}: description "{description}" no longer describes value {code} '
                    f'(field "{field}" in {table_label})'
                )

            alt_description = data.get(primary_key + cls._ALT_SUFFIX, "")
            if alt_description:
                current_alt = alt_values.get(field_key, {}).get(code, {}).get(description, "")
                if current_alt != alt_description:
                    failures.append(
                        f'{where}: alternative description "{alt_description}" no longer matches '
                        f'value {code} (description "{description}", field "{field}" in {table_label})'
                    )

        if deleted:
            try:
                import json

                QgsProject.instance().writeEntry(cls.SCOPE, cls.KEY, json.dumps(data, ensure_ascii=False))
            except Exception as exc:
                _log(f"Could not write back after Clear & Scan: {exc}")

        return deleted, total, failures

    @classmethod
    def reset_legacy_entries(cls) -> Tuple[int, int]:
        """Delete every remembered choice that has no expression-identity
        id at all - i.e. everything recorded before that existed.

        A deliberate, one-time, explicit action - never something
        ``clear_and_scan()`` does on its own, since an entry missing an id
        only ever means "predates this feature", not "no longer needed";
        treating those the same would silently delete still-valid choices
        the very first time this ships (see ``clear_and_scan()``'s own
        docstring). Meant to be run once, by choice, to start fresh - e.g.
        right after upgrading a project that already has a lot of
        pre-existing entries, so every choice from then on can be
        precisely tracked.

        Returns ``(deleted_count, total_count)`` - counts of whole
        choices, not raw JSON keys.
        """
        cls._ensure_hook()
        data = cls._load()

        entries: List[Tuple[str, str, str, str, int]] = []
        seen = set()
        for key in list(data):
            parsed = cls._parse_key(key)
            if parsed is not None and parsed not in seen:
                seen.add(parsed)
                entries.append(parsed)

        total = len(entries)
        deleted = 0
        for context, table, field, code, occurrence in entries:
            primary_key = cls._key(table, field, code, occurrence, context)
            if primary_key + cls._EID_SUFFIX in data:
                continue  # has an id - not legacy, clear_and_scan() owns this one
            cls._delete_conceptual_entry(data, context, table, field, code, occurrence)
            deleted += 1

        if deleted:
            try:
                import json

                from qgis.core import QgsProject

                QgsProject.instance().writeEntry(cls.SCOPE, cls.KEY, json.dumps(data, ensure_ascii=False))
            except Exception as exc:
                _log(f"Could not write back after resetting legacy entries: {exc}")

        return deleted, total

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


def _scan_value_literals(text: str) -> List[Tuple[str, str, int, int, int]]:
    """Every value literal in ``text``, as ``(field, code, start, end,
    occurrence)`` - the same field-attribution and occurrence-counting rule
    ``substitute_descriptions()``/``occurrence_index()`` use, just also
    keeping each literal's exact character span (quotes included), which is
    what ``reconcile_choices()`` needs to line up two versions of the same
    expression.
    """
    results: List[Tuple[str, str, int, int, int]] = []
    current_field = ""
    counters: Dict[Tuple[str, str], int] = {}
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
        if not current_field or not code:
            continue
        key = (current_field, code)
        occurrence = counters.get(key, 0)
        counters[key] = occurrence + 1
        results.append((current_field, code, match.start(), match.end(), occurrence))
    return results


def _map_span(
    blocks: List[Tuple[int, int, int]], start: int, end: int
) -> Optional[Tuple[int, int]]:
    """Where ``text_a[start:end]`` ends up in ``text_b``, if it survived
    unchanged - i.e. it falls entirely within one of ``blocks`` (from
    ``difflib.SequenceMatcher.get_matching_blocks()``). ``None`` if the span
    was touched by an edit (a delete, insert or replace) at all.
    """
    for i, j, size in blocks:
        if size and start >= i and end <= i + size:
            offset = start - i
            return (j + offset, j + offset + (end - start))
    return None


def reconcile_choices(context: str, table: str, before_text: str, after_text: str) -> None:
    """Rewrite ``ChoiceMemory`` so its entries exactly match ``after_text``,
    given what the same expression looked like before (``before_text``).

    Two things happen, for every ``(field, code)`` pair either text
    mentions:

    * a remembered choice for a literal that SURVIVED the edit unchanged -
      its exact characters are still there, just possibly at a different
      occurrence index now (e.g. because another instance of the same
      code was inserted earlier in the expression) - is MOVED to its new,
      correct occurrence index, rather than left stranded under the old
      one. Left stranded, it would either never be recalled again (a
      silent, permanent loss of the user's choice) or - worse - end up
      wrongly attributed to a *different* literal that now happens to
      share that old occurrence number.
    * anything left over after that - a choice for an occurrence that no
      longer corresponds to any literal at all, because it (or the whole
      clause it was in) was removed or replaced - is deleted outright,
      instead of accumulating in the project file forever.

    A CHARACTER-LEVEL diff (``difflib``, not a per-field/code diff, which
    cannot tell two identical literals apart at all) is what identifies
    survivors: longest-common-subsequence matching naturally uses each
    literal's surrounding, unchanged context to decide which specific
    occurrence in ``after_text`` a given occurrence in ``before_text``
    corresponds to - the same way a text editor's own diff view lines up
    unchanged lines around a small edit.

    Meant to run once, when the surrounding dialog is accepted (OK) - see
    ``ChoiceReconciler`` - comparing the expression as it stood when that
    editing session began against its final, saved form. Since only the
    NET difference between those two matters here, this is correct
    regardless of how many separate edits happened in between.
    """
    if before_text == after_text:
        return  # nothing could possibly have moved or disappeared

    before_matches = _scan_value_literals(before_text)
    after_matches = _scan_value_literals(after_text)
    if not before_matches and not after_matches:
        return

    blocks = difflib.SequenceMatcher(None, before_text, after_text, autojunk=False).get_matching_blocks()
    after_field_by_span = {(s, e): (f, c, occ) for f, c, s, e, occ in after_matches}

    # (field, code) -> {old_occurrence: new_occurrence}, for literals whose
    # exact characters survived the edit at all.
    moves: Dict[Tuple[str, str], Dict[int, int]] = {}
    for field, code, start, end, old_occurrence in before_matches:
        target = _map_span(blocks, start, end)
        if target is None:
            continue
        after_hit = after_field_by_span.get(target)
        if after_hit is None:
            continue
        after_field, after_code, new_occurrence = after_hit
        if after_field != field or after_code != code:
            continue  # identical characters but a different parse - be conservative
        moves.setdefault((field, code), {})[old_occurrence] = new_occurrence

    before_counts: Dict[Tuple[str, str], int] = {}
    for field, code, _s, _e, occurrence in before_matches:
        before_counts[(field, code)] = max(before_counts.get((field, code), 0), occurrence + 1)
    after_counts: Dict[Tuple[str, str], int] = {}
    for field, code, _s, _e, occurrence in after_matches:
        after_counts[(field, code)] = max(after_counts.get((field, code), 0), occurrence + 1)

    pairs = set(before_counts) | set(after_counts)
    for field, code in pairs:
        old_count = before_counts.get((field, code), 0)
        pair_moves = moves.get((field, code), {})

        # Read out whatever is currently recorded for every OLD occurrence
        # BEFORE anything is deleted - forget() below would otherwise erase
        # a choice this same pass still needs to carry forward.
        to_place: Dict[int, Tuple[str, str]] = {}
        for old_occurrence in range(old_count):
            description = ChoiceMemory.recall(table, field, code, old_occurrence, context)
            if not description:
                continue
            new_occurrence = pair_moves.get(old_occurrence)
            if new_occurrence is None:
                continue  # this literal did not survive - nothing to carry forward
            alt = ChoiceMemory.recall_alt(table, field, code, old_occurrence, context)
            to_place[new_occurrence] = (description, alt)

        # Clear only OLD occurrences that this pass actually owns: ones
        # whose literal is gone entirely, or that survived but moved to a
        # different occurrence number (so their old slot must be vacated
        # before the move below rewrites it elsewhere). An occurrence that
        # survived AND stayed at the same number needs neither.
        #
        # Deliberately NEVER touches an occurrence index that only exists
        # in after_text (>= old_count with no entry in pair_moves) - that is
        # a choice made fresh during THIS SAME editing session (recorded
        # live as it was picked from the popup), not something this pass
        # has any business erasing just because it did not exist in
        # before_text. The previous version cleared range(max(old_count,
        # new_count)) unconditionally, which wiped exactly those - deleting
        # a value's remembered description the instant the surrounding
        # dialog was accepted.
        for old_occurrence in range(old_count):
            if pair_moves.get(old_occurrence) == old_occurrence:
                continue  # unchanged position - nothing to clear
            ChoiceMemory.forget(table, field, code, old_occurrence, context)

        for new_occurrence, (description, alt) in to_place.items():
            ChoiceMemory.remember(table, field, code, description, new_occurrence, context)
            if alt:
                ChoiceMemory.remember_alt(table, field, code, new_occurrence, context, alt)


#: Left-to-Right Mark (U+200E) - zero-width, no glyph of its own, but a
#: "strong LTR" character as far as the Unicode Bidi Algorithm is concerned.
#: See force_ltr_paragraphs() for why read mode needs it. Spelled as an
#: escape, not pasted as a literal invisible character, so it survives
#: editors/diffs/encodings that might otherwise silently mangle it.
_LRM = "‎"


def force_ltr_paragraphs(text: str) -> str:
    """Pin every paragraph (line) of read-mode preview text to an overall
    left-to-right layout, regardless of which script its first character
    happens to be - without changing a single visible character.

    Qt determines a QTextDocument paragraph's bidi base direction from its
    own first STRONG character (see RtlOverlayEditor's own comment on this -
    it is exactly what lets a typed expression like ``"F_CODE" = 'בית כנסת'``
    render correctly: the first strong character is the ``F`` of the field
    name, so the paragraph resolves as left-to-right and the embedded Hebrew
    literal is simply an RTL "island" within it, in its correct place).

    A read-mode substitution can replace the FIELD NAME itself - normally
    the very first token of the expression - with an RTL (e.g. Hebrew)
    description. That silently makes an RTL character the paragraph's
    FIRST strong character instead, flipping the WHOLE line's base
    direction to RTL. Once that happens, the Unicode Bidi Algorithm does
    not just render that one word right-to-left (correct, expected) - it
    also visually mirrors the LTR/neutral structure around it: an
    operator's position, and the order of a comma-separated list, both
    end up reversed relative to the expression's own source order, even
    though every individual word (English or Hebrew) is still spelled
    correctly within itself.

    Inserting a Left-to-Right Mark as the first character of every
    paragraph anchors its base direction to LTR - matching how the
    expression is actually written and edited in QGIS - regardless of what
    script the first substituted token happens to be in. Every RTL run
    (a Hebrew description) still renders correctly right-to-left WITHIN
    itself; only the overall left-to-right ordering of the expression's own
    structure is pinned, exactly matching the original, unsubstituted text's
    layout.
    """
    if not text:
        return text
    return _LRM + text.replace("\n", "\n" + _LRM)


#: First Strong Isolate / Pop Directional Isolate (U+2068 / U+2069) - the
#: Unicode-recommended way to embed a run of text whose own script is
#: unknown or mixed inside surrounding text, without letting its direction
#: leak out and affect its neighbours. See _isolate() for why a single
#: paragraph-level LRM (force_ltr_paragraphs) is not enough on its own.
_FSI = "⁨"
_PDI = "⁩"


def _isolate(label: str) -> str:
    """Wrap one substituted label so it never bidi-merges with whatever
    substituted label sits next to it.

    force_ltr_paragraphs() pins the overall PARAGRAPH direction, but that
    alone does not stop two adjacent RTL runs, separated only by a neutral
    character (a comma, a space, "="), from being treated as ONE combined
    bidi run and reordered as a unit: in "מבנה דת, מבנה חקלאי", the comma
    between two Hebrew phrases resolves to the SAME direction as its
    neighbours, extending the RTL run across it - so the Unicode Bidi
    Algorithm can still visually swap the two phrases relative to each
    other even though the line as a whole is anchored left-to-right.

    Wrapping each substituted label individually in an isolate (rather
    than just relying on the paragraph-level mark) is what actually
    prevents that: per the Unicode bidi spec, an isolate is treated as one
    opaque, neutral unit by everything OUTSIDE it, so the structural
    characters around it (operators, commas, parentheses) always resolve
    against the surrounding left-to-right paragraph, never against
    whatever direction happens to be inside a neighbouring label. Each
    label's OWN text still resolves its own internal direction normally -
    Hebrew still reads right-to-left within itself - only the relative
    ordering BETWEEN labels (and everything structural around them) is
    protected, no matter how many of them sit next to each other or how
    deeply the expression nests.
    """
    if not label:
        return label
    return _FSI + label + _PDI


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
                # relationship a value has to its description. Isolated
                # (see _isolate()) so it cannot bidi-merge with the value
                # label that follows it.
                out.append(text[last_end:match.start()])
                out.append(_isolate(field_desc))
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
        #
        # Isolated (see _isolate()) so that two of these sitting next to each
        # other - e.g. "2300, 2301" both becoming Hebrew descriptions inside
        # an IN (...) list - never bidi-merge into one run and swap order
        # relative to each other, no matter how many of them there are.
        out.append(text[last_end:match.start()])
        out.append(_isolate(label))
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

    # Each candidate isolated individually (see _isolate()) before joining -
    # otherwise two adjacent RTL meanings ("mosque / greenhouse" in Hebrew)
    # could bidi-merge across the " / " separator and swap order, the same
    # problem a comma-separated IN (...) list has.
    if mode == "alt":
        # dict.fromkeys(): de-duplicated, order-preserving - two distinct
        # primary descriptions can share one alternative, or both fall back
        # to their own (different) primary text.
        return " / ".join(dict.fromkeys(_isolate(_render(candidate, persist=False)) for candidate in candidates))
    return " / ".join(_isolate(normalize_code(candidate)) for candidate in candidates)


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
        # A substituted field or value can now start with an RTL (e.g.
        # Hebrew) description where the original expression had an LTR
        # token - see force_ltr_paragraphs() for why that alone would
        # otherwise flip the whole line's bidi base direction and visually
        # mirror its structure, not just that one word.
        preview = force_ltr_paragraphs(preview)

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


# --------------------------------------------------------------------------- #
# Choice reconciliation
# --------------------------------------------------------------------------- #


class ChoiceReconciler(QObject):
    """Rewrites ``ChoiceMemory`` to exactly match the final expression, once,
    when the surrounding dialog is accepted (OK - not Cancel, and not just
    Scintilla's own live "Apply" preview) - see ``reconcile_choices()`` for
    what that actually does and why it is needed.

    Without this, ``ChoiceMemory`` only ever gains entries and never loses
    or renumbers any: editing an expression so that a remembered value
    ends up at a different occurrence index (inserting another instance of
    the same code earlier in the expression, or removing one) leaves the
    old entry behind, unused, while the literal that now holds that old
    occurrence number gets that entry's description whether it matches or
    not. Over time - a lot of back-and-forth editing - this both bloats
    the project file with entries nothing recalls any more, and can show
    the wrong description for a value that was never ambiguous in the
    first place.

    Captures the expression's text as it stood when this editor was first
    attached - the baseline reconcile_choices() compares against once OK
    is pressed. Only the NET difference between those two points matters,
    so this is correct regardless of how many separate edits happened in
    between.
    """

    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor
        self._window = None
        try:
            sci = getattr(editor, "_sci", None)
            baseline = sci.text() if sci is not None else editor.toPlainText()
            self._baseline = baseline.replace("\r\n", "\n").replace("\r", "\n")
        except Exception:
            self._baseline = editor.toPlainText()

        try:
            window = editor.window()
            # Not every dialog this plugin attaches to necessarily exposes
            # QDialog.accepted (a degraded/unknown host window might not) -
            # checked explicitly rather than assumed, so a window that
            # lacks it simply never reconciles rather than raising.
            if window is not None and hasattr(window, "accepted"):
                window.accepted.connect(self._on_accepted)
                self._window = window
        except Exception as exc:
            _log(f"Choice reconciliation unavailable: {exc}", Qgis.MessageLevel.Info)

    def _on_accepted(self) -> None:
        editor = self._editor
        if editor is None:
            return
        try:
            sci = getattr(editor, "_sci", None)
            after_text = sci.text() if sci is not None else editor.toPlainText()
            after_text = after_text.replace("\r\n", "\n").replace("\r", "\n")

            from .rtl_autocomplete import resolve_table_candidates

            probe = sci if sci is not None else editor
            tables = resolve_table_candidates(probe)
            table = tables[0] if tables else ""
            context = expression_context_key(probe)
            reconcile_choices(context, table, self._baseline, after_text)
        except Exception as exc:
            _log(f"Choice reconciliation failed: {exc}", Qgis.MessageLevel.Warning)

    def teardown(self) -> None:
        """Safe to call more than once."""
        if self._window is not None:
            try:
                self._window.accepted.disconnect(self._on_accepted)
            except Exception:
                pass
            self._window = None
        self._editor = None

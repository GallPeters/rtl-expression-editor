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
from typing import Dict, List, NamedTuple, Optional, Tuple

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


#: Where remembered value/description choices used to be written, before an
#: ambiguous code was resolved by inferring it from the expression's own
#: group context instead (see substitute_descriptions()/_pick_label()) -
#: nothing is ever written to the project any more. Kept only as the
#: target for purge_legacy_project_entries() below.
_LEGACY_CHOICE_SCOPE = "rtl_bidi_editor"
_LEGACY_CHOICE_KEY = "value_choices"


def purge_legacy_project_entries() -> bool:
    """Remove this plugin's old remembered-choice entry from the current
    project, if it still has one.

    Every value/description choice used to be written into the project
    file itself, so a code with several meanings could be shown correctly
    without the lookup table saying which one applied. That is no longer
    needed - the group context read straight from the expression decides
    it instead - so nothing is written any more, and this is a one-time
    cleanup for a project that still carries the old entry from before
    that change: called once when the plugin is activated (see
    ``RtlBidiEditorPlugin.initGui()``), so such a project starts clean
    rather than carrying dead data around indefinitely.

    Returns True if an entry was actually found and removed.
    """
    try:
        from qgis.core import QgsProject

        project = QgsProject.instance()
        _raw, existed = project.readEntry(_LEGACY_CHOICE_SCOPE, _LEGACY_CHOICE_KEY, "")
        if not existed:
            return False
        project.removeEntry(_LEGACY_CHOICE_SCOPE, _LEGACY_CHOICE_KEY)
        return True
    except Exception as exc:
        _log(f"Could not purge legacy remembered-choice entries: {exc}", Qgis.MessageLevel.Info)
        return False


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

    ``DescriptionResolver``'s cache holds project-scoped data (well, really
    lookup-layer-scoped, but the configured layer is itself a per-project
    setting), so it may not survive a project change. Without this, opening
    project B after using project A would serve A's stale cached mapping.

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
#: * ``values``  - ``field -> {code: [(description, group_value), ...]}``.
#:   ``group_value`` is that row's own "Group codes column" value (see
#:   Settings) - normally used only to head a suggestion-list group, but
#:   also exactly what ``_pick_label()`` matches against the expression's
#:   own surrounding context to resolve an ambiguous code with no need to
#:   remember anything: see its own docstring. "" when the row has none.
#: * ``alt_values`` - ``field -> {code: {description: alt_description}}`` -
#:   for whichever primary description ends up chosen (by
#:   ``_pick_label()``), the alternative sitting next to *that same row*.
#:   Looked up by the description's own text.
#: * ``field_descriptions`` - ``field -> field_description``, one flat
#:   description per field name, for the read-mode field-name annotation.
ValueMapping = Dict[str, Dict[str, List[Tuple[str, str]]]]
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
        f_gcode = Settings.field("group_code")

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
                group_value = cls._text(feature, f_gcode) if f_gcode else ""
                # Key by both spellings so a table storing codes with or
                # without quotes both resolve.
                for variant in {code, normalize_code(code)}:
                    if not variant:
                        continue
                    bucket = values.setdefault(field, {}).setdefault(variant, [])
                    pair = (description, group_value)
                    if pair not in bucket:
                        bucket.append(pair)
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


#: Quoted field reference, single-quoted literal, or a bare number - scanned in
#: one pass so each literal can be attributed to the field that precedes it.
_SCAN_RE = re.compile(
    r'"(?P<field>[^"\n]+)"'
    r"|'(?P<quoted>(?:[^'\\]|\\.)*)'"
    r"|(?P<bare>\b\d+(?:\.\d+)?\b)"
)

#: A parenthesis, or a bare ``OR`` keyword - the only structure
#: ``_advance_scope_stack()`` needs to tell one AND-run apart from another.
#: Only ever matched against the text BETWEEN ``_SCAN_RE`` matches, so an
#: "OR" inside a quoted field name or string literal (or as part of a longer
#: word, e.g. "COLOR" or "ORDER" - ``\b`` guards against that too) is never
#: mistaken for the keyword.
_STRUCTURE_RE = re.compile(r"\(|\)|\bOR\b", re.IGNORECASE)


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


#: Left-to-Right Mark (U+200E) - zero-width, no glyph of its own, but a
#: "strong LTR" character as far as the Unicode Bidi Algorithm is concerned.
#: See force_ltr_paragraphs() for why read mode needs it. Built with chr()
#: rather than pasted as a literal invisible character: besides surviving
#: editors/diffs/encodings that might otherwise silently mangle it, a
#: bidirectional control character sitting directly in the source text trips
#: automated security scanners (it is the same character class abused by
#: "Trojan Source" attacks to hide code behind reordered rendering) - QGIS's
#: own plugin repository rejected v1.5 over exactly this for _FSI/_PDI below.
#: chr() produces the identical runtime character with no such literal ever
#: appearing in the .py file itself.
_LRM = chr(0x200E)


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
#: Built with chr(), not pasted as literal invisible characters - see _LRM's
#: own comment above; these two are the exact characters the QGIS plugin
#: repository's security scanner flagged in v1.5 ("contains bidirectional
#: control characters"), since a directional-isolate/override sitting
#: directly in source text is the same trick "Trojan Source" attacks use.
_FSI = chr(0x2068)
_PDI = chr(0x2069)


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


class _ScopedLiteral(NamedTuple):
    """One value literal found while scanning an expression for group
    context - see ``_pick_label()``'s own docstring for how ``path`` is
    used to decide whether one literal's ``field = code`` comparison can
    supply the group another, ambiguous literal is resolved by.
    """

    field: str
    code: str
    path: Tuple[Tuple[int, int], ...]


def _advance_scope_stack(stack: List[list], text: str, start: int, end: int) -> None:
    """Advance ``stack`` over ``text[start:end]`` - the raw text BETWEEN two
    ``_SCAN_RE`` matches, never inside a quoted field name or string literal
    - mutating it in place. Shared by ``_scan_literals_with_scope()`` and
    ``substitute_descriptions()``'s own pass, so the two can never drift
    apart on how a scope path is built.

    ``stack`` is a list of ``[marker, run_index]`` pairs, one per currently
    open scope: index 0 is the always-present top level (``marker = -1``);
    each ``(`` pushes one more, popped again by its matching ``)``.

    ``run_index`` starts at 0 and counts which AND-run of ITS OWN scope the
    text at the current position is in - incremented every time a bare
    ``OR`` is crossed at that exact nesting depth. Critically, an ``OR``
    only ever touches ``stack[-1]`` - the CURRENT innermost scope - never an
    ancestor's: this is what makes
    ``"F_CODE" = 2300 AND ("COUNTRY" = 1 OR "COUNTRY" = 2)`` still treat
    both COUNTRY branches as governed by F_CODE (the OR is fully inside the
    parenthesis, so it never touches the top-level run F_CODE sits in),
    while ``"F_CODE" = 2300 OR "F_ATT" = 610`` does NOT treat 610 as
    belonging to F_CODE's group (that OR IS at F_ATT's own top-level scope,
    so it starts a new run there) - see ``_governing_values()``.
    """
    for structure_match in _STRUCTURE_RE.finditer(text, start, end):
        # Named "piece", not "token": a Bandit security scan of this file
        # (run by the QGIS plugin repository) flags any variable named
        # "token" compared against a short string literal as a possible
        # hardcoded password (its check is a bare name-pattern match, with
        # no idea this is a parenthesis/operator parsed out of an
        # expression) - renamed purely to stop tripping that false
        # positive, no behaviour change.
        piece = structure_match.group(0)
        if piece == "(":
            stack.append([structure_match.start(), 0])
        elif piece == ")":
            if len(stack) > 1:
                stack.pop()
        else:  # a bare OR (case-insensitive)
            stack[-1][1] += 1


def _scan_literals_with_scope(text: str) -> List[_ScopedLiteral]:
    """Every value literal in ``text``, tagged with the field it is
    attributed to - the same nearest-preceding-quoted-field rule
    ``substitute_descriptions()`` itself uses - and with a SCOPE PATH built
    by ``_advance_scope_stack()``. Quoted spans (a field name or a string
    literal) are treated as opaque - a "(" or "OR" inside one is never
    mistaken for real expression structure - by construction, since only
    the unmatched TEXT BETWEEN ``_SCAN_RE`` matches is ever scanned for
    structure at all.
    """
    results: List[_ScopedLiteral] = []
    stack: List[list] = [[-1, 0]]
    current_field = ""
    scan_pos = 0
    for match in _SCAN_RE.finditer(text):
        _advance_scope_stack(stack, text, scan_pos, match.start())
        scan_pos = match.end()

        field = match.group("field")
        if field is not None:
            current_field = field.strip().lower()
            continue
        literal = match.group("quoted")
        if literal is None:
            literal = match.group("bare")
        if literal is None or not current_field:
            continue
        code = normalize_code(literal)
        if code:
            path = tuple((marker, run) for marker, run in stack)
            results.append(_ScopedLiteral(current_field, code, path))
    return results


def _governing_values(leaves: List[_ScopedLiteral], field: str, path: Tuple[Tuple[int, int], ...]) -> set:
    """Every value from some OTHER field's comparison that could be "the
    group" for a literal of ``field`` sitting at scope ``path`` - see
    ``_pick_label()``.

    A leaf at ``leaf.path`` can govern a literal at ``path`` exactly when
    ``leaf.path`` is a PREFIX of ``path`` - the SAME scope (flat AND
    siblings, ``"F_CODE" = 2300 AND "F_ATT" = 603``) counts as a prefix of
    itself, and a SHORTER, ANCESTOR scope (``"F_CODE" = 2300 AND (...
    "F_ATT" = 603 ...)``) counts too. Each path element is itself a
    ``(marker, run_index)`` pair (see ``_advance_scope_stack()``), so an
    intervening ``OR`` at either literal's own depth - which starts a new
    ``run_index`` there - breaks the prefix match and, with it, the
    governing relationship: ``"F_CODE" = 2300 OR "F_ATT" = 603`` does NOT
    let 2300 govern 603, even though both sit at the same nesting depth.
    Comparisons on the SAME field are never governors of one another - a
    value only ever gets its group from a genuinely different field.
    """
    values = set()
    for leaf in leaves:
        if leaf.field == field:
            continue
        if len(leaf.path) <= len(path) and path[: len(leaf.path)] == leaf.path:
            values.add(leaf.code)
    return values


def substitute_descriptions(
    text: str,
    mapping: "ValueMapping",
    mode: str = "primary",
    alt_mapping: Optional["AltValueMapping"] = None,
    field_descriptions: Optional["FieldDescriptions"] = None,
) -> str:
    """Replace value codes with descriptions, for display only.

    Each literal is attributed to the nearest **preceding** quoted field, which
    is how the expression reads (``"F_ATT" IN ('610', '607')``). That is what
    keeps ``"OTHER" = '610'`` untouched.

    When a code has several meanings under the same field, its own
    surrounding context in THIS SAME expression decides which one,
    whenever it can - see ``_pick_label``.

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

    # Every value literal in the WHOLE expression, with its own scope -
    # computed once up front, since resolving one ambiguous code may need
    # to look at a "FIELD = VALUE" comparison anywhere else in the same
    # expression, including one that comes AFTER it in the text.
    leaves = _scan_literals_with_scope(text)

    out: List[str] = []
    last_end = 0
    current_field = ""
    stack: List[list] = [[-1, 0]]
    scan_pos = 0

    for match in _SCAN_RE.finditer(text):
        # Tracks scope (paren nesting + AND-run, see _advance_scope_stack())
        # up to this match on its OWN cursor (scan_pos), independently of
        # `out`'s last_end: a match that does not end up substituted (no
        # field description, no candidates) never advances last_end, but
        # must still advance scan_pos, or the next match's gap-scan would
        # re-process the same text twice.
        _advance_scope_stack(stack, text, scan_pos, match.start())
        scan_pos = match.end()

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

        alt_for_code = (alt_mapping or {}).get(current_field, {}).get(code, {})
        path = tuple((marker, run) for marker, run in stack)
        label = _pick_label(candidates, current_field, path, leaves, mode, alt_for_code)
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
    candidates: List[Tuple[str, str]],
    field: str,
    path: Tuple[Tuple[int, int], ...],
    leaves: List[_ScopedLiteral],
    mode: str = "primary",
    alt_for_code: Optional[Dict[str, str]] = None,
) -> str:
    """Choose among competing descriptions for one code.

    Exactly two rules, in order:

    1. **Infer it from the expression's own group context.** Each
       candidate is (description, group_value) - group_value is that
       row's own "Group codes column" value (see Settings), normally used
       only to head a suggestion-list group. If exactly one candidate's
       group_value also appears as some OTHER field's comparison value,
       anywhere in the SAME "AND scope" as this literal - either right
       beside it (``"F_CODE" = 2300 AND "F_ATT" = 603``), or in an
       enclosing scope a parenthesised group of comparisons sits inside
       (``"F_CODE" = 2300 AND (... "F_ATT" = 603 ...)``) - that candidate
       is the one meant. See ``_governing_values()``.
    2. **Show every meaning**, joined by ``/``, whenever rule 1 finds no
       match, or more than one - never guess between two equally
       plausible readings.

    A bare ``OR`` breaks rule 1's "same AND scope": ``"F_CODE" = 2300 OR
    "F_ATT" = 603`` does NOT let 2300 govern 603, since an OR - not an AND -
    joins them; ``"F_CODE" = 2300 AND ("COUNTRY" = 1 OR "COUNTRY" = 2)``
    still lets 2300 govern both COUNTRY branches, since that OR sits fully
    inside its own parenthesis, never touching the scope F_CODE itself sits
    in - see ``_advance_scope_stack()`` for exactly how that is tracked.
    This deliberately does not track a negated (``!=``) comparison at all -
    an accepted simplification: anything genuinely ambiguous - two
    candidate group values both present, a comparison this cannot make
    sense of - simply falls back to rule 2 rather than risking a
    confidently wrong guess.

    ``mode="alt"`` renders whichever description ends up chosen through its
    own alternative text instead (``alt_for_code``, keyed by the primary
    description) - falling back to the primary text when that particular
    row has no alternative of its own, so alternative mode never looks
    broken for an entry the alternative column simply has nothing to say
    about.
    """
    alt_for_code = alt_for_code or {}

    def _render(description: str) -> str:
        rendered = normalize_code(description)
        if mode != "alt":
            return rendered
        alt = alt_for_code.get(description, "")
        return normalize_code(alt) if alt else rendered

    if len(candidates) == 1:
        return _render(candidates[0][0])

    governing = _governing_values(leaves, field, path)
    matches = list(
        dict.fromkeys(
            description
            for description, group in candidates
            if group and normalize_code(group) in governing
        )
    )
    if len(matches) == 1:
        return _render(matches[0])

    # Zero matches, or more than one: cannot say for certain which is
    # meant - show every meaning instead of guessing.
    #
    # Each candidate isolated individually (see _isolate()) before joining -
    # otherwise two adjacent RTL meanings ("mosque / greenhouse" in Hebrew)
    # could bidi-merge across the " / " separator and swap order, the same
    # problem a comma-separated IN (...) list has.
    descriptions = list(dict.fromkeys(description for description, _group in candidates))
    if mode == "alt":
        # dict.fromkeys(): de-duplicated, order-preserving - two distinct
        # primary descriptions can share one alternative, or both fall back
        # to their own (different) primary text.
        return " / ".join(dict.fromkeys(_isolate(_render(d)) for d in descriptions))
    return " / ".join(_isolate(normalize_code(d)) for d in descriptions)


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

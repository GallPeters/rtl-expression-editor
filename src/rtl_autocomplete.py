# -*- coding: utf-8 -*-
"""
Custom autocomplete source for the RTL / BiDi editor plugin.

This module is **purely additive**.  Nothing here touches the overlay editor's
synchronisation, geometry, appearance or detection logic.  The single
integration point is ``CustomAutocompleteController``, which:

* installs an event filter on the overlay editor to catch Ctrl+Space, and
* inserts the chosen value with ``QTextCursor.insertText()``.

That second point matters: the insertion is an ordinary edit on the overlay's
document, so it flows through the *existing* ``textChanged`` -> Scintilla push
exactly like a keystroke.  The synchronisation mechanism is reused, not
modified, and not bypassed.

Design notes
------------
**No layer scanning.**  Values are fetched with a provider-side
``QgsFeatureRequest`` filter expression, so a lookup reads only the handful of
matching rows.  On a PostGIS or GeoPackage source the filter is pushed down to
the database.  A full-table scan never happens, which is why no background
``QgsTask`` is needed to keep the GUI responsive.

**Per-key memoisation.**  Results are cached under
``(field_name, table_context)``.  Repeated Ctrl+Space on the same field costs
nothing.  The cache is dropped when settings change, when the source layer's
data changes, or when the layer is removed from the project.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QEvent, QObject, Qt
from qgis.PyQt.QtGui import QColor, QFont, QTextCursor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QListWidget,
    QListWidgetItem,
    QToolTip,
)

from qgis.core import Qgis, QgsExpression, QgsFeatureRequest, QgsMessageLog

from .rtl_settings import BUS, Settings

LOG_TAG = "RTL BiDi Editor"

#: Hard ceiling on rows pulled per lookup.  Protects against a misconfigured
#: field selector turning into an unbounded read.
QUERY_LIMIT = 2000

#: Maximum items shown in the popup at once.
MAX_DISPLAYED = 500

#: Characters that make up a value token being typed before the cursor.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-.]+$")

#: A field name in double quotes, e.g. "COUNTRY".
_QUOTED_FIELD_RE = re.compile(r'"([^"\n]+)"')

#: An unterminated quote at the very end, e.g. ... "COUN
_OPEN_QUOTE_RE = re.compile(r'"([^"\n]*)$')

#: A single-quoted string literal, blanked out before quote counting.
_SQ_STRING_RE = re.compile(r"'(?:[^'\\\n]|\\.)*'")

#: Roles used to carry the insertable value on popup items.
VALUE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
#: Description carried alongside the value, for remembering the user's choice.
DESC_ROLE = int(Qt.ItemDataRole.UserRole) + 2
#: Text to insert, which differs from the displayed label for functions.
INSERT_ROLE = int(Qt.ItemDataRole.UserRole) + 3
#: Characters to move the caret left after inserting.
CARET_ROLE = int(Qt.ItemDataRole.UserRole) + 4


#: Diagnostic logging for the Ctrl+Space path.
#:
#: Off by default, and deliberately so: writing to the QGIS message log can
#: cause the Log Messages dock to be raised, which pulls keyboard focus off a
#: modal dialog. In the Expression Builder that leaves the caret invisible until
#: the dialog is reopened. Set to True only while diagnosing, or better, call
#: diagnose() from the Python Console, which prints instead of logging.
DEBUG_AC = False

#: When a table-filtered lookup finds nothing, retry without the table clause.
#:
#: OFF: values are returned only when the current layer's source table name
#: appears in the configured Table Field. A mismatch yields no popup, which is
#: the intended, strict behaviour. Use diagnose() to investigate a mismatch -
#: never the message log, see DEBUG_AC.
TABLE_FILTER_FALLBACK = False


def _log(message: str, level=Qgis.MessageLevel.Info) -> None:
    """Log a genuine problem.  Never called on the normal Ctrl+Space path."""
    try:
        QgsMessageLog.logMessage(message, LOG_TAG, level)
    except Exception:
        pass


def _dbg(message: str) -> None:
    """Diagnostic-only logging; silent unless DEBUG_AC is enabled."""
    if DEBUG_AC:
        _log(message, Qgis.MessageLevel.Info)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


def _unquote_for_display(text: str) -> str:
    """Strip one layer of matching outer quotes, for DISPLAY only.

    A lookup table may store codes with their quotes included, e.g. the literal
    seven characters ``'farm'``, so that double-clicking inserts a ready-made SQL
    string. Showing that raw in the popup gives ``'farm' (300)``, which is noisy.

    This affects only what the popup renders and what typing filters against.
    The value inserted into the expression comes from the entry's raw ``value``,
    so a code stored with quotes still produces valid SQL.

    Uses the same single-matching-pair rule as rtl_readmode.normalize_code, so
    the popup and read mode agree on what "unquoted" means.
    """
    value = (text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


class AutocompleteEntry:
    """One candidate value, plus the optional labels used to present it."""

    __slots__ = (
        "value",
        "description",
        "group_code",
        "group_description",
        "insert_text",
        "display_text",
        "help_text",
        "caret_offset",
    )

    def __init__(
        self,
        value: str,
        description: str = "",
        group_code: str = "",
        group_description: str = "",
        insert_text: str = "",
        display_text: str = "",
        help_text: str = "",
        caret_offset: int = 0,
    ):
        self.value = value
        self.description = description
        self.group_code = group_code
        self.group_description = group_description
        #: What actually goes into the document. Defaults to ``value``. A
        #: function displays ``buffer(geometry, distance)`` but inserts
        #: ``buffer()``, so the two must be separable.
        self.insert_text = insert_text or value
        #: Overrides the computed label when set.
        self.display_text = display_text
        #: Tooltip - QGIS help text is HTML, which Qt tooltips render natively.
        self.help_text = help_text
        #: Characters to move the caret LEFT after inserting, so ``buffer()``
        #: leaves the caret between the parentheses.
        self.caret_offset = caret_offset

    @property
    def display(self) -> str:
        """Label shown in the popup: ``IL (Israel)`` or just ``IL``.

        Quotes are stripped for presentation only - see _unquote_for_display.
        """
        if self.display_text:
            return self.display_text
        value = _unquote_for_display(self.value)
        description = _unquote_for_display(self.description)
        if description:
            return f"{value} ({description})"
        return value

    @property
    def filter_text(self) -> str:
        """Text typing is matched against: unquoted value plus description."""
        return f"{_unquote_for_display(self.value)} {_unquote_for_display(self.description)}"

    @property
    def group_label(self) -> str:
        """Header label: ``Country Codes (International)``."""
        code = _unquote_for_display(self.group_code)
        description = _unquote_for_display(self.group_description)
        if code and description:
            return f"{code} ({description})"
        return code or description or ""


# --------------------------------------------------------------------------- #
# Field-name detection
# --------------------------------------------------------------------------- #


#: Group headings used by the mixed suggestion list.
GROUP_FIELDS = "Fields"
GROUP_FUNCTIONS = "Functions"
GROUP_VARIABLES = "Variables"
GROUP_OPERATORS = "Operators"
GROUP_VALUES = "Values"

#: Static operator/keyword list. Not obtainable from any API.
_OPERATORS = (
    "AND", "OR", "NOT", "IN", "LIKE", "ILIKE", "IS NULL", "IS NOT NULL",
    "BETWEEN", "CASE WHEN", "THEN", "ELSE", "END",
)

#: Cache for the function index - QgsExpression.Functions() is stable for a
#: session, and building signatures for several hundred functions on every
#: keypress would be wasteful.
_FUNCTION_CACHE: Optional[List[tuple]] = None


def builtin_functions() -> List[tuple]:
    """Return ``(name, signature, help_html)`` for every registered function.

    Scintilla's own completion cannot be reused from an overlay (it needs to own
    the input loop to filter and accept entries), so the list is rebuilt from
    the public API instead. That also lets us show signatures and help text,
    which Scintilla's plain list does not.
    """
    global _FUNCTION_CACHE
    if _FUNCTION_CACHE is not None:
        return _FUNCTION_CACHE

    functions: List[tuple] = []
    try:
        for function in QgsExpression.Functions():
            try:
                name = function.name()
                if not name or name.startswith("_"):
                    continue

                params: List[str] = []
                try:
                    for parameter in function.parameters():
                        param_name = parameter.name()
                        if param_name:
                            params.append(param_name)
                except Exception:
                    # Not every function exposes parameters; variadic ones in
                    # particular may report none. Fall back to an empty
                    # signature rather than dropping the function.
                    params = []

                signature = f"{name}({', '.join(params)})" if params else f"{name}()"
                try:
                    help_html = function.helpText() or ""
                except Exception:
                    help_html = ""
                functions.append((name, signature, help_html, len(params)))
            except Exception:
                continue
    except Exception as exc:
        _dbg(f"Could not enumerate functions: {exc}")

    functions.sort(key=lambda item: item[0].lower())
    _FUNCTION_CACHE = functions
    return functions


def builtin_variables(layer=None) -> List[str]:
    """Variable names from the global, project and layer scopes.

    Covers @map_scale, @atlas_feature, project variables and any custom ones,
    correctly scoped, without hard-coding a list that would go stale.
    """
    names: List[str] = []
    seen = set()

    def add_scope(scope) -> None:
        if scope is None:
            return
        try:
            for name in scope.variableNames():
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        except Exception:
            pass

    try:
        from qgis.core import QgsExpressionContextUtils, QgsProject

        add_scope(QgsExpressionContextUtils.globalScope())
        add_scope(QgsExpressionContextUtils.projectScope(QgsProject.instance()))
        if layer is not None:
            add_scope(QgsExpressionContextUtils.layerScope(layer))
    except Exception as exc:
        _dbg(f"Could not enumerate variables: {exc}")

    names.sort(key=str.lower)
    return names


def context_field_names(layer) -> List[str]:
    """Field names of the layer being edited."""
    if layer is None:
        return []
    try:
        return [field.name() for field in layer.fields()]
    except Exception:
        return []


#: Caret sitting immediately after @ or partway through a variable name.
_VARIABLE_RE = re.compile(r"@(\w*)$")


def suggestion_context(text_before_cursor: str) -> str:
    """Decide WHAT to suggest from the text before the caret.

    The syntax is usually unambiguous. Where it is not, the answer is "mixed"
    rather than a guess: a wrong guess hides the thing the user wanted, whereas
    a grouped list costs them only a keystroke of filtering.

    Returns one of: "fields", "variables", "values", "mixed".
    """
    if not text_before_cursor:
        return "mixed"

    scrubbed = _SQ_STRING_RE.sub(
        lambda m: " " * (m.end() - m.start()), text_before_cursor
    )

    # Inside an unterminated double quote -> naming a field.
    if scrubbed.count('"') % 2 == 1:
        return "fields"

    # Immediately after @ -> a variable.
    if _VARIABLE_RE.search(text_before_cursor):
        return "variables"

    # Inside an unterminated single quote -> typing a value.
    without_fields = re.sub(r'"[^"\n]*"', lambda m: " " * len(m.group(0)), text_before_cursor)
    if without_fields.count("'") % 2 == 1:
        return "values"

    # After a comparison against a known field -> that field's values.
    if detect_field_name(text_before_cursor):
        tail = text_before_cursor[text_before_cursor.rfind('"') + 1:]
        if re.search(r"(=|!=|<>|<|>|\bIN\b|\bLIKE\b|\bILIKE\b)\s*\(?\s*[\w'%]*$",
                     tail, re.IGNORECASE):
            return "values"

    return "mixed"


def detect_field_name(text_before_cursor: str) -> str:
    """Extract the field name the cursor is currently working on.

    Scans backwards from the cursor for the nearest double-quoted token, which
    is how both the expression language and the SQL filter spell a field
    reference.

    Two cases are distinguished by counting double quotes, because a naive
    "search backwards for a quote" also matches the text *after* a closing
    quote:

    * **even** number of quotes - every quoted token is closed, so the caret is
      somewhere after a complete field reference. The nearest one wins.
    * **odd** number - the caret sits inside an unterminated token, i.e. the
      user is still typing the field name itself.

    Single-quoted string literals are blanked out before counting, so a stray
    double quote inside a literal (``'say "hi"'``) cannot corrupt the parity.
    Blanking preserves length, keeping all offsets valid.

    >>> detect_field_name('"NAME" = \\'x\\' AND "COUNTRY" = ')
    'COUNTRY'
    >>> detect_field_name('"COUN')
    'COUN'
    """
    if not text_before_cursor:
        return ""

    scrubbed = _SQ_STRING_RE.sub(lambda m: " " * (m.end() - m.start()), text_before_cursor)

    if scrubbed.count('"') % 2 == 1:
        open_quote = _OPEN_QUOTE_RE.search(scrubbed)
        return open_quote.group(1).strip() if open_quote else ""

    matches = list(_QUOTED_FIELD_RE.finditer(scrubbed))
    return matches[-1].group(1).strip() if matches else ""


def _strip_quotes(name: str) -> str:
    """Remove the quoting a provider URI may wrap a table name in."""
    name = (name or "").strip()
    for quote in ('"', "'", "`", "[", "]"):
        name = name.strip(quote)
    return name.strip()


def source_table_name(layer) -> str:
    """The layer's *source table* name - not its display name.

    Uses ``QgsProviderRegistry.decodeUri()``, which is provider-aware and is the
    only reliable way to do this across backends.  ``QgsDataSourceUri`` alone is
    not enough: it understands the ``key=value`` form used by Postgres/MSSQL but
    returns an empty table for OGR sources, which is why an earlier version of
    this function silently fell back to the layer name.

    Examples::

        .../world_map.gpkg|layername=countries|subset="NAME" 20   -> countries
        dbname='test' ... table="public"."World Map" (geom)       -> World Map
        /data/roads.shp                                          -> roads
    """
    if layer is None:
        return ""

    source = ""
    try:
        source = layer.source() or ""
    except Exception:
        pass

    # 1. Provider-aware decode. Returns 'layerName' for OGR/GPKG and 'table'
    #    (plus a separate 'schema') for database providers.
    try:
        from qgis.core import QgsProviderRegistry

        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), source)
        if isinstance(parts, dict):
            for key in ("layerName", "table", "tableName"):
                value = parts.get(key)
                if value not in (None, ""):
                    name = _strip_quotes(str(value))
                    if name:
                        return name
    except Exception:
        pass

    # 2. key=value URI form (Postgres, MSSQL, Oracle...).
    try:
        from qgis.core import QgsDataSourceUri

        table = QgsDataSourceUri(source).table()
        if table:
            return _strip_quotes(table)
    except Exception:
        pass

    # 3. Textual fallbacks, in case a provider is not registered.
    match = re.search(r"layername=([^|]+)", source, re.IGNORECASE)
    if match:
        return _strip_quotes(match.group(1))
    match = re.search(r'table=(?:"[^"]*"\.)?("[^"]+"|[^\s(]+)', source, re.IGNORECASE)
    if match:
        return _strip_quotes(match.group(1))

    # 4. File-based source with no layer name: the file stem is the table.
    try:
        import os

        path = source.split("|")[0].strip()
        if path and (os.sep in path or "/" in path):
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem:
                return stem
    except Exception:
        pass

    return ""


def _find_context_layer(widget):
    """The layer the dialog is operating on.

    Two strategies, in order of reliability:

    1. Walk up from the Scintilla widget for an ancestor exposing ``layer()``.
       This is the same technique the editor already uses to harvest field
       names, so no new assumption is introduced. It covers the Expression
       Builder and the Field Calculator.
    2. Fall back to ``iface.activeLayer()``. The Query Builder does not expose
       its layer to Python, but it is always opened from the layer that is
       active in the layer tree, so this is accurate in practice.
    """
    try:
        node = widget
        depth = 0
        while node is not None and depth < 10:
            getter = getattr(node, "layer", None)
            if callable(getter):
                try:
                    candidate = getter()
                    if candidate is not None and hasattr(candidate, "source"):
                        return candidate
                except Exception:
                    pass
            node = node.parentWidget() if hasattr(node, "parentWidget") else None
            depth += 1
    except Exception:
        pass

    # 2. Fall back to the active layer, but never to the autocomplete lookup
    #    layer itself: if the user has the lookup table selected in the layer
    #    tree, activeLayer() would return it, and the table filter would then
    #    compare the lookup table against its own name and match nothing.
    try:
        from qgis.utils import iface

        if iface is not None:
            active = iface.activeLayer()
            if active is not None and hasattr(active, "source"):
                lookup = Settings.autocomplete_layer()
                if lookup is not None and active.id() == lookup.id():
                    _dbg(
                        "Autocomplete: the active layer IS the lookup layer; "
                        "no table context could be determined."
                    )
                    return None
                return active
    except Exception:
        pass
    return None


def resolve_table_candidates(widget) -> List[str]:
    """Values to match against the configured Table Field.

    The **source table name is authoritative** - that is what the user
    configures against:

        .../world_map.gpkg|layername=countries|subset="NAME" 20  -> countries
        dbname='test' ... table="public"."World Map" (geom)      -> World Map

    Only two spellings are returned: the bare table name, and - for database
    providers - the schema-qualified form. The qualified form is strictly
    narrower, so OR-ing it can never let through a row that the bare name would
    not already match.

    The layer's display name and the container file's stem are used *only* when
    the source table name cannot be determined at all. Earlier this function
    OR-ed all four spellings together on the theory that the user should not
    have to guess the convention; in practice that widened the filter and let
    rows from other tables through, e.g. matching ``world_map`` (the file stem)
    when the table is ``countries``.

    An empty list means "the table context is unknown", which the caller now
    treats as "return nothing" rather than "match every table".
    """
    layer = _find_context_layer(widget)
    if layer is None:
        _dbg("Autocomplete: could not determine the current layer.")
        return []

    candidates: List[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and not any(value.lower() == existing.lower() for existing in candidates):
            candidates.append(value)

    table = source_table_name(layer)
    add(table)

    # Schema-qualified variant, e.g. public.World Map.
    if table:
        try:
            from qgis.core import QgsProviderRegistry

            parts = QgsProviderRegistry.instance().decodeUri(
                layer.providerType(), layer.source()
            )
            if isinstance(parts, dict):
                schema = str(parts.get("schema") or "").strip()
                if schema:
                    add(f"{schema}.{table}")
        except Exception:
            pass
    else:
        # No source table could be determined - fall back to looser spellings
        # rather than disabling the feature entirely.
        _dbg("Autocomplete: no source table name; falling back to layer name.")
        try:
            add(layer.name())
        except Exception:
            pass
        try:
            import os

            path = (layer.source() or "").split("|")[0].strip()
            if path and (os.sep in path or "/" in path):
                add(os.path.splitext(os.path.basename(path))[0])
        except Exception:
            pass

    _dbg(f"Autocomplete table context: {candidates}")
    return candidates


# --------------------------------------------------------------------------- #
# Cache / lookup
# --------------------------------------------------------------------------- #


class AutocompleteCache(QObject):
    """Memoised, provider-filtered lookups against the configured layer.

    One instance is shared by every overlay editor (see ``cache()`` below), so
    opening several dialogs does not multiply the queries.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._memo: Dict[Tuple[str, str], List[AutocompleteEntry]] = {}
        self._watched_layer_id: str = ""
        self._watched_layer = None
        BUS.changed.connect(self.invalidate)

    # -- invalidation ------------------------------------------------------ #

    def invalidate(self) -> None:
        """Drop everything.  Called on settings change and on layer edits."""
        self._memo.clear()
        self._sync_layer_hooks()

    def _sync_layer_hooks(self) -> None:
        """Keep edit-signal connections pointed at the configured layer."""
        layer = Settings.autocomplete_layer()
        layer_id = layer.id() if layer is not None else ""
        if layer_id == self._watched_layer_id:
            return

        # Disconnect the previous layer.
        if self._watched_layer is not None:
            for signal_name in (
                "dataChanged",
                "featureAdded",
                "featuresDeleted",
                "attributeValueChanged",
                "willBeDeleted",
            ):
                try:
                    getattr(self._watched_layer, signal_name).disconnect(self._on_layer_touched)
                except Exception:
                    pass

        self._watched_layer = layer
        self._watched_layer_id = layer_id

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
                getattr(layer, signal_name).connect(self._on_layer_touched)
            except Exception:
                pass  # not every provider exposes every signal

    def _on_layer_touched(self, *_args) -> None:
        self._memo.clear()

    # -- lookup ------------------------------------------------------------ #

    def lookup(self, field_name: str, table_candidates: Sequence[str]) -> List[AutocompleteEntry]:
        """Return sorted entries for ``field_name`` in the given table context."""
        if not field_name:
            return []
        usable, reason = Settings.autocomplete_is_usable()
        if not usable:
            _dbg(f"Custom autocomplete unavailable: {reason}")
            return []

        self._sync_layer_hooks()

        key = (field_name.strip().lower(), "|".join(sorted(t.lower() for t in table_candidates)))
        if key in self._memo:
            return self._memo[key]

        entries = self._query(field_name, table_candidates)
        entries.sort(key=lambda e: (e.group_label.lower(), e.value.lower()))
        self._memo[key] = entries
        return entries

    def lookup_field_names(self, table_candidates: Sequence[str]) -> List[str]:
        """Distinct values of the Fields Names column for this table context.

        Powers field-name completion: the user types a quote and gets the list
        of fields that actually have definitions, instead of having to remember
        them. Memoised under a reserved key alongside the value lookups.
        """
        usable, _ = Settings.autocomplete_is_usable()
        if not usable:
            return []
        self._sync_layer_hooks()

        key = ("\x00field-names", "|".join(sorted(t.lower() for t in table_candidates)))
        cached = self._memo.get(key)
        if cached is not None:
            return [entry.value for entry in cached]

        layer = Settings.autocomplete_layer()
        f_names = Settings.field("field_names")
        f_table = Settings.field("table")
        names: List[str] = []
        seen = set()
        try:
            request = QgsFeatureRequest()
            if f_table and table_candidates:
                column = QgsExpression.quotedColumnRef(f_table)
                ors = " OR ".join(
                    f"lower(trim({column})) = {QgsExpression.quotedString(t.lower())}"
                    for t in table_candidates
                )
                request.setFilterExpression(ors)
            request.setLimit(QUERY_LIMIT)
            try:
                request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
            except Exception:
                pass
            for feature in layer.getFeatures(request):
                value = self._as_text(feature, f_names)
                if value and value.lower() not in seen:
                    seen.add(value.lower())
                    names.append(value)
        except Exception as exc:
            _dbg(f"Field-name lookup failed: {exc}")
            return []

        names.sort(key=str.lower)
        self._memo[key] = [AutocompleteEntry(value=n) for n in names]
        return names

    def _query(self, field_name: str, table_candidates: Sequence[str]) -> List[AutocompleteEntry]:
        """Run up to four increasingly permissive passes until one finds rows.

        Order matters: the narrowest, most correct query runs first, and each
        fallback is only reached when the previous one found nothing.

        1. exact field match + table filter   <- the intended query
        2. substring field match + table filter
        3. exact field match, no table filter
        4. substring field match, no table filter

        Passes 3 and 4 exist because a Table Field value that does not match the
        current layer used to make the popup silently fail to appear, which is
        far worse than showing slightly broader results.  When a fallback is
        used it is reported at Warning level so the misconfiguration is visible.
        Set TABLE_FILTER_FALLBACK = False to keep the filter strict.
        """
        layer = Settings.autocomplete_layer()
        if layer is None:
            return []

        f_names = Settings.field("field_names")
        f_value = Settings.field("value")
        f_table = Settings.field("table")
        f_desc = Settings.field("description")
        f_gcode = Settings.field("group_code")
        f_gdesc = Settings.field("group_description")

        try:
            available = {f.name() for f in layer.fields()}
        except Exception:
            return []

        # Silently ignore optional fields that were deleted from the layer.
        f_desc = f_desc if f_desc in available else ""
        f_gcode = f_gcode if f_gcode in available else ""
        f_gdesc = f_gdesc if f_gdesc in available else ""
        f_table = f_table if f_table in available else ""

        wanted = [n for n in (f_names, f_value, f_table, f_desc, f_gcode, f_gdesc) if n]

        # A configured Table Field with no resolvable table context used to fall
        # through to the unfiltered passes below, which returned values for
        # *every* table - indistinguishable from the filter not working. Treat an
        # unknown context as "no match" instead, so the behaviour is strict and
        # the misconfiguration is visible rather than silently wrong.
        if f_table and not table_candidates:
            _dbg(
                f"Autocomplete: Table Field '{f_table}' is configured but the "
                f"current layer's source table could not be determined; "
                f"returning no values. Run diagnose() to investigate."
            )
            return []

        filtering_by_table = bool(f_table and table_candidates)
        passes: List[Tuple[bool, bool]] = []
        if filtering_by_table:
            passes += [(True, True), (False, True)]
            if TABLE_FILTER_FALLBACK:
                passes += [(True, False), (False, False)]
        else:
            passes += [(True, False), (False, False)]

        for exact, use_table in passes:
            expression = self._expression(
                field_name,
                f_names,
                f_table if use_table else "",
                table_candidates,
                exact,
            )
            _dbg(f"Autocomplete query: {expression}")
            entries = self._run(
                layer, wanted, expression, f_value, f_desc, f_gcode, f_gdesc
            )
            if entries:
                if filtering_by_table and not use_table:
                    # Diagnostic only. This must NOT reach the message log by
                    # default: writing to it can raise the Log Messages dock,
                    # which pulls keyboard focus off a modal dialog and leaves
                    # the caret invisible until the dialog is reopened.
                    _dbg(
                        f"Autocomplete: no rows matched the table filter "
                        f"({f_table} in {list(table_candidates)}); fell back to "
                        f"all tables for field '{field_name}'."
                    )
                return entries

        _dbg(f"Autocomplete: no rows at all for field '{field_name}'.")
        return []

    @staticmethod
    def _expression(
        field_name: str,
        f_names: str,
        f_table: str,
        table_candidates: Sequence[str],
        exact: bool,
    ) -> str:
        """Build a provider-side filter expression.

        Uses QgsExpression quoting helpers throughout, so a field name or value
        containing quotes cannot break the expression.
        """
        # lower(trim(...)) on the column side: lookup tables are hand-maintained
        # and trailing spaces are a common, invisible cause of "no match".  It
        # can stop the provider from using an index, which is an acceptable
        # trade for a small configuration table capped by QUERY_LIMIT.
        col = f"lower(trim({QgsExpression.quotedColumnRef(f_names)}))"
        needle = field_name.strip().lower()
        if exact:
            clauses = [f"{col} = {QgsExpression.quotedString(needle)}"]
        else:
            clauses = [f"{col} LIKE {QgsExpression.quotedString('%' + needle + '%')}"]

        if f_table and table_candidates:
            tcol = f"lower(trim({QgsExpression.quotedColumnRef(f_table)}))"
            ors = " OR ".join(
                f"{tcol} = {QgsExpression.quotedString(t.strip().lower())}"
                for t in table_candidates
            )
            clauses.append(f"({ors})")

        return " AND ".join(clauses)

    def _run(
        self,
        layer,
        wanted: List[str],
        expression: str,
        f_value: str,
        f_desc: str,
        f_gcode: str,
        f_gdesc: str,
    ) -> List[AutocompleteEntry]:
        entries: List[AutocompleteEntry] = []
        try:
            request = QgsFeatureRequest().setFilterExpression(expression)
            request.setLimit(QUERY_LIMIT)
            try:
                request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
            except Exception:
                try:
                    request.setFlags(QgsFeatureRequest.NoGeometry)
                except Exception:
                    pass
            # Deliberately NOT calling setSubsetOfAttributes(): when a filter
            # expression is evaluated client-side (which happens whenever the
            # provider cannot compile it to SQL), any column missing from the
            # subset evaluates as NULL and the row silently fails to match.
            # A lookup table is small, so fetching all attributes is cheaper
            # than that failure mode. `wanted` is kept for diagnostics.
            _ = wanted

            # Deduplicate on the whole entry, not on the value alone.
            #
            # A value is legitimately allowed to repeat under different groups or
            # with a different description - e.g. code 610 meaning 'mosque' in
            # group 2300 and 'greenhouse' in group 2301. Keying only on the value
            # discarded the second row, and where that row was a group's only
            # member the entire group disappeared from the popup.
            #
            # Using the full tuple still collapses genuinely identical rows,
            # which is all the dedup was ever meant to do.
            seen = set()
            for feature in layer.getFeatures(request):
                value = self._as_text(feature, f_value)
                if not value:
                    continue
                description = self._as_text(feature, f_desc)
                group_code = self._as_text(feature, f_gcode)
                group_description = self._as_text(feature, f_gdesc)
                key = (value, description, group_code, group_description)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    AutocompleteEntry(
                        value=value,
                        description=description,
                        group_code=group_code,
                        group_description=group_description,
                    )
                )
                if len(entries) >= QUERY_LIMIT:
                    break
        except Exception as exc:
            # Also diagnostic-only, for the focus reason described above.
            _dbg(f"Autocomplete query failed: {exc}")
            return []
        return entries

    @staticmethod
    def _as_text(feature, field_name: str) -> str:
        """Read a field as a trimmed string, tolerating NULLs and numerics."""
        if not field_name:
            return ""
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


_CACHE: Optional[AutocompleteCache] = None


def cache() -> AutocompleteCache:
    """Shared cache instance, created on first use."""
    global _CACHE
    if _CACHE is None:
        _CACHE = AutocompleteCache()
    return _CACHE


# --------------------------------------------------------------------------- #
# Popup
# --------------------------------------------------------------------------- #


class AutocompletePopup(QListWidget):
    """Grouped, non-focus-stealing completion list.

    Configured the same way ``QCompleter`` configures its own popup - a
    parentless ``Qt.Popup`` window with ``NoFocus`` and a focus proxy back to
    the editor - so the caret keeps blinking in the editor and typing continues
    to reach it while the list is open.

    Group headers are inserted as non-selectable items; navigation skips them.
    """

    def __init__(self, editor):
        super().__init__(None)
        self._editor = editor
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFocusProxy(editor)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setUniformItemSizes(False)
        self.setAlternatingRowColors(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Right-to-left values must still read correctly inside the list.
        self.setLayoutDirection(editor.layoutDirection())

    # -- population -------------------------------------------------------- #

    def populate(self, entries: Sequence[AutocompleteEntry]) -> int:
        """Fill the list, inserting group headers when grouping is configured.

        Returns the number of selectable value rows.
        """
        self.clear()
        grouped = any(e.group_label for e in entries)
        selectable = 0
        current_group = None

        for entry in entries[:MAX_DISPLAYED]:
            if grouped and entry.group_label != current_group:
                current_group = entry.group_label
                self.addItem(self._make_header(current_group or "Ungrouped"))
            item = QListWidgetItem(("    " if grouped else "") + entry.display)
            item.setData(VALUE_ROLE, entry.value)
            item.setData(DESC_ROLE, entry.description)
            item.setData(INSERT_ROLE, entry.insert_text)
            item.setData(CARET_ROLE, int(entry.caret_offset))
            tooltip = entry.help_text or entry.description
            if tooltip:
                item.setToolTip(tooltip)
            self.addItem(item)
            selectable += 1

        self._select_first_value()
        return selectable

    def show_notice(self, text: str) -> None:
        """Display a single, non-selectable explanatory line.

        Silence was the worst property of the old behaviour: a field with no
        definitions, or an unresolvable table context, produced no popup and no
        feedback, indistinguishable from the key not arriving at all. A visible
        notice makes the feature self-diagnosing without writing to the message
        log (which can raise the Log dock and steal focus from a modal dialog).
        """
        self.clear()
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(QColor("#b7791f"))
        self.addItem(item)
        self.show_at_cursor()

    @staticmethod
    def _make_header(text: str) -> QListWidgetItem:
        item = QListWidgetItem(text)
        font = QFont(item.font())
        font.setBold(True)
        item.setFont(font)
        item.setFlags(Qt.ItemFlag.NoItemFlags)  # not selectable, not enabled
        item.setForeground(QColor("#4271ae"))
        return item

    # -- navigation -------------------------------------------------------- #

    def _is_value_row(self, row: int) -> bool:
        item = self.item(row)
        return item is not None and bool(item.data(VALUE_ROLE))

    def _select_first_value(self) -> None:
        for row in range(self.count()):
            if self._is_value_row(row):
                self.setCurrentRow(row)
                return

    def move_selection(self, step: int) -> None:
        """Move by ``step`` rows, skipping group headers and wrapping around."""
        count = self.count()
        if count == 0:
            return
        row = self.currentRow()
        for _ in range(count):
            row = (row + step) % count
            if self._is_value_row(row):
                self.setCurrentRow(row)
                return

    def current_value(self) -> str:
        item = self.currentItem()
        if item is None:
            return ""
        return str(item.data(VALUE_ROLE) or "")

    # -- placement --------------------------------------------------------- #

    def show_at_cursor(self) -> None:
        """Position below the caret, kept inside the available screen area."""
        editor = self._editor
        rect = editor.cursorRect()
        point = editor.mapToGlobal(rect.bottomLeft())

        rows = min(self.count(), 12)
        height = max(60, sum(self.sizeHintForRow(r) for r in range(rows)) + 8)
        width = max(220, min(520, self.sizeHintForColumn(0) + 40))

        try:
            screen = editor.screen().availableGeometry()
            if point.y() + height > screen.bottom():
                point.setY(editor.mapToGlobal(rect.topLeft()).y() - height)
            if point.x() + width > screen.right():
                point.setX(max(screen.left(), screen.right() - width))
        except Exception:
            pass

        self.setGeometry(point.x(), point.y(), width, height)
        self.show()
        self.raise_()


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #


class CustomAutocompleteController(QObject):
    """Wires Ctrl+Space on one overlay editor to the custom source.

    Integration is intentionally minimal and reversible:

    * an event filter on the editor - so the editor's own ``keyPressEvent`` is
      untouched and its built-in function/field completer keeps working;
    * ``QTextCursor.insertText()`` for insertion - so the existing overlay ->
      Scintilla synchronisation carries the change, unchanged.

    If the feature is disabled in settings, the filter returns immediately and
    the editor behaves exactly as before.
    """

    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor
        self._popup: Optional[AutocompletePopup] = None
        self._entries: List[AutocompleteEntry] = []
        self._field_name = ""
        #: True while the popup lists field names rather than values.
        self._field_mode = False
        #: True while a key event is being re-sent to the editor, to stop the
        #: editor branch of eventFilter from re-entering the popup handler.
        self._forwarding = False
        try:
            editor.installEventFilter(self)
        except Exception as exc:
            _log(f"Custom autocomplete not attached: {exc}", Qgis.MessageLevel.Warning)

    # -- teardown ---------------------------------------------------------- #

    def teardown(self) -> None:
        """Called from the editor's detach(); safe to call more than once."""
        self.hide_popup()
        try:
            if self._editor is not None:
                self._editor.removeEventFilter(self)
        except Exception:
            pass
        if self._popup is not None:
            try:
                self._popup.removeEventFilter(self)
            except Exception:
                pass
            try:
                self._popup.deleteLater()
            except Exception:
                pass
            self._popup = None
        self._editor = None

    # -- event handling ---------------------------------------------------- #

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        """Filter both the editor and the popup.

        The popup is a ``Qt.Popup`` window, and Qt gives such a window a
        keyboard *and* mouse grab.  Filtering only the editor therefore never
        sees Escape or the arrow keys while the list is open, and never sees a
        click landing outside it.  Both targets are filtered here, which is what
        ``QCompleter`` does internally for the same reason.
        """
        try:
            event_type = event.type()
            popup = self._popup

            # --- popup is open: it owns the keyboard and mouse grabs ------- #
            if popup is not None and obj is popup:
                if event_type == QEvent.Type.KeyPress:
                    return self._on_popup_key(event)
                if event_type in (
                    QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonDblClick,
                ):
                    if not self._event_inside_popup(event):
                        # A click outside dismisses, exactly like a menu.
                        self.hide_popup()
                        return True
                    return False
                if event_type in (QEvent.Type.WindowDeactivate, QEvent.Type.FocusOut):
                    self.hide_popup()
                    return False
                return False

            # --- editor ---------------------------------------------------- #
            if obj is self._editor:
                # Claim the trigger before Qt's shortcut machinery does.
                #
                # If any QAction anywhere in the window chain is bound to our
                # trigger combination, Qt sends ShortcutOverride first; if nobody
                # accepts it, the QAction fires and NO KeyPress is ever delivered
                # to the widget. Accepting the override tells Qt "this widget
                # will handle the key itself", after which the normal KeyPress
                # arrives and the branch below runs. Without this, Ctrl+Space can
                # silently do nothing with no way to tell why.
                if event_type == QEvent.Type.ShortcutOverride and (
                    self._is_trigger(event) or self._is_diagnostic(event)
                ):
                    event.accept()
                    return True
                if event_type == QEvent.Type.KeyPress and not self._forwarding:
                    if popup is not None and popup.isVisible():
                        # Keys normally reach the popup; if one arrives here
                        # anyway, handle it identically.
                        return self._on_popup_key(event)
                    if self._is_diagnostic(event):
                        self._show_report()
                        return True
                    if self._is_trigger(event):
                        self.trigger()
                        return True
                    # Typing '(' raises the signature hint. Deferred so the
                    # character is in the document before we look for it.
                    if event.text() == "(":
                        self._call_tip_soon()
                elif event_type == QEvent.Type.FocusOut:
                    # Showing a Qt.Popup window takes the keyboard grab, which
                    # makes Qt deliver FocusOut to the editor with reason
                    # PopupFocusReason. Treating that as "user looked away" would
                    # hide the list in the same instant it appears - the popup
                    # would flash, or never seem to open at all. Only a genuine
                    # focus change ends the session.
                    try:
                        reason = event.reason()
                        transient = reason in (
                            Qt.FocusReason.PopupFocusReason,
                            Qt.FocusReason.ActiveWindowFocusReason,
                        )
                    except Exception:
                        transient = False
                    if not transient:
                        self.hide_popup()
                elif event_type in (
                    QEvent.Type.Wheel,
                    QEvent.Type.Hide,
                ):
                    # Scrolling or the dialog closing leaves a floating list stale.
                    self.hide_popup()
        except Exception as exc:
            _log(f"Custom autocomplete error: {exc}", Qgis.MessageLevel.Warning)
        return False

    def _event_inside_popup(self, event) -> bool:
        """True when a mouse event landed within the popup's own rectangle."""
        if self._popup is None:
            return False
        try:
            try:
                point = event.globalPosition().toPoint()  # Qt6
            except AttributeError:
                point = event.globalPos()  # Qt5 fallback
            return self._popup.geometry().contains(point)
        except Exception:
            return True  # when in doubt, do not dismiss

    @staticmethod
    def _is_trigger(event) -> bool:
        """True for any of the accepted trigger combinations.

        Three combinations are accepted because Ctrl+Space is not dependable on
        every system:

        * **Ctrl+Space** - the documented default.
        * **Ctrl+Shift+Space** - distinguishes an OS-level grab from a plugin
          problem. On Windows, Ctrl+Space toggles input method modes for several
          keyboard layouts (Hebrew among them), in which case the key never
          reaches Qt at all and no amount of event filtering can see it.
        * **Ctrl+J** - a plain fallback that no input method claims. If this
          works and Ctrl+Space does not, the OS or the IME is the culprit.
        """
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return False
        return event.key() in (
            Qt.Key.Key_Space,
            Qt.Key.Key_nobreakspace,
            Qt.Key.Key_J,
        )

    @staticmethod
    def _is_diagnostic(event) -> bool:
        """Ctrl+Shift+D - show the diagnostic report in a message box.

        Needed because the Layer Filter and Field Calculator are **modal**
        dialogs: while one is open the Python Console cannot be used at all, so
        diagnose() and force_complete() are unreachable exactly when the problem
        is happening. A QMessageBox opens on top of a modal dialog, so this
        works where console-based diagnostics cannot.
        """
        modifiers = event.modifiers()
        return (
            bool(modifiers & Qt.KeyboardModifier.ControlModifier)
            and bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            and event.key() == Qt.Key.Key_D
        )

    def _report_text(self) -> str:
        """Build the same information diagnose() prints, as a single string."""
        lines = []
        editor = self._editor
        lines.append(f"module: {__file__}")
        lines.append(f"controller attached: yes ({type(self).__name__})")

        usable, reason = Settings.autocomplete_is_usable()
        lines.append(f"configuration usable: {usable}" + ("" if usable else f" ({reason})"))

        layer = Settings.autocomplete_layer()
        lines.append(f"lookup layer: {layer.name() if layer is not None else None}")
        if layer is not None:
            try:
                lines.append(f"lookup rows: {layer.featureCount()}")
            except Exception:
                pass

        if editor is None:
            lines.append("editor: GONE")
            return "\n".join(lines)

        text_before = editor.toPlainText()[: editor.textCursor().position()]
        field_name = detect_field_name(text_before)
        lines.append(f"text before caret: {text_before[-60:]!r}")
        lines.append(f"detected field: {field_name!r}")

        sci = getattr(editor, "_sci", None)
        context = _find_context_layer(sci if sci is not None else editor)
        lines.append(f"context layer: {context.name() if context is not None else None}")
        if context is not None:
            lines.append(f"context provider: {context.providerType()}")
            lines.append(f"source table: {source_table_name(context)!r}")
        candidates = resolve_table_candidates(sci if sci is not None else editor)
        lines.append(f"table candidates: {candidates}")

        # This must DIFFER between two expression slots (e.g. fill colour vs
        # stroke colour). If it is identical, their remembered choices collide.
        try:
            from .rtl_readmode import expression_context_key

            lines.append(f"context key: {expression_context_key(sci if sci is not None else editor)!r}")
        except Exception as exc:
            lines.append(f"context key: FAILED {exc!r}")

        if usable and field_name:
            try:
                entries = cache().lookup(field_name, candidates)
                lines.append(f"rows found: {len(entries)}")
                if entries:
                    lines.append(
                        "sample: " + ", ".join(e.display for e in entries[:5])
                    )
            except Exception as exc:
                lines.append(f"lookup raised: {exc}")

        return "\n".join(lines)

    def _show_report(self) -> None:
        """Display the report over the modal dialog."""
        from qgis.PyQt.QtWidgets import QMessageBox

        try:
            text = self._report_text()
        except Exception as exc:
            text = f"report failed: {exc}"
        try:
            box = QMessageBox(self._editor)
            box.setWindowTitle("RTL autocomplete - diagnostic")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText("Ctrl+Space diagnostic")
            box.setDetailedText(text)
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
        except Exception:
            pass
        # Then run the real completion. Ctrl+Shift+D is known to reach the
        # editor, so if the popup appears now but Ctrl+Space does nothing, the
        # key is being intercepted before Qt rather than the feature being
        # broken. This makes one keypress test the entire path.
        try:
            self.trigger()
        except Exception:
            pass

    def _on_popup_key(self, event) -> bool:
        """Handle a key while the list is open.  Returns True if consumed.

        Keys we do not use are forwarded to the editor, so typing keeps working
        and narrows the list; the list is then refiltered.
        """
        key = event.key()

        if key == Qt.Key.Key_Escape:
            self.hide_popup()
            return True
        if key == Qt.Key.Key_Down:
            self._popup.move_selection(1)
            return True
        if key == Qt.Key.Key_Up:
            self._popup.move_selection(-1)
            return True
        if key == Qt.Key.Key_PageDown:
            self._popup.move_selection(5)
            return True
        if key == Qt.Key.Key_PageUp:
            self._popup.move_selection(-5)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            self.accept_current()
            return True
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End):
            # Caret navigation ends the completion session.
            self.hide_popup()
            self._forward_to_editor(event)
            return True

        # Printable characters, Backspace, Delete: let the editor apply them.
        self._forward_to_editor(event)
        self._refilter_soon()
        return True

    def _forward_to_editor(self, event) -> None:
        """Re-send a key event to the editor.

        ``_forwarding`` stops the editor branch of eventFilter from seeing the
        forwarded event and looping back into the popup handler.
        """
        if self._editor is None:
            return
        self._forwarding = True
        try:
            QApplication.sendEvent(self._editor, event)
        except Exception:
            pass
        finally:
            self._forwarding = False

    def _refilter_soon(self) -> None:
        """Refilter after the editor has applied the keystroke.

        A zero-delay single shot, not a timer loop: it just defers to the end of
        the current event cycle so ``_current_token()`` sees the new text.
        """
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(0, self._refilter)

    # -- the feature ------------------------------------------------------- #

    def trigger(self) -> None:
        """Ctrl+Space: detect the field, fetch values, show the popup."""
        editor = self._editor
        if editor is None:
            return

        enabled, reason = Settings.autocomplete_is_usable()
        if not enabled:
            _dbg(f"Ctrl+Space ignored: {reason}")
            return

        text_before = editor.toPlainText()[: editor.textCursor().position()]
        sci = getattr(editor, "_sci", None)
        probe = sci if sci is not None else editor

        kind = suggestion_context(text_before)
        self._field_mode = kind == "fields"
        self._entries = self._collect_entries(kind, text_before, probe)

        if not self._entries:
            self._notice("Nothing to suggest here")
            return

        self._refilter(show=True)

    def _collect_entries(self, kind: str, text_before: str, probe) -> List[AutocompleteEntry]:
        """Build the ordered, grouped entry list for this context.

        Ordering is by relevance: the most likely group first, then the rest.
        Built-in functions and variables are appended to the value context too,
        so they are always reachable - Scintilla's own list is unavailable to an
        overlay, and hiding ours behind a guess would leave no way to get at
        them.
        """
        from .rtl_readmode import _find_context_layer

        try:
            layer = _find_context_layer(probe)
        except Exception:
            layer = None

        entries: List[AutocompleteEntry] = []

        def add_fields() -> None:
            for name in context_field_names(layer):
                entries.append(
                    AutocompleteEntry(
                        value=f'"{name}"',
                        group_code=GROUP_FIELDS,
                        display_text=name,
                        insert_text=f'"{name}"',
                    )
                )

        def add_functions() -> None:
            for name, signature, help_html, param_count in builtin_functions():
                entries.append(
                    AutocompleteEntry(
                        value=name,
                        group_code=GROUP_FUNCTIONS,
                        display_text=signature,
                        insert_text=f"{name}()",
                        help_text=help_html,
                        # Land the caret between the parentheses when the
                        # function takes arguments; after them when it does not.
                        caret_offset=1 if param_count else 0,
                    )
                )

        def add_variables() -> None:
            for name in builtin_variables(layer):
                entries.append(
                    AutocompleteEntry(
                        value=f"@{name}",
                        group_code=GROUP_VARIABLES,
                        display_text=f"@{name}",
                        insert_text=f"@{name}",
                    )
                )

        def add_operators() -> None:
            for token in _OPERATORS:
                entries.append(
                    AutocompleteEntry(
                        value=token, group_code=GROUP_OPERATORS, display_text=token
                    )
                )

        def add_values() -> None:
            field_name = detect_field_name(text_before)
            if not field_name:
                return
            self._field_name = field_name
            try:
                tables = resolve_table_candidates(probe)
                found = cache().lookup(field_name, tables)
            except Exception as exc:
                _dbg(f"Value lookup failed: {exc}")
                return
            for entry in found:
                if not entry.group_code and not entry.group_description:
                    entry.group_code = GROUP_VALUES
                entries.append(entry)

        if kind == "fields":
            add_fields()
            if not entries:
                # No layer resolved - fall back to the lookup table's own list.
                self._offer_field_names_into(entries, probe)
        elif kind == "variables":
            add_variables()
        elif kind == "values":
            add_values()
            add_functions()
            add_variables()
        else:
            add_fields()
            add_functions()
            add_variables()
            add_operators()

        return entries

    def _offer_field_names_into(self, entries: List[AutocompleteEntry], probe) -> None:
        """Field names taken from the lookup table, when the layer is unknown."""
        try:
            tables = resolve_table_candidates(probe)
            for name in cache().lookup_field_names(tables):
                entries.append(
                    AutocompleteEntry(
                        value=f'"{name}"',
                        group_code=GROUP_FIELDS,
                        display_text=name,
                        insert_text=f'"{name}"',
                    )
                )
        except Exception as exc:
            _dbg(f"Field-name fallback failed: {exc}")

    def _offer_field_names(self) -> None:
        """Populate the popup with field names rather than values."""
        editor = self._editor
        sci = getattr(editor, "_sci", None)
        tables = resolve_table_candidates(sci if sci is not None else editor)

        try:
            names = cache().lookup_field_names(tables)
        except Exception as exc:
            _dbg(f"Field-name lookup raised: {exc}")
            names = []

        if not names:
            self._notice(
                "No field names defined"
                + (f" for table '{tables[0]}'" if tables else "")
            )
            return

        self._field_mode = True
        self._entries = [AutocompleteEntry(value=n) for n in names]
        self._refilter(show=True)

    def _notice(self, text: str) -> None:
        """Show a transient explanatory popup instead of failing silently."""
        try:
            if self._popup is None:
                self._popup = AutocompletePopup(self._editor)
                self._popup.itemClicked.connect(lambda _i: self.accept_current())
                self._popup.installEventFilter(self)
            self._entries = []
            self._popup.show_notice(text)
        except Exception:
            pass

    def _restore_caret(self) -> None:
        """Force the caret to repaint, without touching focus.

        Re-applying the text cursor makes QPlainTextEdit re-render and restart
        the caret blink.  Deliberately does *not* call setFocus(): this is
        reached from the editor's own FocusOut handling, and calling setFocus()
        while a FocusOut is being processed can leave Qt with no focused widget
        at all - which is itself a way to lose the caret.

        Re-applying an unchanged cursor emits cursorPositionChanged, which the
        existing synchronisation handles as a no-op push of the same position.
        No document modification occurs.
        """
        editor = self._editor
        if editor is None:
            return
        try:
            editor.setTextCursor(editor.textCursor())
            editor.ensureCursorVisible()
            editor.viewport().update()
        except Exception:
            pass

    def _current_token(self) -> str:
        """The partial value already typed immediately before the caret."""
        editor = self._editor
        if editor is None:
            return ""
        cursor = editor.textCursor()
        line = cursor.block().text()[: cursor.positionInBlock()]
        match = _TOKEN_RE.search(line)
        return match.group(0) if match else ""

    def _refilter(self, show: bool = False) -> None:
        """Apply the typed prefix and repopulate, hiding when nothing matches."""
        if self._editor is None or not self._entries:
            return
        token = self._current_token().lower()
        if token:
            subset = [
                e
                for e in self._entries
                if _unquote_for_display(e.value).lower().startswith(token)
            ]
            if not subset:
                subset = [
                    e
                    for e in self._entries
                    if token in e.filter_text.lower()
                ]
        else:
            subset = list(self._entries)

        if not subset:
            self.hide_popup()
            return

        if self._popup is None:
            self._popup = AutocompletePopup(self._editor)
            self._popup.itemClicked.connect(lambda _i: self.accept_current())
            # Filter the popup as well as the editor: as a Qt.Popup window it
            # holds the keyboard and mouse grabs (see eventFilter).
            self._popup.installEventFilter(self)
        self._popup.populate(subset)
        if show or self._popup.isVisible():
            self._popup.show_at_cursor()

    def accept_current(self) -> None:
        """Insert the selected value - only the value, never the description."""
        if self._popup is None:
            return
        value = self._popup.current_value()
        insert_text = value
        caret_offset = 0
        try:
            item = self._popup.currentItem()
            if item is not None:
                insert_text = str(item.data(INSERT_ROLE) or value)
                caret_offset = int(item.data(CARET_ROLE) or 0)
        except Exception:
            pass
        chosen_description = ""
        try:
            item = self._popup.currentItem()
            if item is not None:
                chosen_description = str(item.data(DESC_ROLE) or "")
        except Exception:
            pass
        self.hide_popup()
        if not value or self._editor is None:
            return

        # Replace the partial token, then insert.  This is a normal document
        # edit, so the existing overlay -> Scintilla synchronisation picks it up
        # through textChanged like any keystroke would.
        token_len = len(self._current_token())
        cursor = self._editor.textCursor()
        cursor.beginEditBlock()
        if token_len:
            cursor.movePosition(
                QTextCursor.MoveOperation.Left,
                QTextCursor.MoveMode.KeepAnchor,
                token_len,
            )
        # In field-name mode, close the quote for the user unless one is already
        # sitting to the right of the caret - the same courtesy every editor
        # extends when completing a bracketed or quoted token.
        if self._field_mode:
            text = self._editor.toPlainText()
            after = text[cursor.position():cursor.position() + 1]
            # Field entries are inserted already quoted, so only trim when a
            # closing quote is already sitting to the right of the caret.
            if after == '"' and insert_text.endswith('"'):
                insert_text = insert_text[:-1]

        cursor.insertText(insert_text)
        cursor.endEditBlock()
        if caret_offset:
            cursor.movePosition(
                QTextCursor.MoveOperation.Left,
                QTextCursor.MoveMode.MoveAnchor,
                caret_offset,
            )
        self._editor.setTextCursor(cursor)

        # A function was just completed - show its signature immediately.
        if caret_offset:
            self._call_tip_soon()

        # Record which meaning was chosen, so read mode can resolve a code that
        # has several. Stores the choice, not a copy of the expression - see
        # ChoiceMemory for why that distinction matters.
        if not self._field_mode and chosen_description:
            try:
                from .rtl_readmode import (
                    ChoiceMemory,
                    expression_context_key,
                    occurrence_index,
                )

                sci = getattr(self._editor, "_sci", None)
                tables = resolve_table_candidates(sci if sci is not None else self._editor)

                # Identify WHICH occurrence of this code we just inserted, so a
                # value the user typed by hand keeps showing every meaning while
                # this one resolves to the description they picked. Count over
                # the text up to the insertion point, excluding the partial
                # token we replaced - otherwise that fragment would be counted
                # as an earlier literal and shift the index.
                cursor_position = self._editor.textCursor().position()
                insertion_start = max(0, cursor_position - len(value))
                text_before = self._editor.toPlainText()[:insertion_start]
                index = occurrence_index(text_before, self._field_name, value)

                # Scope the choice to THIS expression slot, so a fill-colour
                # override and a stroke-colour override on the same layer keep
                # separate descriptions instead of overwriting each other.
                context = expression_context_key(sci if sci is not None else self._editor)

                ChoiceMemory.remember(
                    tables[0] if tables else "",
                    self._field_name,
                    value,
                    chosen_description,
                    index,
                    context,
                )
            except Exception as exc:
                _dbg(f"Could not remember choice: {exc}")

    def _call_tip_soon(self) -> None:
        """Show the call tip after the document has settled."""
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(0, self.show_call_tip)

    def show_call_tip(self) -> None:
        """Signature hint for the function enclosing the caret.

        Our own tooltip rather than Scintilla's call tip, which has the same
        limitation as its completion list: it needs Scintilla to own the input
        loop. A QToolTip renders the HTML that helpText() returns and works over
        a modal dialog.
        """
        editor = self._editor
        if editor is None:
            return
        try:
            text = editor.toPlainText()
            position = editor.textCursor().position()
            name = self._enclosing_function(text, position)
            if not name:
                QToolTip.hideText()
                return

            for func_name, signature, help_html, _count in builtin_functions():
                if func_name.lower() != name.lower():
                    continue
                summary = re.sub(r"<[^>]+>", " ", help_html or "")
                summary = re.sub(r"\s+", " ", summary).strip()
                if len(summary) > 220:
                    summary = summary[:220].rsplit(" ", 1)[0] + "..."
                body = f"<b>{signature}</b>"
                if summary:
                    body += f"<br>{summary}"
                point = editor.mapToGlobal(editor.cursorRect().bottomLeft())
                QToolTip.showText(point, body, editor)
                return
            QToolTip.hideText()
        except Exception as exc:
            _dbg(f"Call tip failed: {exc}")

    @staticmethod
    def _enclosing_function(text: str, position: int) -> str:
        """Name of the function whose parentheses contain ``position``.

        Walks backwards counting bracket depth, so a nested call reports the
        innermost function - the one the caret is actually inside.
        """
        depth = 0
        index = position - 1
        while index >= 0:
            char = text[index]
            if char == ")":
                depth += 1
            elif char == "(":
                if depth == 0:
                    end = index
                    start = end
                    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_$"):
                        start -= 1
                    return text[start:end].strip()
                depth -= 1
            index -= 1
        return ""

    def hide_popup(self) -> None:
        """Close the list and hand the caret back to the editor."""
        if self._popup is not None:
            try:
                self._popup.hide()
            except Exception:
                pass
        self._restore_caret()


# --------------------------------------------------------------------------- #
# Console diagnostic
# --------------------------------------------------------------------------- #


def force_complete() -> None:
    """Trigger completion from the Python Console, bypassing the keyboard.

    This isolates the two possible causes of "Ctrl+Space does nothing":

    * the popup appears -> the whole lookup path works and the problem is
      purely that the key is not reaching the editor (a shortcut conflict, or an
      input method grabbing Ctrl+Space at the OS level);
    * nothing happens -> the controller is not attached to the live editor,
      which usually means the installed plugin is running an older copy of this
      module. Check the reported __file__ against the plugin folder.

    Open the Expression Builder or Layer Filter, click into the editor, then::

        from rtl_bidi_editor.rtl_autocomplete import force_complete
        force_complete()
    """
    from qgis.PyQt.QtWidgets import QApplication, QPlainTextEdit

    print("=" * 68)
    print("RTL autocomplete - forced trigger")
    print("=" * 68)
    print(f"module file : {__file__}")

    editors = []
    focused = QApplication.focusWidget()
    if isinstance(focused, QPlainTextEdit):
        editors.append(focused)
    for widget in QApplication.allWidgets():
        if (
            isinstance(widget, QPlainTextEdit)
            and widget.objectName() == "rtlBidiOverlayEditor"
            and widget not in editors
        ):
            editors.append(widget)

    if not editors:
        print("No overlay editor found. Is the Expression Builder / Filter open?")
        print("=" * 68)
        return

    for editor in editors:
        controller = getattr(editor, "_custom_autocomplete", "MISSING")
        print(f"editor      : {editor.objectName()!r} visible={editor.isVisible()}")
        print(f"controller  : {controller}")
        if controller in (None, "MISSING"):
            print("  >>> The controller is not attached. The installed plugin is")
            print("      probably running an older rtl_autocomplete.py, or its")
            print("      import failed - check the Log Messages panel.")
            continue
        cursor_text = editor.toPlainText()[: editor.textCursor().position()]
        print(f"text before : {cursor_text!r}")
        print(f"detected    : {detect_field_name(cursor_text)!r}")
        try:
            controller.trigger()
            print("  trigger() called - a popup or a notice should be visible.")
        except Exception as exc:
            print(f"  trigger() raised: {exc}")
    print("=" * 68)


def diagnose(field_name: str = "") -> None:
    """Print why Ctrl+Space is or is not producing results.

    Run from the QGIS Python Console with the layer you are filtering selected
    in the layer tree::

        from rtl_bidi_editor.rtl_autocomplete import diagnose
        diagnose("COUNTRY")

    Uses print() rather than the message log on purpose, so it cannot raise the
    Log Messages dock and steal focus from a modal dialog.
    """
    print("=" * 68)
    print("RTL autocomplete diagnostic")
    print("=" * 68)

    usable, reason = Settings.autocomplete_is_usable()
    print(f"configuration usable : {usable}" + ("" if usable else f"  ({reason})"))
    print(f"enabled              : {Settings.autocomplete_enabled()}")

    layer = Settings.autocomplete_layer()
    print(f"lookup layer         : {layer.name() if layer else None}")
    if layer is not None:
        try:
            print(f"lookup layer fields  : {[f.name() for f in layer.fields()]}")
            print(f"lookup feature count : {layer.featureCount()}")
        except Exception as exc:
            print(f"  (could not read fields: {exc})")
    for key in ("table", "field_names", "value", "description", "group_code", "group_description"):
        print(f"  field[{key:17s}] = {Settings.field(key)!r}")

    context = _find_context_layer(None)
    print("-" * 68)
    if context is None:
        print("context layer        : NONE  <- table filtering cannot work")
        print("   Select the layer you are filtering in the Layers panel.")
    else:
        print(f"context layer        : {context.name()!r}")
        try:
            print(f"context source       : {context.source()}")
            print(f"context provider     : {context.providerType()}")
        except Exception:
            pass
        print(f"source_table_name()  : {source_table_name(context)!r}")

    candidates = resolve_table_candidates(None)
    print(f"table candidates     : {candidates}")

    if not field_name:
        print("\nPass a field name to test a lookup, e.g. diagnose('COUNTRY').")
        print("=" * 68)
        return

    if not usable:
        print("\nConfiguration is not usable; fix the above first.")
        print("=" * 68)
        return

    print("-" * 68)
    f_names = Settings.field("field_names")
    f_table = Settings.field("table")
    instance = cache()
    for label, exact, use_table in (
        ("exact + table", True, True),
        ("substring + table", False, True),
        ("exact, no table", True, False),
        ("substring, no table", False, False),
    ):
        expression = AutocompleteCache._expression(
            field_name, f_names, f_table if use_table else "", candidates, exact
        )
        rows = instance._run(
            layer,
            [n for n in (f_names, Settings.field("value"), f_table,
                         Settings.field("description"), Settings.field("group_code"),
                         Settings.field("group_description")) if n],
            expression,
            Settings.field("value"),
            Settings.field("description"),
            Settings.field("group_code"),
            Settings.field("group_description"),
        )
        print(f"{label:22s} -> {len(rows):4d} rows")
        print(f"{'':22s}    {expression}")
        _check_expression(expression)
        if rows:
            print(f"{'':22s}    sample: {[r.display for r in rows[:5]]}")

    # The single most useful output: what values actually exist in the lookup
    # table.  A mismatch between these and the table candidates above is the
    # usual reason a table-filtered lookup returns nothing.
    print("-" * 68)
    print("LOOKUP LAYER CONTENTS (repr, so hidden characters are visible)")
    _print_unique_values(layer, f_table, "Table Field")
    _print_unique_values(layer, Settings.field("field_names"), "Fields Names Field")

    print("-" * 68)
    print("PURE-PYTHON CROSS-CHECK (bypasses the expression engine)")
    _python_side_check(layer, field_name, candidates)
    print("=" * 68)


def _print_unique_values(layer, field_name: str, label: str, limit: int = 40) -> None:
    """Print the distinct values of one field, as repr().

    repr() matters: it exposes trailing spaces, non-breaking spaces and other
    invisible characters that make an apparently correct value fail to compare
    equal.  A plain print would hide exactly the problem we are hunting.
    """
    if layer is None or not field_name:
        print(f"  {label:22s}: (not configured)")
        return
    try:
        index = layer.fields().indexOf(field_name)
        if index < 0:
            print(f"  {label:22s}: field {field_name!r} NOT FOUND in the layer")
            return
        field = layer.fields().at(index)
        values = sorted(
            repr(str(v)) for v in layer.uniqueValues(index, limit + 1) if v is not None
        )
        suffix = " ..." if len(values) > limit else ""
        print(f"  {label:22s} field={field_name!r} type={field.typeName()}")
        print(f"  {'':22s} values={values[:limit]}{suffix}")
    except Exception as exc:
        print(f"  {label:22s}: could not read values ({exc})")


def _python_side_check(layer, field_name: str, candidates) -> None:
    """Compare in pure Python, bypassing the expression engine entirely.

    This is the decisive test.  If Python finds matching rows but none of the
    expression passes do, the fault is in the query or the provider.  If Python
    finds none either, the stored data genuinely differs from what the attribute
    table appears to show - check the repr() output above for hidden characters,
    and check whether the field uses a value map or value relation widget, which
    displays a label while storing a different code.
    """
    f_names = Settings.field("field_names")
    f_table = Settings.field("table")
    if layer is None or not f_names:
        return

    wanted_field = field_name.strip().lower()
    wanted_tables = {c.strip().lower() for c in candidates}

    field_hits = 0
    both_hits = 0
    table_values_seen = set()
    samples = []
    try:
        for feature in layer.getFeatures():
            raw_field = feature[f_names] if f_names else None
            if raw_field is None:
                continue
            if str(raw_field).strip().lower() != wanted_field:
                continue
            field_hits += 1
            if not f_table:
                continue
            raw_table = feature[f_table]
            text = "" if raw_table is None else str(raw_table)
            table_values_seen.add(repr(text))
            if text.strip().lower() in wanted_tables:
                both_hits += 1
                if len(samples) < 5:
                    samples.append(repr(text))
    except Exception as exc:
        print(f"  python-side scan failed: {exc}")
        return

    print(f"  rows matching field {field_name!r}          : {field_hits}")
    if f_table:
        print(f"  ...of which the table also matches      : {both_hits}")
        print(f"  table values present on those rows      : {sorted(table_values_seen)}")
        if samples:
            print(f"  matching table values                   : {samples}")
        if field_hits and not both_hits:
            print("  >>> The field matches but the table does not. Compare the")
            print("      repr() values above with the table candidates; look for")
            print("      trailing spaces or a value-map/value-relation widget.")
        elif both_hits:
            print("  >>> Python finds matching rows. If the expression passes")
            print("      above returned 0, the fault is in the query, not the data.")


def _check_expression(expression: str) -> bool:
    """Report a parser error in a generated expression."""
    try:
        parsed = QgsExpression(expression)
        if parsed.hasParserError():
            print(f"  PARSER ERROR: {parsed.parserErrorString()}")
            return False
    except Exception as exc:
        print(f"  could not parse: {exc}")
        return False
    return True

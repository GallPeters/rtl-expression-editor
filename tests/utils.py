# -*- coding: utf-8 -*-
"""Shared helpers for the test suite. Not tests themselves."""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

from qgis.core import QgsFeature, QgsField, QgsVectorLayer
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QWidget


def make_layer(
    uri: str,
    name: str,
    fields: Sequence[Tuple[str, "QVariant.Type"]],
    features: Iterable[Dict[str, object]],
) -> QgsVectorLayer:
    """A throwaway in-memory vector layer, built and populated in one call."""
    layer = QgsVectorLayer(uri, name, "memory")
    assert layer.isValid(), f"memory layer failed to load ({uri!r})"
    provider = layer.dataProvider()
    provider.addAttributes([QgsField(field_name, field_type) for field_name, field_type in fields])
    layer.updateFields()
    for row in features:
        feature = QgsFeature(layer.fields())
        for key, value in row.items():
            feature.setAttribute(key, value)
        provider.addFeature(feature)
    layer.updateExtents()
    return layer


def make_context_layer(field_names: Sequence[str] = ("NAME", "COUNTRY")) -> QgsVectorLayer:
    """Stands in for "the layer being edited" - fields only, no lookup role."""
    return make_layer(
        "Point?crs=EPSG:4326",
        "context",
        [(field_name, QVariant.String) for field_name in field_names],
        [],
    )


def make_lookup_layer() -> QgsVectorLayer:
    """A lookup table using the same column roles the Settings dialog offers.

    Includes the table/field/alternative description columns too, so a
    single fixture covers both the plain lookup tests and the description
    ones - COUNTRY/IL deliberately has no alt_description, to also exercise
    "falls back to the primary description when this row has no alternative
    of its own".
    """
    return make_layer(
        "None",
        "lookup",
        [
            ("field_name", QVariant.String),
            ("value", QVariant.String),
            ("description", QVariant.String),
            ("alt_description", QVariant.String),
            ("group_code", QVariant.String),
            ("group_description", QVariant.String),
            ("table", QVariant.String),
            ("table_description", QVariant.String),
            ("field_description", QVariant.String),
        ],
        [
            {
                "field_name": "STATUS",
                "value": "1",
                "description": "Active",
                "alt_description": "פעיל",
                "group_code": "G1",
                "group_description": "State",
                "table": "context",
                "table_description": "הקשר",
                "field_description": "מצב",
            },
            {
                "field_name": "STATUS",
                "value": "2",
                "description": "Inactive",
                "alt_description": "לא פעיל",
                "group_code": "G1",
                "group_description": "State",
                "table": "context",
                "table_description": "הקשר",
                "field_description": "מצב",
            },
            {
                "field_name": "COUNTRY",
                "value": "IL",
                "description": "Israel",
                "alt_description": "",
                "group_code": "",
                "group_description": "",
                "table": "context",
                "table_description": "הקשר",
                "field_description": "מדינה",
            },
        ],
    )


def host_in_dialog(widget: QWidget) -> QDialog:
    """Put ``widget`` inside a plain QDialog.

    Stands in for the Expression Builder / Field Calculator / Layer Filter
    window it would normally live inside: the plugin only ever needs a real
    ``window()`` and a real ``Show`` event to react to, never anything
    specific to those dialogs' own logic.
    """
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    layout.addWidget(widget)
    dialog.resize(500, 300)
    return dialog


def reset_plugin_settings() -> None:
    """Clear every autocomplete-related setting, so tests never see leftovers
    from a previous run or from the user's own real configuration.
    """
    from _rtl_plugin.rtl_settings import Settings

    Settings.set_autocomplete_enabled(False)
    Settings.set_layer_id("")
    for key in Settings.FIELD_KEYS:
        Settings.set_field(key, "")


def reset_choice_memory():
    """Snapshot the active project's remembered-choice entry, blank it, and
    return a zero-argument callable that restores the exact original
    snapshot - call it from ``tearDown``.

    ``ChoiceMemory`` is keyed by (context, table, field, code, occurrence),
    not by anything test-specific, so a test that only snapshots-and-restores
    (the pattern this used to be, repeated across most of the classes in
    ``test_read_mode.py``) still reads and counts whatever a REAL project
    already has stored under the same project-scoped property - exactly the
    situation whenever this suite runs from inside a live QGIS session via
    the Settings dialog's "Run Tests" button, with the user's own actual
    remembered choices already in the open project. That silently inflated
    every ``total``/``deleted`` count and failure list by however many real
    entries happened to be present, with no relation to what the test itself
    added - blanking the entry here, on top of the same snapshot/restore, is
    what makes a test see only what it put there itself, regardless of the
    project it happens to run inside.
    """
    from qgis.core import QgsProject

    from _rtl_plugin import rtl_readmode as rm

    project = QgsProject.instance()
    scope, key = rm.ChoiceMemory.SCOPE, rm.ChoiceMemory.KEY
    original, existed = project.readEntry(scope, key, "")
    project.removeEntry(scope, key)
    rm.ChoiceMemory.invalidate()

    def _restore() -> None:
        if existed:
            project.writeEntry(scope, key, original)
        else:
            project.removeEntry(scope, key)
        rm.ChoiceMemory.invalidate()

    return _restore

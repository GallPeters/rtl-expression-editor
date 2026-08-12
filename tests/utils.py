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
    """A lookup table using the same column roles the Settings dialog offers."""
    return make_layer(
        "None",
        "lookup",
        [
            ("field_name", QVariant.String),
            ("value", QVariant.String),
            ("description", QVariant.String),
            ("group_code", QVariant.String),
            ("group_description", QVariant.String),
            ("table", QVariant.String),
        ],
        [
            {
                "field_name": "STATUS",
                "value": "1",
                "description": "Active",
                "group_code": "G1",
                "group_description": "State",
                "table": "context",
            },
            {
                "field_name": "STATUS",
                "value": "2",
                "description": "Inactive",
                "group_code": "G1",
                "group_description": "State",
                "table": "context",
            },
            {
                "field_name": "COUNTRY",
                "value": "IL",
                "description": "Israel",
                "group_code": "",
                "group_description": "",
                "table": "context",
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
    from src.rtl_settings import Settings

    Settings.set_autocomplete_enabled(False)
    Settings.set_layer_id("")
    for key in Settings.FIELD_KEYS:
        Settings.set_field(key, "")

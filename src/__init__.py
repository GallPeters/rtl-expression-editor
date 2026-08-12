# -*- coding: utf-8 -*-
"""
RTL / BiDi Code Editor for QGIS
===============================

QGIS plugin entry point.

The core editor/overlay mechanism (detect editor -> overlay it -> keep it in
sync) lives in ``rtl_editor.py``; the optional autocomplete, read-mode and
settings features each live in their own module alongside it.
"""


def classFactory(iface):  # noqa: N802  (name mandated by the QGIS plugin API)
    """Instantiate the plugin. Called by the QGIS plugin manager."""
    from .rtl_editor import RtlBidiEditorPlugin

    return RtlBidiEditorPlugin(iface)

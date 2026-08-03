# -*- coding: utf-8 -*-
"""
RTL / BiDi Code Editor for QGIS
===============================

QGIS plugin entry point.

The whole implementation lives in a single module (``rtl_bidi_editor.py``) on
purpose: the plugin is one cohesive mechanism (detect editor -> overlay it ->
keep it in sync) and splitting it would add indirection without any
maintenance benefit.
"""


def classFactory(iface):  # noqa: N802  (name mandated by the QGIS plugin API)
    """Instantiate the plugin. Called by the QGIS plugin manager."""
    from .rtl_bidi_editor import RtlBidiEditorPlugin

    return RtlBidiEditorPlugin(iface)

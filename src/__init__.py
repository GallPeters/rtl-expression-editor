# -*- coding: utf-8 -*-

"""
RTL Companion Editor plugin package.

This file is required by the QGIS plugin loader.
"""


def classFactory(iface):
    """
    QGIS plugin entry point.

    :param iface: QGIS interface object.
    :return: Main plugin object.
    """
    from .plugin import RtlEditorPlugin

    return RtlEditorPlugin(iface)
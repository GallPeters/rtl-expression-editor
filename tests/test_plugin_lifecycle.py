# -*- coding: utf-8 -*-
"""The plugin's own load/unload cycle, as QGIS's plugin manager drives it."""

import unittest

from _rtl_plugin import rtl_editor as ed

from . import classFactory


class PluginLifecycleTests(unittest.TestCase):
    def test_class_factory_returns_a_plugin_instance(self):
        plugin = classFactory(iface=None)
        self.assertIsInstance(plugin, ed.RtlBidiEditorPlugin)

    def test_init_gui_and_unload_do_not_raise_even_with_no_real_iface(self):
        # QGIS always supplies a real iface; every failure path here is
        # exercised anyway, since every step is independently guarded (a
        # deliberate design choice: one failed optional feature must never
        # break plugin load).
        plugin = classFactory(iface=None)
        plugin.initGui()
        try:
            self.assertIsNotNone(plugin._watcher)
        finally:
            plugin.unload()
        self.assertIsNone(plugin._watcher)

    def test_unload_before_init_gui_does_not_raise(self):
        plugin = classFactory(iface=None)
        plugin.unload()  # never initialised - must be a safe no-op

    def test_removing_a_layer_purges_its_remembered_choices(self):
        """End-to-end: a layer removed from the project takes its
        remembered value/description choices with it, since
        reconcile_choices() only ever runs from a dialog's own OK - it has
        no way to notice the layer behind it disappearing on its own."""
        from qgis.core import QgsProject, QgsVectorLayer

        from _rtl_plugin.rtl_readmode import ChoiceMemory

        layer = QgsVectorLayer("Point?crs=EPSG:4326", "purge_test_layer", "memory")
        self.assertTrue(layer.isValid())
        project = QgsProject.instance()
        project.addMapLayer(layer)

        original, existed = project.readEntry("rtl_bidi_editor", "value_choices", "")
        plugin = classFactory(iface=None)
        plugin.initGui()
        try:
            context = f"QgsQueryBuilderBase|Query Builder|QgisApp|{layer.id()}"
            ChoiceMemory.remember("t", "f", "1", "Active", 0, context)
            self.assertEqual(ChoiceMemory.recall("t", "f", "1", 0, context), "Active")

            project.removeMapLayer(layer.id())

            self.assertEqual(ChoiceMemory.recall("t", "f", "1", 0, context), "")
        finally:
            plugin.unload()
            if existed:
                project.writeEntry("rtl_bidi_editor", "value_choices", original)
            else:
                project.removeEntry("rtl_bidi_editor", "value_choices")
            ChoiceMemory.invalidate()


if __name__ == "__main__":
    unittest.main()

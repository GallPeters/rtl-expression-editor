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

    def test_init_gui_purges_any_legacy_remembered_choice_entry(self):
        """A project carrying the old, retired "remembered choices" entry
        (see rtl_readmode.purge_legacy_project_entries()) starts clean the
        moment the plugin activates, rather than carrying dead data around
        indefinitely."""
        from qgis.core import QgsProject

        project = QgsProject.instance()
        original, existed = project.readEntry("rtl_bidi_editor", "value_choices", "")
        project.writeEntry("rtl_bidi_editor", "value_choices", '{"stale": "entry"}')

        plugin = classFactory(iface=None)
        try:
            plugin.initGui()
            _raw, still_there = project.readEntry("rtl_bidi_editor", "value_choices", "")
            self.assertFalse(still_there)
        finally:
            plugin.unload()
            if existed:
                project.writeEntry("rtl_bidi_editor", "value_choices", original)
            else:
                project.removeEntry("rtl_bidi_editor", "value_choices")

    def test_init_gui_does_not_raise_when_there_is_no_legacy_entry(self):
        from qgis.core import QgsProject

        project = QgsProject.instance()
        original, existed = project.readEntry("rtl_bidi_editor", "value_choices", "")
        project.removeEntry("rtl_bidi_editor", "value_choices")

        plugin = classFactory(iface=None)
        try:
            plugin.initGui()  # must not raise
        finally:
            plugin.unload()
            if existed:
                project.writeEntry("rtl_bidi_editor", "value_choices", original)


if __name__ == "__main__":
    unittest.main()

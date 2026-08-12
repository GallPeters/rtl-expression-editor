# -*- coding: utf-8 -*-
"""The plugin's own load/unload cycle, as QGIS's plugin manager drives it."""

import unittest

from .. import classFactory
from .. import rtl_editor as ed


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


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-

"""
Main QGIS plugin object.

Hotfix version adds:

- Enable/disable automatic monitoring action.
- Manual "Scan dialogs now" action for debugging and recovery.
"""

from PyQt6.QtGui import QAction

from .monitor import RtlEditorMonitor
from .utils import log_exception, log_message


class RtlEditorPlugin:
    """
    QGIS plugin implementation.
    """

    def __init__(self, iface):
        self.iface = iface
        self.monitor = None
        self.toggle_action = None
        self.scan_action = None
        self.actions = []
        self._menu_name = "&RTL Editor"

    def initGui(self):
        """Called by QGIS when the plugin is loaded."""
        parent = None

        try:
            if self.iface is not None:
                parent = self.iface.mainWindow()
        except Exception:
            parent = None

        self.monitor = RtlEditorMonitor(self.iface, parent=None)

        self.toggle_action = QAction("RTL Companion Editor", parent)
        self.toggle_action.setCheckable(True)
        self.toggle_action.setChecked(True)
        self.toggle_action.triggered.connect(self._toggle_monitor)

        self.scan_action = QAction("Scan dialogs now", parent)
        self.scan_action.setCheckable(False)
        self.scan_action.triggered.connect(self._scan_now)

        self.actions = [
            self.toggle_action,
            self.scan_action,
        ]

        if self.iface is not None:
            for action in self.actions:
                try:
                    self.iface.addPluginToMenu(self._menu_name, action)
                except Exception as e:
                    log_exception("RtlEditorPlugin.initGui.addPluginToMenu", e)

        self.monitor.start()

    def unload(self):
        """Called by QGIS when the plugin is unloaded."""
        if self.monitor is not None:
            try:
                self.monitor.stop()
            except Exception as e:
                log_exception("RtlEditorPlugin.unload.monitor.stop", e)

            try:
                self.monitor.deleteLater()
            except Exception:
                pass

            self.monitor = None

        if self.iface is not None:
            for action in self.actions:
                try:
                    self.iface.removePluginMenu(self._menu_name, action)
                except Exception as e:
                    log_exception("RtlEditorPlugin.unload.removePluginMenu", e)

        self.toggle_action = None
        self.scan_action = None
        self.actions = []

    def _toggle_monitor(self, checked):
        """Enable or disable monitoring from the menu action."""
        if self.monitor is None:
            return

        try:
            if checked:
                self.monitor.start()
                log_message("Automatic companion monitoring enabled.")
            else:
                self.monitor.stop()
                log_message("Automatic companion monitoring disabled.")
        except Exception as e:
            log_exception("RtlEditorPlugin._toggle_monitor", e)

    def _scan_now(self):
        """Manually scan open dialogs."""
        if self.monitor is None:
            return

        try:
            log_message("Manual scan requested from menu.")
            self.monitor.scan_now(force=True)
        except Exception as e:
            log_exception("RtlEditorPlugin._scan_now", e)
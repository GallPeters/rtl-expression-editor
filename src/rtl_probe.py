# -*- coding: utf-8 -*-
"""
PROBE - paste into the QGIS Python Console, then open the Expression Builder.

This is independent of the plugin.  It answers one question:
does an application-level event filter see window Show events in your QGIS?
"""

from qgis.PyQt.QtCore import QEvent, QObject
from qgis.PyQt.QtWidgets import QApplication, QWidget


class _Probe(QObject):
    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.Type.Show and isinstance(obj, QWidget):
                if obj.isWindow():
                    print(f"SHOW WINDOW : {type(obj).__name__}")
                    for child in obj.findChildren(QWidget):
                        name = type(child).__name__
                        if "Scintilla" in name or "CodeEditor" in name:
                            print(
                                f"      editor: {name}  "
                                f"objectName={child.objectName()!r}  "
                                f"size={child.width()}x{child.height()}"
                            )
        except Exception as exc:
            print("probe error:", exc)
        return False


# Keep a module-level reference or Python will garbage-collect the filter.
probe = _Probe()
QApplication.instance().installEventFilter(probe)
print("Probe installed. Now open the Expression Builder / Layer Filter.")
print("To remove later:  QApplication.instance().removeEventFilter(probe)")

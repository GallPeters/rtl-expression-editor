# -*- coding: utf-8 -*-
"""
RTL / BiDi editor - DIAGNOSTIC
==============================

HOW TO USE
----------
1. Open the Expression Builder (or the Layer Filter / Query Builder) and
   LEAVE IT OPEN.
2. In QGIS: Plugins -> Python Console.
3. Paste this whole file into the console and press Enter.
4. Copy the printed output back to me.

It only reads and prints - it changes nothing.
"""

from qgis.PyQt.QtWidgets import QApplication, QWidget

print("=" * 72)
print("RTL / BiDi DIAGNOSTIC")
print("=" * 72)

# --- 1. Which QGIS / Qt are we on? ---------------------------------------- #
try:
    from qgis.core import Qgis
    from qgis.PyQt.QtCore import QT_VERSION_STR, PYQT_VERSION_STR

    print(f"QGIS  : {Qgis.QGIS_VERSION}")
    print(f"Qt    : {QT_VERSION_STR}   PyQt: {PYQT_VERSION_STR}")
except Exception as exc:
    print("version probe failed:", exc)

# --- 2. Are the expected classes importable? ------------------------------ #
for module_name, class_name in [
    ("qgis.gui", "QgsCodeEditor"),
    ("qgis.gui", "QgsCodeEditorExpression"),
    ("qgis.gui", "QgsCodeEditorSQL"),
    ("qgis.gui", "QgsCodeEditorWidget"),
    ("qgis.gui", "QgsCodeEditorColorScheme"),
    ("qgis.PyQt.Qsci", "QsciScintilla"),
]:
    try:
        module = __import__(module_name, fromlist=[class_name])
        print(f"import {module_name}.{class_name:28s} -> OK")
    except Exception as exc:
        print(f"import {module_name}.{class_name:28s} -> FAILED: {exc}")

# --- 3. Walk every visible window and dump editor-ish widgets ------------- #
print("-" * 72)
print("VISIBLE TOP-LEVEL WINDOWS AND THEIR EDITOR WIDGETS")
print("-" * 72)

INTERESTING = ("scintilla", "codeeditor", "editor", "textedit")
found_any = False

for window in QApplication.topLevelWidgets():
    if not window.isVisible():
        continue
    print(f"\nWINDOW  {type(window).__name__}   objectName={window.objectName()!r}")
    for child in window.findChildren(QWidget):
        name = type(child).__name__
        if not any(token in name.lower() for token in INTERESTING):
            continue
        found_any = True
        mro = [k.__name__ for k in type(child).__mro__]
        print(f"   - {name}")
        print(f"       objectName : {child.objectName()!r}")
        print(f"       visible    : {child.isVisible()}   geometry: {child.geometry()}")
        print(f"       parent     : {type(child.parentWidget()).__name__ if child.parentWidget() else None}")
        print(f"       MRO        : {' -> '.join(mro[:7])}")

if not found_any:
    print("\n*** No editor-like widget found in any visible window. ***")
    print("    Is the Expression Builder / Filter dialog actually open right now?")

# --- 4. Is the plugin's watcher alive, and what does it think? ------------ #
print("-" * 72)
try:
    from rtl_bidi_editor import rtl_bidi_editor as mod

    print("plugin module imported OK")
    print("TARGET_EDITOR_CLASSES:", sorted(mod.TARGET_EDITOR_CLASSES))
    watcher = getattr(mod, "_WATCHER", None)
    print("watcher instance     :", watcher)
    if watcher is not None:
        print("live overlays        :", len(watcher._overlays))
        print("\nRunning a forced rescan...")
        watcher.scan_existing()
        print("live overlays after  :", len(watcher._overlays))
    else:
        print("NOTE: this build of the plugin does not expose _WATCHER (pre-1.1).")
except Exception as exc:
    print("could not import plugin module:", exc)

print("=" * 72)
print("END OF DIAGNOSTIC - please copy everything above.")
print("=" * 72)

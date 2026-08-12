# -*- coding: utf-8 -*-
"""Snapshot/restore the active QgsProject around a test run.

Several tests deliberately add and remove map layers to get a clean,
predictable starting point - exactly right for a disposable qgis.testing
process, exactly wrong if this suite is ever run from inside a live QGIS
session with the user's own project open (e.g. via the Settings dialog's
"Run Tests" button). This is the outer safety net: it restores both the
project's map layers and the plugin's own project-scoped custom property
(read mode's remembered choices) to exactly what they were before the run,
regardless of what any individual test does or whether the run raises.

It is a backstop, not the primary defence - the tests themselves remove only
the specific layers they add (see e.g. test_autocomplete.py), rather than
relying on this to clean up after a blunter "wipe everything" approach. A
layer this guard finds missing that existed beforehand cannot be resurrected
(removing a QgsMapLayer destroys the underlying C++ object), which is exactly
why that discipline matters at the test level too.
"""

from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")

_SCOPE = "rtl_bidi_editor"
_KEY = "value_choices"


def guarded_run(run: Callable[[], T]) -> T:
    """Call ``run()``, then restore the active project's layers/properties."""
    from qgis.core import QgsProject

    project = QgsProject.instance()
    original_layer_ids = set(project.mapLayers().keys())
    original_entry, entry_existed = project.readEntry(_SCOPE, _KEY, "")

    try:
        return run()
    finally:
        added = set(project.mapLayers().keys()) - original_layer_ids
        if added:
            project.removeMapLayers(list(added))
        if entry_existed:
            project.writeEntry(_SCOPE, _KEY, original_entry)
        else:
            project.removeEntry(_SCOPE, _KEY)

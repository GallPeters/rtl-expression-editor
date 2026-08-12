# -*- coding: utf-8 -*-
"""
Test suite for the RTL Expression Editor plugin.

Works from either of the two layouts the plugin's own modules can be found
in, detected automatically below rather than assumed:

* the git-checkout layout: this ``tests/`` folder sits at the repository
  root, a sibling of ``src/`` (developer/QA tooling, not part of what gets
  installed - only ``src/``'s contents are packaged into a release);
* an installed-plugin layout: ``tests/`` copied *inside* the plugin's own
  install folder, alongside ``rtl_editor.py`` and the rest - the natural
  result of installing the plugin *with* its tests rather than without them.

Either way, the folder actually holding ``rtl_editor.py`` is registered as a
real package under the synthetic name ``_rtl_plugin`` - never hard-coded to
``src`` - and every test module imports from that fixed name instead, e.g.
``from _rtl_plugin import rtl_autocomplete`` or
``from _rtl_plugin.rtl_settings import Settings``.

A synthetic *package* (not a bare ``sys.path`` insertion + plain module
imports) is required because the plugin's own modules import each other with
relative imports - ``rtl_autocomplete.py`` has ``from .rtl_settings import
...``, ``rtl_editor.py`` similarly - exactly as they do once QGIS installs
and imports the real plugin package. Importing them as bare top-level
modules leaves each one with no package context of its own, and that
relative import then fails with "attempted relative import with no known
parent package". Giving the located directory a real (if arbitrarily named)
package identity, with ``submodule_search_locations`` pointing at it, is what
makes those relative imports resolve correctly regardless of what the
plugin's real install folder is actually called.

Exercises the plugin's own logic against a real QGIS/PyQt environment -
``qgis.testing`` (started once here, before any test module runs) rather than
mocks, since almost everything this plugin does is reacting to real Qt
widgets and real QgsExpression/QgsVectorLayer behaviour. Nothing here talks to
a network, a website, or any source outside the installed QGIS itself: every
function name, variable and value tested comes from the live
``QgsExpression``/``QgsExpressionContextUtils``/``QgsVectorLayer`` APIs, the
same ones the plugin itself calls at runtime - so the coverage tracks whatever
QGIS version is actually installed, automatically.

How to run:

    From the QGIS Python Console, with the repository (or an installed copy
    that includes this ``tests/`` folder) somewhere on disk::

        import sys
        sys.path.insert(0, r"<path to the folder CONTAINING tests/>")
        from tests import run_all
        run_all.main()

    From a shell, with QGIS's own Python interpreter, run from that same
    folder so the relative "tests" package resolves::

        python3 -m tests.run_all

    The plugin's own Settings dialog also has a "Run Tests" button that does
    the above automatically when a ``tests/`` folder is found either nested
    inside the running plugin's own directory, or as a sibling of it one
    level up (see ``rtl_settings.SettingsDialog._tests_directory()``) - not
    the case for a normal end-user install, which only ships ``src/``'s
    contents with no ``tests/`` anywhere nearby; the button reports that
    plainly rather than failing silently.

Nothing here is run automatically when the plugin loads - these are
developer/QA tests, run on demand, not a startup check.
"""

import importlib.util
import sys
from pathlib import Path

from qgis.core import QgsApplication

# start_app() bootstraps a whole new QgsApplication from scratch - meant for
# a standalone test process that has none yet. Its own idempotency guard
# only knows about a QgsApplication *it* previously started (tracked in a
# module-level global inside qgis.testing itself); it has no way to notice
# one that QGIS Desktop itself already constructed and initialised before
# this ever runs - exactly the case for the Settings dialog's "Run Tests"
# button, executing inside the user's already-open, already-initialised
# session. Attempting to start a second one there does not fail cleanly -
# it surfaces many calls later as a confusing, unrelated-looking TypeError
# from deep inside Qt/SIP's own plumbing. Checking for an existing instance
# first, ourselves, is what actually makes this safe either way: a
# standalone interpreter (python3 -m tests.run_all) has none yet and still
# gets one bootstrapped exactly as before; a live QGIS session already has
# one and this becomes a no-op.
if QgsApplication.instance() is None:
    from qgis.testing import start_app

    start_app()

#: Fixed name every test module imports the plugin under, regardless of what
#: its real install folder is actually called.
PLUGIN_PACKAGE_NAME = "_rtl_plugin"


def _locate_plugin_dir():
    """Where rtl_editor.py and its siblings actually live.

    Checked in the same order, and for the same reason, as
    ``rtl_settings.SettingsDialog._tests_directory()`` checks for ``tests/``:
    the git-checkout ``src/`` sibling first, then this folder's own parent
    directly (this ``tests/`` copied inside an installed plugin folder).
    """
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "src", here.parent):
        if (candidate / "rtl_editor.py").is_file():
            return candidate
    return None


def _load_plugin_package():
    """Register the located plugin directory as ``_rtl_plugin`` and return
    its ``classFactory`` - or ``None`` if the modules could not be found at
    all (every test that needs them will then fail with a clear
    ModuleNotFoundError of its own, rather than this failing silently)."""
    if PLUGIN_PACKAGE_NAME in sys.modules:
        return sys.modules[PLUGIN_PACKAGE_NAME].classFactory

    plugin_dir = _locate_plugin_dir()
    if plugin_dir is None:
        return None

    spec = importlib.util.spec_from_file_location(
        PLUGIN_PACKAGE_NAME,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_PACKAGE_NAME] = package
    spec.loader.exec_module(package)
    return package.classFactory


classFactory = _load_plugin_package()

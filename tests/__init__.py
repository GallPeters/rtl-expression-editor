# -*- coding: utf-8 -*-
"""
Test suite for the RTL Expression Editor plugin.

Lives at the repository root, alongside ``src/`` rather than inside it - it
is developer/QA tooling, not part of what gets installed as the QGIS plugin
(only ``src/`` is packaged into a release). ``src`` is imported as an
ordinary Python package by adding the repository root to ``sys.path`` below,
so the plugin's own relative imports between its modules
(``rtl_editor.py``'s ``from .rtl_settings import ...`` and similar) resolve
exactly as they do when QGIS itself imports the installed plugin package.

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

    From the QGIS Python Console, with the repository checked out somewhere
    on disk::

        import sys
        sys.path.insert(0, r"<path to the repository root>")
        from tests import run_all
        run_all.main()

    From a shell, with QGIS's own Python interpreter, run from the
    repository root so the relative "tests" package resolves::

        python3 -m tests.run_all

    The plugin's own Settings dialog also has a "Run Tests" button that does
    the above automatically when the repository's ``tests/`` folder is found
    as a sibling of the running plugin's own directory (true for a
    development checkout; not the case for a normal end-user install, which
    only ships ``src/``'s contents - the button reports that plainly rather
    than failing silently).

Nothing here is run automatically when the plugin loads - these are
developer/QA tests, run on demand, not a startup check.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qgis.testing import start_app

# Idempotent: safe even if something else already started the QGIS
# application in this process (e.g. running inside the QGIS Python Console).
start_app()

# -*- coding: utf-8 -*-
"""
Test suite for the RTL Expression Editor plugin.

Exercises the plugin's own logic against a real QGIS/PyQt environment -
``qgis.testing`` (started once here, before any test module runs) rather than
mocks, since almost everything this plugin does is reacting to real Qt
widgets and real QgsExpression/QgsVectorLayer behaviour. Nothing here talks to
a network, a website, or any source outside the installed QGIS itself: every
function name, variable and value tested comes from the live
``QgsExpression``/``QgsExpressionContextUtils``/``QgsVectorLayer`` APIs, the
same ones the plugin itself calls at runtime - so the coverage tracks whatever
QGIS version is actually installed, automatically.

How to run, after installing the plugin into the QGIS profile's
``python/plugins/<plugin folder>`` directory:

    From the QGIS Python Console::

        import unittest
        from <plugin folder>.test import run_all
        run_all.main()

    From a shell, with QGIS's own Python interpreter (adjust the plugin
    folder name to match how it was installed)::

        python3 -m unittest discover -s <plugin folder>/test -v

Nothing here is run automatically when the plugin loads - these are
developer/QA tests, run on demand, not a startup check.
"""

from qgis.testing import start_app

# Idempotent: safe even if something else already started the QGIS
# application in this process (e.g. running inside the QGIS Python Console).
start_app()

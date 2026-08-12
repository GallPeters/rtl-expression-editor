# -*- coding: utf-8 -*-
"""Convenience entry point: run every test in this package in one call.

From the QGIS Python Console, after installing the plugin::

    from <plugin folder>.test import run_all
    run_all.main()

From a shell, using QGIS's own Python interpreter, run as a module so the
relative imports inside the test package resolve correctly::

    python3 -m <plugin folder>.test.run_all

(``<plugin folder>`` is whatever name the plugin was installed under, e.g.
``rtl_bidi_editor`` - see rtl_settings.SETTINGS_PREFIX / this package's own
``__init__.py`` for why that name is unrelated to any individual module's
filename.)
"""

import sys
import unittest


def main(verbosity: int = 2) -> unittest.TestResult:
    """Discover and run every ``test_*.py`` module in this package."""
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=__package__, top_level_dir=None, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=verbosity)
    return runner.run(suite)


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result.wasSuccessful() else 1)

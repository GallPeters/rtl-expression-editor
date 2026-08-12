# -*- coding: utf-8 -*-
"""Convenience entry point: run every test in this package in one call.

From the QGIS Python Console, with the repository checked out somewhere on
disk::

    import sys
    sys.path.insert(0, r"<path to the repository root>")
    from tests import run_all
    run_all.main()

From a shell, using QGIS's own Python interpreter, run from the repository
root so the relative imports inside the package resolve correctly::

    python3 -m tests.run_all

The plugin's own Settings dialog has a "Run Tests" button that does the
above automatically - see rtl_settings.SettingsDialog._run_tests() - when a
``tests/`` folder is found as a sibling of the running plugin's own
directory (true for a development checkout; not the case for a normal
end-user install, which only ships ``src/``'s contents).
"""

import sys
import unittest
from pathlib import Path


def main(verbosity: int = 2, stream=None) -> unittest.TestResult:
    """Discover and run every ``test_*.py`` module in this package.

    ``start_dir``/``top_level_dir`` are passed as real filesystem paths
    rather than left for unittest to infer from ``__package__``/``sys.argv``.
    That inference only reliably works when this is launched with
    ``python -m tests.run_all``; called as a plain function - exactly what
    both the documented Python Console usage and the Settings dialog's "Run
    Tests" button do - it silently imported each ``test_*.py`` as a bare
    top-level module instead of ``tests.test_*``, which broke every module
    using ``from .utils import ...`` with "attempted relative import with no
    known parent package". Explicit paths make this work the same way
    regardless of how ``main()`` itself was reached.

    Wrapped in a snapshot/restore of the active QgsProject (see
    ``_project_guard``), so running this from inside a live QGIS session
    never leaves the user's own project layers or read-mode choices altered,
    regardless of what any individual test does or whether it raises.
    """
    from ._project_guard import guarded_run

    tests_dir = Path(__file__).resolve().parent
    repo_root = tests_dir.parent

    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=str(tests_dir), top_level_dir=str(repo_root), pattern="test_*.py"
    )
    runner = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=verbosity)
    return guarded_run(lambda: runner.run(suite))


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result.wasSuccessful() else 1)

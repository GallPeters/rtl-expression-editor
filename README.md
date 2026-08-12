# RTL Expression Editor

Fix right-to-left (Hebrew, Arabic, and more) text rendering in QGIS expressions and filters.

**Without** RTL Expression Editor:

<kbd>
<img width="566" height="452" alt="image" src="https://github.com/user-attachments/assets/e2a4cdf5-660b-4d6f-b513-570d6029dcab" />
</kbd>

**With** RTL Expression Editor:

<kbd>
<img width="566" height="449" alt="image" src="https://github.com/user-attachments/assets/48b54cee-95a3-4763-ad69-dce87587e5df" />
</kbd>



## What it does

QGIS's built-in editor doesn't render RTL and mixed text correctly. This plugin opens a companion editor window that does — and keeps both in sync. Edit in either window; the other updates automatically.

## Supported dialogs

- Expression Builder
- Layer Filter / Query Builder
- Other dialogs with a QGIS code editor may also work

## Installation

1. Install from the QGIS Plugin Repository.
2. Restart QGIS.
3. Enable **RTL Companion Editor** in Plugin Manager.

### Installing from source instead

Everything QGIS needs to load the plugin lives in `src/` - `__init__.py`,
`metadata.txt`, `LICENSE`, `icon.png` and every `rtl_*.py` module, all as
direct siblings of each other. That's the important part: QGIS's plugin
loader requires `__init__.py` to sit next to `rtl_editor.py` and friends, not
one directory above them.

So install by copying (or zipping) `src/`'s **contents** - not the `src`
folder itself - into a folder named however you like inside your QGIS
profile's plugin directory, e.g.:

```
<QGIS profile>/python/plugins/rtl_expression_editor/__init__.py
<QGIS profile>/python/plugins/rtl_expression_editor/metadata.txt
<QGIS profile>/python/plugins/rtl_expression_editor/rtl_editor.py
...
```

Copying the repository root (or `src/` as a nested subfolder) instead
produces `ModuleNotFoundError: No module named
'<plugin folder>.rtl_editor'` - `__init__.py`'s `from .rtl_editor import ...`
only resolves when the two are direct siblings. The `tests/` folder is a
sibling of `src/` on purpose, not part of it: it ships with the repository
for development, never with a packaged release. `releases/` holds the actual
`src/`-contents-only zips built for past versions, as a reference for the
expected layout.

## Usage

1. Open a supported dialog (e.g. `Layer → Filter / Query Builder`).
2. A floating RTL editor window appears automatically.
3. Edit text in either window — they stay synchronized.
4. Use the original QGIS dialog buttons to apply your result.

## Running the tests

The `tests/` folder at the repository root - a sibling of `src/`, not part of
it - covers the editor and its sync with the core editor, autocomplete (with
and without a configured lookup table), the function helper, read mode,
occurrence highlighting, the replace-bar shortcuts and the plugin's own
load/unload cycle. It ships with the repository, not with a packaged release
(only `src/`'s contents are packaged), and is never run automatically -
these are developer/QA tests, run on demand.

From the QGIS Python Console, with the repository checked out somewhere on
disk:

```python
import sys
sys.path.insert(0, r"<path to the repository root>")
from tests import run_all
run_all.main()
```

From a shell, using QGIS's own Python interpreter (the exact launcher name
depends on the platform/QGIS build, e.g. `python-qgis-qt6.bat` on Windows,
`python3` inside `qgis --code` on Linux), run from the repository root:

```
python3 -m tests.run_all
```

If the QGIS profile's plugin points straight at this repository's `src/`
folder (a common way to develop against a live QGIS, e.g. via a symlink),
the plugin's own Settings dialog also gets a **Run Tests** button under a
"Developer" section that does the same thing and shows a pass/fail summary
plus the full log. It only appears when `tests/` is found as a sibling of
the running plugin's own directory - never for a normal end-user install.

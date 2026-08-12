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

## Usage

1. Open a supported dialog (e.g. `Layer → Filter / Query Builder`).
2. A floating RTL editor window appears automatically.
3. Edit text in either window — they stay synchronized.
4. Use the original QGIS dialog buttons to apply your result.

## Running the tests

The `test/` folder ships with the plugin and covers the editor and its sync
with the core editor, autocomplete (with and without a configured lookup
table), the function helper, read mode, occurrence highlighting, the
replace-bar shortcuts and the plugin's own load/unload cycle. It is not run
automatically - these are developer/QA tests, run on demand.

From the QGIS Python Console, after installing the plugin:

```python
from rtl_bidi_editor.test import run_all
run_all.main()
```

From a shell, using QGIS's own Python interpreter (the exact launcher name
depends on the platform/QGIS build, e.g. `python-qgis-qt6.bat` on Windows,
`python3` inside `qgis --code` on Linux):

```
python3 -m rtl_bidi_editor.test.run_all
```

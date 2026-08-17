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
only resolves when the two are direct siblings. `releases/` holds the actual
`src/`-contents-only zips built for past versions, as a reference for the
expected layout.

#### Installing with the tests too

The `tests/` folder at the repository root is a sibling of `src/`, not part
of it, precisely so a normal install (above) never pulls it in. To install
*with* it instead - so the Settings dialog's **Run Tests** button shows up -
copy `tests/` in as well, nested **inside** the same plugin folder, alongside
the modules it just tested:

```
<QGIS profile>/python/plugins/rtl_expression_editor/__init__.py
<QGIS profile>/python/plugins/rtl_expression_editor/rtl_editor.py
...
<QGIS profile>/python/plugins/rtl_expression_editor/tests/__init__.py
<QGIS profile>/python/plugins/rtl_expression_editor/tests/run_all.py
...
```

The tests locate the plugin's modules by looking for `rtl_editor.py` next to
them rather than assuming any particular folder name, so this works
regardless of what the plugin folder itself is called.

#### Sharing a pre-configured autocomplete source

The Settings dialog's **Export Settings** / **Import Settings** buttons save
and load the whole configuration - including the autocomplete lookup layer
and its column mapping - as a JSON file. To hand a ready-made setup to
colleagues:

1. Configure the plugin normally, pointing the lookup layer at a data file
   copied *inside* the plugin's own install folder (e.g.
   `<plugin folder>/data/lookup.gpkg`).
2. Click **Export Settings**. When the lookup layer's file lives inside that
   same folder, the exported file also records its path *relative* to it,
   not the absolute path on your own machine.
3. Zip up the plugin folder, with the data file and the exported JSON file
   both included inside it.
4. A colleague installs the zip, then opens **Settings** and clicks
   **Import Settings**, picking the JSON file bundled inside the plugin
   folder. Its lookup layer is located and loaded automatically from the
   relative path recorded in the file - reused if a matching layer is
   already in the project, otherwise loaded fresh - wherever their profile
   happens to install the plugin.

If the layer cannot be resolved, every other setting is still applied and
the reason is reported clearly: **not found** (no file exists at the
recorded path - it was not actually bundled, or ended up somewhere else) or
**not accessible** (a file was found but could not be opened - permissions
or an unsupported format - or a database connection failed, most often
because credentials are missing: usernames and passwords are deliberately
never written into an exported file, so each colleague still needs their
own valid login to that database).

A lookup layer that lives outside the plugin folder (a database connection,
or a file elsewhere on disk) is still exported for convenience, but only as
an absolute path - portable only back to the same machine, not to a
colleague's.

## Usage

1. Open a supported dialog (e.g. `Layer → Filter / Query Builder`).
2. A floating RTL editor window appears automatically.
3. Edit text in either window — they stay synchronized.
4. Use the original QGIS dialog buttons to apply your result.

## Running the tests

The `tests/` folder covers the editor and its sync with the core editor,
autocomplete (with and without a configured lookup table), the function
helper, read mode, occurrence highlighting, the replace-bar shortcuts and the
plugin's own load/unload cycle. It's never run automatically - these are
developer/QA tests, run on demand - and only ships when you choose to include
it (see "Installing with the tests too" above); a normal packaged release is
just `src/`'s contents.

`tests/` locates the plugin's own modules by looking for `rtl_editor.py`
next to it rather than assuming a folder name, so running it works the same
way in either place it can legitimately be:

* at the repository root, a sibling of `src/` (this repository's own
  development layout);
* nested inside an installed plugin folder, alongside `rtl_editor.py` (an
  "installed with tests" copy).

From the QGIS Python Console, with either of those somewhere on disk:

```python
import sys
sys.path.insert(0, r"<path to the folder CONTAINING tests/>")
from tests import run_all
run_all.main()
```

From a shell, using QGIS's own Python interpreter (the exact launcher name
depends on the platform/QGIS build, e.g. `python-qgis-qt6.bat` on Windows,
`python3` inside `qgis --code` on Linux), run from that same folder:

```
python3 -m tests.run_all
```

The plugin's own Settings dialog also gets a **Run Tests** button under a
"Developer" section whenever `tests/` is found either nested inside the
running plugin's own directory or as a sibling of it one level up - covering
both layouts above automatically - and shows a pass/fail summary plus the
full log. It's simply absent for a normal end-user install, which has no
`tests/` anywhere nearby.

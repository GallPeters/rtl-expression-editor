# RTL Companion Editor

This QGIS plugin works around QScintilla's incomplete right-to-left rendering
by opening a synchronized native Qt editor next to QGIS dialogs that use
QScintilla, especially:

- Expression Builder
- Layer Filter / Query Builder

The original QGIS dialog is not modified, hidden, replaced or subclassed.

## Installation

Copy the whole `rtl_editor` folder into your QGIS plugins directory.

Typical locations:

- Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
- Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
- macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`

Then enable the plugin in QGIS Plugin Manager.

## Usage

The plugin starts automatically.

When you open a supported QGIS dialog, a floating companion window appears.

- Edit text in the companion window for correct RTL/BiDi rendering.
- Changes are synchronized to the original QScintilla editor.
- Changes made in the original editor are synchronized back to the companion.
- Apply writes to the original editor and triggers normal QGIS update signals.
- OK applies and closes the companion, leaving the original dialog open.
- Cancel restores the baseline text and closes the companion.

You can disable or re-enable monitoring from:

Plugins -> RTL Editor -> RTL Companion Editor

## Architecture

Components:

- `plugin.py`
  - QGIS plugin lifecycle.
  - Adds the menu action.
  - Starts and stops the monitor.

- `monitor.py`
  - Detects newly opened dialogs.
  - Uses limited top-level polling plus event filters on the QGIS main window
    and candidate dialogs.
  - Creates one controller per detected dialog.

- `analyzer.py`
  - Determines whether a window is a supported dialog.
  - Finds the embedded QScintilla editor or QGIS code-editor wrapper.
  - Scores candidates robustly.

- `controller.py`
  - Owns one companion editor and one original editor.
  - Performs bidirectional synchronization.
  - Implements Apply, OK, Cancel, cleanup and window lifecycle behavior.

- `companion_dialog.py`
  - Floating Qt dialog containing a `QPlainTextEdit`.
  - Handles geometry persistence and button semantics.

- `utils.py`
  - Safe Qt introspection helpers.
  - Logging helpers.
  - Deleted-object protection.

## Synchronization strategy

### Original -> companion

Preferred mechanism:

- Connect to `textChanged()` on the QGIS code-editor wrapper or the raw
  `QsciScintilla` widget.

Fallback mechanism:

- If no usable signal exists, install an event filter on the original editor
  and check the text after relevant interaction events:
  - key release
  - input method
  - mouse button release
  - drop
  - focus out

Changes are coalesced with a zero-interval `QTimer`.

### Companion -> original

The companion `QPlainTextEdit` emits `textChanged()`.

The controller:

1. Stores the pending text.
2. Coalesces rapid changes with a zero-interval timer.
3. Writes to the original editor.

When writing to QScintilla, the plugin prefers:

```python
selectAll()
replaceSelectedText(new_text)

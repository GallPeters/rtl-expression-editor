# RTL / BiDi Code Editor for QGIS 4 (Qt6)

Makes the **Expression Builder** and the **Layer Filter / Query Builder** usable
with Hebrew, Arabic and other right-to-left scripts, without patching or
rebuilding QGIS.

---

## The problem

QGIS renders those editors with **QScintilla**. Scintilla's text layout engine
does not implement the Unicode bidirectional algorithm, so a mixed
Hebrew/English expression such as

```
"שם_רחוב" LIKE '%הרצל%' AND intersects($geometry, @atlas_geometry)
```

is drawn with overlapping glyphs, and the caret lands in the wrong visual
position. The *stored* expression is always correct — only the rendering is
broken. That is precisely why an overlay fixes the problem completely.

## The fix

Whenever a `QgsCodeEditorExpression` or `QgsCodeEditorSQL` becomes visible, the
plugin creates a `QPlainTextEdit` **as a child of that editor**, sized to cover
it exactly. Qt's text engine (HarfBuzz + the Unicode BiDi algorithm) renders
mixed-direction text correctly. The two editors are kept in strict two-way
synchronisation, so QGIS itself is unaware anything changed.

---

## Installation

**Option A — copy the folder**

1. Copy the whole `rtl_bidi_editor` directory into your QGIS profile plugins
   folder:

   | OS | Path |
   |---|---|
   | Linux | `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/` |
   | Windows | `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\` |
   | macOS | `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/` |

   You can find it from QGIS: *Settings → User Profiles → Open Active Profile Folder*.
2. Restart QGIS (or use the *Plugin Reloader* plugin).
3. *Plugins → Manage and Install Plugins → Installed* → tick **RTL / BiDi Code Editor**.

**Option B — ZIP**

```bash
cd <parent-of-rtl_bidi_editor>
zip -r rtl_bidi_editor.zip rtl_bidi_editor
```

Then *Plugins → Manage and Install Plugins → Install from ZIP*.

There is **no** menu entry, toolbar button or settings dialog by design — the
requirement is that the user never enables or disables anything. Open the
Expression Builder and start typing.

**Requirements:** QGIS 4.0+ (Qt6). No external Python packages.
It also runs unchanged on QGIS 3.28+ / Qt5, since nothing Qt6-exclusive is used.

---

## Architecture

```
QApplication
   └── event filter ──► CodeEditorWatcher      (detects editors on QEvent.Show)
                              │  attach()
                              ▼
        QgsCodeEditorExpression / QgsCodeEditorSQL   (kept alive, functional)
                              │  child widget, geometry = parent.rect()
                              ▼
                        RtlOverlayEditor  (QPlainTextEdit)
                              ├── BidiSyntaxHighlighter
                              └── QCompleter
```

Four classes in one module (~600 lines):

| Class | Responsibility |
|---|---|
| `RtlBidiEditorPlugin` | Plugin lifecycle; installs/removes the watcher. |
| `CodeEditorWatcher` | One application-wide event filter; detects targeted editors and attaches overlays. |
| `RtlOverlayEditor` | Geometry, appearance mirroring, two-way sync, completion. |
| `BidiSyntaxHighlighter` | Expression / SQL highlighting using QGIS's own colour scheme. |

### Key design decisions

**1. Detect the editor class, not the dialog class.**
The Expression Builder and the Query Builder share no dialog base class, but
both editors derive from `QgsCodeEditor`. Keying on the editor class avoids
fragile `objectName` lookups (`txtExpressionString`, `mTxtSql` are private
implementation details) and automatically covers the Field Calculator, the
rule-based renderer and labeling expression dialogs, the virtual-layer dialog,
DB Manager's SQL window, and any future QGIS dialog. Matching walks the MRO by
class *name*, so downstream subclasses work too.

**2. The overlay is a child of the editor it covers.**
This is the single most important implementation choice. Child geometry lives
in the parent's coordinate system, so moving the dialog, resizing it, dragging
a splitter, changing DPI, switching monitors and re-running the layout are all
handled by Qt. The only thing the plugin reacts to is the parent's `Resize`
event — `setGeometry(sci.rect())`. No timers, no polling, no global coordinate
maths, and no possibility of drift. A floating top-level overlay would have
required tracking screen coordinates and would still have drifted.

**3. Overlay, never replace or hide.**
The C++ side of QGIS holds pointers to the editor and connects signals to it.
Removing it from the layout risks dangling pointers; *hiding* it would drop it
from the layout and collapse the very rectangle we need to cover. Keeping it
alive, functional and simply covered is both safer and simpler.

**4. Focus proxy.**
`sci.setFocusProxy(overlay)` — when QGIS calls `setFocus()` on the editor, Qt
forwards it to the overlay, so keystrokes always reach the visible widget. The
proxy is cleared on detach; a dangling proxy pointer would crash Qt.

**5. One re-entrancy guard for both sync directions.**
A single `_syncing` boolean is what prevents infinite update loops.

* **Overlay → Scintilla:** push the full text with `setText()` (this emits
  Scintilla's `textChanged`, which is what drives QGIS validation, the preview
  pane and the OK button state), then push the caret and selection. The caret
  push is essential: every "insert field / function / operator / value" action
  inserts at *Scintilla's* caret.
* **Scintilla → Overlay:** replace the text inside a single `QTextCursor` edit
  block rather than `setPlainText()`, so the overlay's undo history survives,
  then read the caret back from Scintilla. This one path transparently covers
  *every* insertion mechanism QGIS has — present or future — because all of
  them ultimately mutate the Scintilla document and emit `textChanged`.

**6. Character vs. byte offsets.**
Scintilla addresses text as UTF-8 byte offsets, which would corrupt caret
positions in Hebrew and Arabic (2 bytes per character). The plugin uses
`getCursorPosition()` / `setCursorPosition()` / `getSelection()` /
`setSelection()` exclusively; QScintilla's Qt layer converts between characters
and bytes internally via `SCI_POSITIONAFTER`, so the plugin never does byte
arithmetic.

**7. `QPlainTextEdit`, not `QTextEdit`.**
The content is plain text: cheaper document layout, no rich-text paste
surprises, and identical BiDi support (both sit on `QTextDocument` /
`QTextLayout`).

### Performance

* One event filter on `QApplication`. Its first statement is an integer
  comparison on the event type; only `Show` events proceed. Cost is
  unmeasurable.
* Zero timers, zero polling.
* One `QPlainTextEdit` per open dialog, destroyed with it.
* Highlighting is per-block and only runs on changed blocks.

---

## Requirement coverage

| # | Requirement | Status | Notes |
|---|---|---|---|
| 1 | Automatic activation / deactivation | ✅ Full | `QEvent.Show` filter attaches; the overlay is a child, so Qt destroys it with the dialog. Also scans already-open dialogs at load. |
| 2 | Minimal UI (editor area only) | ✅ Full | Bare `QPlainTextEdit`. Frame style, font, tab width and the QGIS code-editor palette are copied from the original. |
| 3 | Perfect overlay (resize / move / DPI / monitor) | ✅ Full | Child widget + `setGeometry(parent.rect())`. All cases delegated to Qt's parent/child geometry propagation. |
| 4 | Two-way synchronisation | ✅ Full | Text, caret and selection in both directions, guarded against loops. |
| 4a | Undo/redo consistency | 🟡 Partial | The *visible* undo stack is the overlay's (Ctrl+Z works normally, and text edits are pushed inside `beginEditBlock`). Scintilla's own hidden undo stack is reset on each push. QGIS exposes no undo UI in these dialogs, so this is invisible to users. Merging the two stacks is not possible without Scintilla-level delta tracking. |
| 5 | Every insertion mechanism (operators, functions, fields, variables, values, tree double-click, future ones) | ✅ Full | Handled generically via Scintilla's `textChanged` + caret read-back. No per-control code, so new QGIS insertion paths are covered automatically. |
| 6 | Editing workflow / correct stored expression | ✅ Full | QGIS keeps reading the expression from its own editor, which is always up to date. |
| 7 | Syntax highlighting | ✅ Full | `QSyntaxHighlighter` for keywords, functions, numbers, strings, quoted field names, `@`/`$` variables, `--` and `/* */` comments; separate SQL keyword set. Colours come from the user's active `QgsCodeEditorColorScheme`, so light/dark themes match. |
| — | Autocomplete | 🟡 Partial | QScintilla's own popup is driven by Scintilla's internal typing pipeline and cannot be reused from an external widget. Replaced by a native `QCompleter` seeded with all `QgsExpression` function names plus the layer's field names (discovered via an ancestor exposing `layer()`). Field completion is therefore available in the Expression Builder but not in the plain Query Builder, which exposes no layer accessor. |
| — | Scintilla-specific extras (brace matching, call tips, code folding, margin markers, error underlining inside the editor) | ❌ Not carried over | These are Scintilla features with no Qt equivalent. The Expression Builder's own error label and preview pane, which live outside the editor, keep working normally. |
| — | Qt6 / PyQGIS only / no C++ / no deps | ✅ Full | Pure PyQGIS; stdlib `re` only. Runs on Qt5 too. |
| — | No polling | ✅ Full | Signals and events exclusively. |
| — | Robust error handling | ✅ Full | Every QGIS/Qt touchpoint is guarded; failures degrade (no highlighting, no completion) rather than breaking editing, and are reported to the QGIS message log under `RTL BiDi Editor`. |

### Known limitations and assumptions

* **Assumption:** QGIS reads the final expression from the Scintilla widget it
  owns. It does — which is why mirroring text into it is sufficient and no
  QGIS-internal API is monkey-patched.
* **Assumption:** the targeted editors are direct, layout-managed children of
  their dialogs and are never reparented while visible. True in current QGIS.
* If QGIS ever renames `QgsCodeEditorExpression` / `QgsCodeEditorSQL`, add the
  new name to `TARGET_EDITOR_CLASSES` at the top of `rtl_bidi_editor.py` — a
  one-line fix, and the plugin fails safe (does nothing) until then.
* Scintilla's caret is invisible to the user; on rare programmatic
  select-and-replace operations the selection *direction* (anchor before or
  after the caret) is not preserved, because `QsciScintilla.setSelection()`
  has no anchor-direction parameter. The selected range itself is always exact.
* Very large SQL statements (hundreds of KB) would make the full-text push
  noticeable. Expressions and filters are orders of magnitude smaller, so a
  delta-based sync was not worth the complexity.

---

## Troubleshooting

* **Nothing seems to change:** check *View → Panels → Log Messages → RTL BiDi
  Editor*.
* **Testing changes:** install the *Plugin Reloader* plugin. Reloading is safe —
  `unload()` detaches every overlay and clears focus proxies, and re-attachment
  is idempotent (guarded by the overlay's object name).

## License

GPL-2.0-or-later, matching QGIS.

# RTL Expression Editor

Correct right-to-left (Hebrew, Arabic) text in QGIS expression and filter editors, with smart autocomplete from your own lookup tables.

**Without** RTL Expression Editor:

<kbd>
<img width="566" height="452" alt="image" src="https://github.com/user-attachments/assets/e2a4cdf5-660b-4d6f-b513-570d6029dcab" />
</kbd>

**With** RTL Expression Editor:

<kbd>
<img width="566" height="449" alt="image" src="https://github.com/user-attachments/assets/48b54cee-95a3-4763-ad69-dce87587e5df" />
</kbd>

## What it does

- **Correct RTL text.** QGIS's built-in editor cannot display mixed Hebrew/Arabic and English text properly. The plugin places an editor that can on top of the original one and keeps the two in sync. All QGIS tools keep working: field and function lists, validation, preview and syntax highlighting.
- **Autocomplete.** Replaces QGIS's own suggestion list. It offers fields, values, functions, variables (including `@map_scale`) and operators, with help text and a function argument helper.
- **Your own values.** Optionally, use a lookup table so that values come with descriptions.
- **Read mode.** Shows those descriptions in place of the stored codes. The expression itself is never changed.

Works in the Expression Builder, Field Calculator, Select by Expression and Layer Filter (Query Builder).

## Installation

1. Install **RTL Expression Editor** from the QGIS Plugin Repository (*Plugins → Manage and Install Plugins*).
2. Open any expression or filter dialog. The editor appears automatically.

Settings are under *Plugins → RTL Text Editor → Settings…*.

## Autocomplete

| Type             | Shows                                  |
| ---------------- | -------------------------------------- |
| `"`              | field names                            |
| `'` after a field | values of that field                  |
| `@`              | variables                              |
| a word           | fields, functions, variables, operators |

Press **Ctrl+Space** to open the list at any time (**Ctrl+J** also works if your keyboard layout takes Ctrl+Space). Use the arrow keys to move through the list, **Enter** or **Tab** to accept and **Esc** to close.

Without a lookup table, values come from the layer's own data. The number of values is set in Settings.

## Using a lookup table

A lookup table is any table QGIS can open (GeoPackage, CSV, PostGIS…) with one row per allowed value. Choose it in **Settings → Custom Autocomplete → Browse…**. It is read directly from its source and never added to your project.

Example, for a `roads` layer with `type` and `subtype` fields:

| table | field  | value | description | group |
| ----- | ------ | ----- | ----------- | ----- |
| roads | type   | 1     | Highway     |       |
| roads | type   | 2     | Street      |       |
| roads | subtype | 11   | Toll road   | 1     |
| roads | subtype | 21   | Main street | 2     |

Map its columns in Settings:

- **Field names column** and **Values column**: required.
- **Value description column**: the label shown next to each value and in Read mode.
- **Table names column**: limits rows to the layer being edited (matched by its source table name).
- **Group codes / Group descriptions columns**: group the suggestions. They also let Read mode choose the right description when one code has different meanings in different groups. For example, in `"type" = 1 AND "subtype" = 11` the subtype is read in group `1`.
- **Alternative value description**, **Table description** and **Field description** columns are optional extra labels.

## Read mode

With value descriptions configured, a switch appears in the editor's bottom-left corner:

- **Edit**: the normal expression.
- **Read**: codes are replaced by their descriptions (read-only).
- **Alternative**: the alternative descriptions, if configured.

## Sharing settings

**Export Settings…** saves the whole configuration, including the lookup table, to a JSON file. **Import Settings…** loads it.

To share a ready-made setup, put the lookup file inside the plugin folder before exporting. Its path is then stored relative to the plugin folder and works on any machine. Database passwords are never exported, so each user needs their own login.

## Installing from source

Copy the **contents** of `src/` (not the folder itself) into a folder in your QGIS profile's plugin directory, e.g. `<profile>/python/plugins/rtl_expression_editor/`. `releases/` holds ready-made zips.

## Running the tests

Copy `tests/` next to the plugin files (or keep the repository layout) and run, with QGIS's Python:

```
python -m tests.run_all
```

When `tests/` is present, Settings also shows a **Run Tests** button.

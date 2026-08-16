# -*- coding: utf-8 -*-
"""
Settings storage and settings dialog for the RTL / BiDi editor plugin.

This module is **purely additive**.  It knows nothing about the overlay editor
or the Scintilla synchronisation mechanism; the editor module only ever reads
booleans and field names from ``Settings``.  Deleting this file would leave the
RTL editor fully functional (the editor module imports it defensively).

Two pieces live here:

``Settings``
    A thin, typed facade over ``QgsSettings``.  All keys live under one prefix
    so they are easy to find in the QGIS settings tree and easy to remove.

``SettingsDialog``
    Built in code rather than from a ``.ui`` file, to keep the plugin's file
    count low, consistent with the existing architecture.  It uses the native
    QGIS widgets ``QgsMapLayerComboBox`` and ``QgsFieldComboBox``, which give
    dynamic field population and layer tracking for free.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsProject, QgsSettings

# Native QGIS selector widgets.  Imported defensively so that a binding change
# degrades to a clear error message instead of breaking plugin load.
try:
    from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
except ImportError:  # pragma: no cover
    QgsFieldComboBox = None
    QgsMapLayerComboBox = None


#: All keys live under this prefix inside QgsSettings.
SETTINGS_PREFIX = "plugins/rtl_bidi_editor/"

#: Top-level marker key in an exported settings file, so a JSON file that
#: happens to parse but is not actually one of ours is rejected with a clear
#: message instead of being silently misapplied - see Settings.apply_dict().
CONFIG_MARKER = "rtl_expression_editor_settings"

#: Bumped only if the exported shape changes in a way older code could not
#: read correctly. apply_dict() does not reject a different version outright
#: - every field is read with a default, so a file from an older (or newer,
#: forward-compatible) plugin version still applies whatever it recognises.
CONFIG_FORMAT_VERSION = 1


class SettingsImportError(Exception):
    """The file as a whole could not be applied - wrong format, or not
    parseable at all.

    Reserved for problems that make the *entire* file untrustworthy. A
    configured lookup layer that cannot actually be found is deliberately
    NOT one of these: it is reported as a soft warning instead (see
    Settings.apply_dict()), so the rest of a mostly-good file still gets
    applied rather than being thrown away over one missing layer.
    """


def _log(message: str) -> None:
    """Report a settings storage problem.

    Settings failures used to be swallowed, which made a read error
    indistinguishable from "the user has not configured anything yet". They are
    now always reported.
    """
    try:
        from qgis.core import Qgis, QgsMessageLog

        QgsMessageLog.logMessage(message, "RTL Expression Editor", Qgis.MessageLevel.Warning)
    except Exception:
        pass


class _SettingsBus(QObject):
    """Broadcasts "settings were saved" to whoever cares.

    The autocomplete cache listens to this to drop stale data, and the plugin
    listens to it to enable/disable the watcher live.  A signal keeps those
    consumers decoupled from the dialog.
    """

    changed = pyqtSignal()


#: Module-level singleton. Consumers connect to ``BUS.changed``.
BUS = _SettingsBus()


class Settings:
    """Typed accessors for the plugin's persisted settings.

    Static methods rather than an instance, because there is exactly one
    settings store and no state worth carrying around.
    """

    # -- general ----------------------------------------------------------- #

    @staticmethod
    def _raw(key: str, default=None):
        """Read a value with no type coercion.

        Deliberately does **not** pass ``type=`` to ``QgsSettings.value()``.
        That signature is ``value(key, defaultValue, type, section)`` and
        supplying ``type`` as a keyword raises ``TypeError`` on several PyQGIS
        builds.  Combined with a silent ``except``, that made every read return
        its default while writes succeeded - settings appeared to save but never
        came back.  Reading raw and coercing in Python avoids the whole issue.

        Failures are logged rather than swallowed, so a storage problem is
        visible instead of masquerading as "not configured".
        """
        try:
            return QgsSettings().value(SETTINGS_PREFIX + key, default)
        except Exception as exc:
            _log(f"settings read failed for '{key}': {exc}")
            return default

    @classmethod
    def _get_bool(cls, key: str, default: bool) -> bool:
        """Coerce a stored value to bool.

        QSettings round-trips booleans through the ini file as the strings
        ``'true'``/``'false'``, so a raw read can return either a real bool or
        text depending on whether the value has been through a save/reload
        cycle.  Both are handled.
        """
        raw = cls._raw(key, None)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "yes", "on")

    @classmethod
    def _get_str(cls, key: str, default: str = "") -> str:
        raw = cls._raw(key, None)
        if raw is None:
            return default
        try:
            return str(raw)
        except Exception:
            return default

    @staticmethod
    def _set(key: str, value) -> None:
        """Write a value, then verify it round-trips.

        Booleans are stored as ``'true'``/``'false'`` explicitly so the stored
        representation is identical whether it was just written or reloaded from
        disk, which keeps ``_get_bool`` simple.
        """
        stored = "true" if value is True else "false" if value is False else value
        try:
            settings = QgsSettings()
            settings.setValue(SETTINGS_PREFIX + key, stored)
            settings.sync()
            check = settings.value(SETTINGS_PREFIX + key, None)
            if check is None:
                _log(f"settings write for '{key}' did not persist")
        except Exception as exc:
            _log(f"settings write failed for '{key}': {exc}")

    @classmethod
    def dump(cls) -> None:
        """Print the raw stored values.  Call from the Python Console.

        Use this to distinguish "never saved" from "saved but unreadable":

            from rtl_bidi_editor.rtl_settings import Settings; Settings.dump()
        """
        print("=" * 68)
        print("RTL Expression Editor - raw stored settings")
        print("=" * 68)
        try:
            settings = QgsSettings()
            print(f"ini file : {settings.fileName()}")
        except Exception as exc:
            print(f"could not open QgsSettings: {exc}")
            return
        keys = [
            "enabled",
            "ac/enabled",
            "ac/layer_id",
            "ac/default_read_mode",
        ] + list(cls.FIELD_KEYS.values())
        for key in keys:
            full = SETTINGS_PREFIX + key
            try:
                raw = settings.value(full, None)
                print(f"  {key:28s} raw={raw!r}  type={type(raw).__name__}")
            except Exception as exc:
                print(f"  {key:28s} READ ERROR: {exc}")
        print("-" * 68)
        print(f"plugin_enabled()       -> {cls.plugin_enabled()}")
        print(f"autocomplete_enabled() -> {cls.autocomplete_enabled()}")
        print(f"autocomplete_layer()   -> {cls.autocomplete_layer()}")
        print(f"autocomplete_is_usable -> {cls.autocomplete_is_usable()}")
        print("=" * 68)

    @classmethod
    def plugin_enabled(cls) -> bool:
        """Master switch.  Defaults to True so existing installs are unchanged."""
        return cls._get_bool("enabled", True)

    @classmethod
    def set_plugin_enabled(cls, value: bool) -> None:
        cls._set("enabled", bool(value))

    # -- custom autocomplete ---------------------------------------------- #

    @classmethod
    def autocomplete_enabled(cls) -> bool:
        """Off by default: an opt-in feature must never change behaviour."""
        return cls._get_bool("ac/enabled", False)

    @classmethod
    def set_autocomplete_enabled(cls, value: bool) -> None:
        cls._set("ac/enabled", bool(value))

    @classmethod
    def layer_id(cls) -> str:
        return cls._get_str("ac/layer_id", "")

    @classmethod
    def set_layer_id(cls, value: str) -> None:
        cls._set("ac/layer_id", value or "")

    #: Logical name -> settings key for every field selector.
    FIELD_KEYS = {
        "table": "ac/field_table",
        "field_names": "ac/field_names",
        "value": "ac/field_value",
        "description": "ac/field_description",
        "group_code": "ac/field_group_code",
        "group_description": "ac/field_group_description",
    }

    @classmethod
    def field(cls, which: str) -> str:
        return cls._get_str(cls.FIELD_KEYS[which], "")

    @classmethod
    def set_field(cls, which: str, value: str) -> None:
        cls._set(cls.FIELD_KEYS[which], value or "")

    # -- convenience ------------------------------------------------------- #

    @classmethod
    def max_suggested_values(cls) -> int:
        """Cap on value suggestions read straight from the current layer.

        Only used when the field being completed has no configured lookup
        table (or the lookup returns nothing): the popup then shows the first
        N distinct, non-null values already present in the layer. The custom
        lookup table has no such cap - QUERY_LIMIT in rtl_autocomplete already
        bounds it generously.
        """
        raw = cls._get_str("ac/max_values", "")
        try:
            value = int(raw)
            return value if value > 0 else 10
        except (TypeError, ValueError):
            return 10

    @classmethod
    def set_max_suggested_values(cls, value: int) -> None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 10
        cls._set("ac/max_values", str(max(1, value)))

    @classmethod
    def default_read_mode(cls) -> bool:
        """Whether the editor opens in description (read-only) mode.

        Defaults to False - edit mode - so the editor behaves exactly as before
        for anyone who does not opt in.
        """
        return cls._get_bool("ac/default_read_mode", False)

    @classmethod
    def set_default_read_mode(cls, value: bool) -> None:
        cls._set("ac/default_read_mode", bool(value))

    @classmethod
    def autocomplete_layer(cls):
        """Resolve the configured layer, or None if it is gone from the project.

        Returning None on a deleted layer is the graceful-degradation path: the
        feature simply does nothing rather than raising.
        """
        layer_id = cls.layer_id()
        if not layer_id:
            return None
        try:
            return QgsProject.instance().mapLayer(layer_id)
        except Exception:
            return None

    @classmethod
    def autocomplete_is_usable(cls) -> tuple:
        """Return ``(usable, reason)`` for the current configuration."""
        if not cls.autocomplete_enabled():
            return False, "custom autocomplete is disabled"
        layer = cls.autocomplete_layer()
        if layer is None:
            return False, "configured autocomplete layer is missing from the project"
        try:
            available = {f.name() for f in layer.fields()}
        except Exception:
            return False, "autocomplete layer has no readable fields"
        for required in ("field_names", "value"):
            name = cls.field(required)
            if not name:
                return False, f"required field '{required}' is not configured"
            if name not in available:
                return False, f"field '{name}' no longer exists in the layer"
        return True, ""

    # -- export / import ---------------------------------------------------- #

    @classmethod
    def export_dict(cls) -> dict:
        """Everything needed to reproduce this configuration elsewhere.

        The autocomplete lookup layer is described by its *source*, never by
        its QGIS layer id: an id is only meaningful inside the project it was
        assigned in, so it could never be resolved in a colleague's project.
        When the source file lives inside this plugin's own install
        directory (or a subfolder of it), its path is additionally recorded
        relative to that directory - see ``_describe_layer_source()`` - so
        the same exported file keeps working after the plugin is reinstalled
        somewhere else entirely, as long as the data file travels with it.
        """
        data = {
            CONFIG_MARKER: True,
            "format_version": CONFIG_FORMAT_VERSION,
            "plugin_enabled": cls.plugin_enabled(),
            "max_suggested_values": cls.max_suggested_values(),
            "default_read_mode": cls.default_read_mode(),
            "autocomplete_enabled": cls.autocomplete_enabled(),
            "autocomplete_fields": {key: cls.field(key) for key in cls.FIELD_KEYS},
            "autocomplete_layer": None,
        }
        layer = cls.autocomplete_layer()
        if layer is not None:
            data["autocomplete_layer"] = _describe_layer_source(layer)
        return data

    @classmethod
    def apply_dict(cls, data: dict, plugin_dir: Optional[Path] = None) -> List[str]:
        """Validate then apply an exported configuration. Returns warnings.

        Raises ``SettingsImportError`` for a file that cannot be trusted as a
        whole (wrong format, not even a dict). Nothing is written until every
        value has been read out of ``data`` successfully, so a file that
        fails validation never leaves a half-applied configuration behind.

        A lookup layer that cannot be found or loaded is a *soft* failure:
        every other setting is still applied, and the layer is left cleanly
        unconfigured (never pointing at a stale, nonexistent id) with the
        problem reported back through the returned list of warnings.
        """
        if not isinstance(data, dict) or not data.get(CONFIG_MARKER):
            raise SettingsImportError(
                "This file is not an RTL Expression Editor settings export."
            )

        warnings: List[str] = []

        plugin_enabled = bool(data.get("plugin_enabled", True))
        default_read_mode = bool(data.get("default_read_mode", False))
        ac_enabled = bool(data.get("autocomplete_enabled", False))

        try:
            max_values = int(data.get("max_suggested_values", 10))
        except (TypeError, ValueError):
            max_values = 10
            warnings.append("'max_suggested_values' was invalid; kept at 10.")

        fields = data.get("autocomplete_fields")
        if not isinstance(fields, dict):
            fields = {}
            if data.get("autocomplete_fields") is not None:
                warnings.append("'autocomplete_fields' was malformed; ignored.")

        layer_id = ""
        layer_info = data.get("autocomplete_layer")
        if ac_enabled and layer_info:
            layer_id, layer_warning = _resolve_layer_from_description(layer_info, plugin_dir)
            if layer_warning:
                warnings.append(layer_warning)

        # Only now, with the whole file read without raising, is anything
        # actually written.
        cls.set_plugin_enabled(plugin_enabled)
        cls.set_max_suggested_values(max_values)
        cls.set_default_read_mode(default_read_mode)
        cls.set_autocomplete_enabled(ac_enabled)
        for key in cls.FIELD_KEYS:
            cls.set_field(key, str(fields.get(key, "") or ""))
        cls.set_layer_id(layer_id)

        BUS.changed.emit()
        return warnings


def _describe_layer_source(layer) -> dict:
    """Capture a layer's source portably, for ``Settings.export_dict()``.

    File-based sources (Shapefile, GeoPackage, CSV, ...) get their path
    recorded twice: as an absolute path (works if the importing machine
    happens to have the same layout), and - when the file lives inside this
    plugin's own directory - relative to that directory, which is what makes
    "bundle the data file inside the plugin zip" actually work regardless of
    where the plugin ends up installed. A database connection or an in-memory
    layer has no such file to point at; only the raw values are recorded, on
    a best-effort basis, since there is nothing portable to make relative.
    """
    info: Dict[str, Optional[str]] = {
        "name": "",
        "provider": "",
        "path_relative_to_plugin": None,
        "path_absolute": None,
        "uri_suffix": "",
    }
    try:
        info["name"] = layer.name()
    except Exception:
        pass
    try:
        info["provider"] = layer.providerType()
    except Exception:
        pass
    try:
        source = layer.source() or ""
    except Exception:
        source = ""

    # A GDAL/OGR source may carry extra "|layername=..." style clauses after
    # the file path itself - kept as-is and reattached on import, so e.g. a
    # specific layer inside a multi-layer GeoPackage is preserved.
    path_part, sep, suffix = source.partition("|")
    info["uri_suffix"] = (sep + suffix) if sep else ""
    path_part = path_part.strip()

    # A real filesystem path never contains these - a memory layer's source
    # is a query string ("Point?crs=...&field=...") and a database URI is
    # "key=value ..." pairs, neither of which is anything to make relative.
    looks_like_path = bool(path_part) and not any(ch in path_part for ch in "?=&")
    if not looks_like_path:
        info["path_absolute"] = source or None
        return info

    try:
        path = Path(path_part).resolve()
    except Exception:
        info["path_absolute"] = path_part
        return info

    info["path_absolute"] = str(path)
    try:
        plugin_dir = Path(__file__).resolve().parent
        info["path_relative_to_plugin"] = path.relative_to(plugin_dir).as_posix()
    except ValueError:
        pass  # outside the plugin directory - only the absolute path applies
    return info


def _resolve_layer_from_description(info, plugin_dir: Optional[Path] = None) -> Tuple[str, str]:
    """Return ``(layer_id, warning)`` for an exported layer description.

    ``layer_id`` is ``""`` whenever nothing could be resolved - never a
    dangling id pointing at a layer that does not exist, which is what keeps
    a failed import from leaving the plugin's autocomplete pointed at
    nothing in a way that looks configured but silently is not.

    Resolution order: reuse an already-loaded layer with a matching source
    first (so importing the same file twice, or onto a project that already
    has the layer, never adds a duplicate); otherwise load it fresh, trying
    the plugin-relative path before the absolute one, since a plugin-relative
    path is the one still valid after the plugin is reinstalled somewhere
    else - the absolute path mainly helps re-importing on the same machine.
    """
    if not isinstance(info, dict):
        return "", "The autocomplete layer description was malformed; not configured."

    name = str(info.get("name") or "lookup table")
    provider = str(info.get("provider") or "ogr")
    relative = info.get("path_relative_to_plugin")
    absolute = info.get("path_absolute")
    suffix = str(info.get("uri_suffix") or "")

    base_dir = plugin_dir or Path(__file__).resolve().parent
    candidates: List[Path] = []
    if relative:
        try:
            candidates.append((base_dir / relative).resolve())
        except Exception:
            pass
    if absolute:
        try:
            candidates.append(Path(absolute).resolve())
        except Exception:
            pass

    if not candidates:
        return (
            "",
            f"Autocomplete layer '{name}' has no usable path recorded (it was likely a "
            "database or in-memory layer, which cannot be bundled); please configure it "
            "manually.",
        )

    project = QgsProject.instance()

    # 1. An already-loaded layer with the same source file wins outright.
    for path in candidates:
        for existing in project.mapLayers().values():
            try:
                existing_source = (existing.source() or "").partition("|")[0].strip()
                if existing_source and Path(existing_source).resolve() == path:
                    return existing.id(), ""
            except Exception:
                continue

    # 2. Load fresh from whichever candidate actually exists on disk.
    for path in candidates:
        try:
            if not path.exists():
                continue
        except Exception:
            continue
        try:
            from qgis.core import QgsVectorLayer

            layer = QgsVectorLayer(str(path) + suffix, name, provider)
            if layer.isValid():
                project.addMapLayer(layer)
                return layer.id(), ""
        except Exception:
            continue

    tried = ", ".join(str(p) for p in candidates)
    return (
        "",
        f"Autocomplete layer '{name}' could not be found (tried: {tried}). "
        "Autocomplete is left unconfigured until it is reselected in Settings.",
    )


class SettingsDialog(QDialog):
    """Plugins -> RTL Text Editor -> Settings.

    Layout mirrors the feature spec: a General group with the master switch,
    then a Custom Autocomplete group whose configuration block is only enabled
    when the feature is switched on.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("RTL Text Editor - Settings")
        self.setMinimumWidth(480)

        root = QVBoxLayout(self)

        # -- General ------------------------------------------------------- #
        general = QGroupBox("General", self)
        general_layout = QVBoxLayout(general)
        self.chk_enabled = QCheckBox("Enable RTL Text Editor plugin", general)
        self.chk_enabled.setToolTip(
            "When unchecked the plugin stays installed but inactive: no overlay "
            "editor is created and QGIS behaves exactly as without the plugin."
        )
        general_layout.addWidget(self.chk_enabled)

        values_row = QFormLayout()
        self.spin_max_values = QSpinBox(general)
        self.spin_max_values.setRange(1, 1000)
        self.spin_max_values.setToolTip(
            "When Ctrl+Space suggests values for a field that has no "
            "configured lookup table (or the lookup has nothing for it), "
            "show this many of the field's own distinct, non-null values."
        )
        values_row.addRow("Suggested values without a lookup table", self.spin_max_values)
        general_layout.addLayout(values_row)

        root.addWidget(general)

        # -- Custom Autocomplete ------------------------------------------- #
        ac_group = QGroupBox("Custom Autocomplete", self)
        ac_layout = QVBoxLayout(ac_group)

        self.chk_ac = QCheckBox("Enable custom autocomplete source", ac_group)
        self.chk_ac.setToolTip(
            "Look up allowed values from a project layer and offer them with "
            "Ctrl+Space (and automatically while typing) in an expression or "
            "filter.\n\n"
            "This is entirely optional: fields, functions, variables and "
            "operators are always suggested regardless of this setting. "
            "Turning it on only adds a lookup table's own values and "
            "descriptions on top of that."
        )
        ac_layout.addWidget(self.chk_ac)

        self.config = QWidget(ac_group)
        form = QFormLayout(self.config)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        if QgsMapLayerComboBox is None or QgsFieldComboBox is None:
            form.addRow(
                QLabel(
                    "QGIS selector widgets are unavailable in this build; "
                    "custom autocomplete cannot be configured."
                )
            )
            self.cmb_layer = None
            self.field_combos = {}
        else:
            self.cmb_layer = QgsMapLayerComboBox(self.config)
            self._apply_vector_filter(self.cmb_layer)
            self.cmb_layer.setAllowEmptyLayer(True)
            self.cmb_layer.setToolTip(
                "The project layer or table that defines what Ctrl+Space "
                "suggests. Each row describes one (field, value) pair - e.g. "
                "a row with 'STATUS' / '1' / 'Active' makes the editor offer "
                "1 (Active) when completing the STATUS field.\n\n"
                "This is a normal vector layer, added to the project like any "
                "other (e.g. a CSV or GeoPackage table with no geometry). "
                "Leave it empty to suggest only the fields and values already "
                "present in the layer being edited, with no lookup table at "
                "all.\n\n"
                "Use 'Export Settings' below to save this configuration - "
                "including a portable reference to this layer's file - so it "
                "can be shared with colleagues."
            )
            heading = QLabel("<b>Required</b>", self.config)
            form.addRow(heading)
            form.addRow("Lookup layer", self.cmb_layer)

            # (logical name, label, optional?)
            # Required first, then optional. Labels say "column" rather than
            # "field" because every one of these selects a COLUMN of the lookup
            # layer - "Fields Names Field Name" was ambiguous about which of the
            # two it meant. The logical keys are unchanged, so settings saved by
            # earlier versions still load.
            spec = [
                ("field_names", "Field names column", False),
                ("value", "Values column", False),
                (None, None, None),  # separator: Optional
                ("description", "Descriptions column", True),
                ("table", "Table names column", True),
                ("group_code", "Group codes column", True),
                ("group_description", "Group descriptions column", True),
            ]
            self.field_combos = {}
            for key, label, optional in spec:
                if key is None:
                    form.addRow(QLabel("<b>Optional</b>", self.config))
                    continue
                combo = QgsFieldComboBox(self.config)
                combo.setAllowEmptyFieldName(True)
                self.field_combos[key] = combo
                form.addRow(label, combo)

            self.field_combos["field_names"].setToolTip(
                "The column that names WHICH field each row's value belongs "
                "to, e.g. a row containing 'STATUS' here matches completions "
                "for the STATUS field.\n\n"
                "One row per (field, value) pair, so the same field name "
                "repeats across as many rows as it has values - e.g. three "
                "rows all naming 'STATUS' for its three possible values."
            )
            self.field_combos["value"].setToolTip(
                "The column holding the value inserted into the expression "
                "when a suggestion is picked, e.g. 1, 2, 'IL', 'US'.\n\n"
                "Inserted exactly as stored - if the field needs a quoted "
                "text literal, store it already quoted here, e.g. 'IL' "
                "rather than IL, so the expression stays valid."
            )
            self.field_combos["table"].setToolTip(
                "The column naming WHICH layer/table each row applies to, so "
                "the same field name (e.g. STATUS) can mean something "
                "different in two different layers.\n\n"
                "Compare against the layer's actual data source name (e.g. "
                "'countries' for a GeoPackage layer named 'countries'), not "
                "necessarily its display name in the Layers panel - use the "
                "'RTL autocomplete - diagnostic' report (Ctrl+Shift+D while "
                "editing) if unsure what a layer's source name is.\n\n"
                "Leave empty to offer these values regardless of which layer "
                "is being edited."
            )
            self.field_combos["group_code"].setToolTip(
                "The column used to group suggestions under a heading, e.g. "
                "all STATUS values sharing group_code 'G1' are shown "
                "together under one heading.\n\n"
                "Used together with 'Group descriptions column' below - with "
                "only one of the two set, suggestions are shown ungrouped."
            )
            self.field_combos["group_description"].setToolTip(
                "The column shown beside each group heading, e.g. group_code "
                "'G1' with group_description 'Life cycle' shows the heading "
                "'G1 (Life cycle)' above every row sharing that code."
            )
            self.field_combos["description"].setToolTip(
                "The column shown beside each value in the suggestion list, "
                "e.g. value '1' with description 'Active' is offered as "
                "'1 (Active)'.\n\n"
                "Only the value itself - never the description - is ever "
                "inserted into the expression. Leave empty to show plain "
                "values with no description."
            )

            self.cmb_layer.layerChanged.connect(self._on_layer_changed)

        self.cmb_mode = QComboBox(self.config)
        self.cmb_mode.addItem("Edit mode (show codes)", False)
        self.cmb_mode.addItem("Read mode (show descriptions)", True)
        self.cmb_mode.setToolTip(
            "Mode the editor starts in. The in-editor switch changes it at any "
            "time; the saved expression always keeps the original codes."
        )
        form.addRow("Default editor mode", self.cmb_mode)

        ac_layout.addWidget(self.config)

        self.lbl_warning = QLabel("", ac_group)
        self.lbl_warning.setWordWrap(True)
        self.lbl_warning.setStyleSheet("color: #b7791f;")
        self.lbl_warning.setVisible(False)
        ac_layout.addWidget(self.lbl_warning)

        root.addWidget(ac_group)

        # -- Import / Export ------------------------------------------------ #
        io_group = QGroupBox("Import / Export Settings", self)
        io_layout = QVBoxLayout(io_group)
        io_label = QLabel(
            "Save this configuration to a file, or load one prepared by "
            "someone else - useful for distributing a ready-made "
            "autocomplete source to colleagues.",
            io_group,
        )
        io_label.setWordWrap(True)
        io_layout.addWidget(io_label)
        io_buttons_row = QHBoxLayout()
        self.btn_export = QPushButton("Export Settings...", io_group)
        self.btn_export.setToolTip(
            "Save every setting on this dialog - including the autocomplete "
            "lookup layer and its column mapping - to a JSON file.\n\n"
            "If the lookup layer's file lives inside this plugin's own "
            "install folder, the file it is saved to also records a path "
            "relative to that folder. Distribute the exported file together "
            "with that data file (e.g. both copied into the plugin's folder "
            "before zipping it up) and 'Import Settings' will locate it "
            "automatically on another machine, wherever the plugin ends up "
            "installed."
        )
        self.btn_export.clicked.connect(self._export_settings)
        self.btn_import = QPushButton("Import Settings...", io_group)
        self.btn_import.setToolTip(
            "Load a configuration previously saved with 'Export Settings'.\n\n"
            "If its autocomplete lookup layer was bundled next to the "
            "plugin, it is located and loaded automatically - no need to "
            "pick it again. If it cannot be found, every other setting is "
            "still applied and you are told exactly what to reconfigure by "
            "hand."
        )
        self.btn_import.clicked.connect(self._import_settings)
        io_buttons_row.addWidget(self.btn_export)
        io_buttons_row.addWidget(self.btn_import)
        io_layout.addLayout(io_buttons_row)
        root.addWidget(io_group)

        # -- Developer -------------------------------------------------------
        # Only shown at all when a development checkout's tests/ folder is
        # found next to the running plugin - a normal end-user install only
        # ships src/'s contents, with no such sibling, so this stays entirely
        # invisible there rather than offering a button that could only fail.
        tests_dir = self._tests_directory()
        if tests_dir is not None:
            dev_group = QGroupBox("Developer", self)
            dev_layout = QVBoxLayout(dev_group)
            self.btn_run_tests = QPushButton("Run Tests", dev_group)
            self.btn_run_tests.setToolTip(f"Run the test suite in:\n{tests_dir}")
            self.btn_run_tests.clicked.connect(self._run_tests)
            dev_layout.addWidget(self.btn_run_tests)
            root.addWidget(dev_group)

        root.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.chk_ac.toggled.connect(self.config.setEnabled)
        self.chk_enabled.toggled.connect(ac_group.setEnabled)

        self._load()

    # ------------------------------------------------------------------ #
    # Import / Export
    # ------------------------------------------------------------------ #

    def _dict_from_dialog(self) -> dict:
        """Build an exportable dict straight from the widgets on screen.

        Deliberately does NOT go through ``Settings.set_*()``/``export_dict()``:
        Export should reflect whatever is currently shown, including changes
        not yet confirmed with OK, without the side effect of persisting them
        - so clicking Export and then Cancelling this dialog still cancels,
        exactly as it always has.
        """
        if self.cmb_layer is not None:
            layer = self.cmb_layer.currentLayer()
            fields = {key: combo.currentField() for key, combo in self.field_combos.items()}
        else:
            layer = Settings.autocomplete_layer()
            fields = {key: Settings.field(key) for key in Settings.FIELD_KEYS}
        try:
            default_read_mode = bool(self.cmb_mode.currentData())
        except Exception:
            default_read_mode = Settings.default_read_mode()
        return {
            CONFIG_MARKER: True,
            "format_version": CONFIG_FORMAT_VERSION,
            "plugin_enabled": self.chk_enabled.isChecked(),
            "max_suggested_values": self.spin_max_values.value(),
            "default_read_mode": default_read_mode,
            "autocomplete_enabled": self.chk_ac.isChecked(),
            "autocomplete_fields": fields,
            "autocomplete_layer": _describe_layer_source(layer) if layer is not None else None,
        }

    def _export_settings(self) -> None:
        """Save everything currently shown in this dialog to a JSON file."""
        start_dir = str(Path(__file__).resolve().parent)
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export Settings",
            str(Path(start_dir) / "rtl_expression_editor_settings.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            data = self._dict_from_dialog()
            Path(path).write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Export Settings", f"Could not write the file:\n{exc}")
            return

        note = ""
        layer_info = data.get("autocomplete_layer")
        if layer_info and layer_info.get("path_relative_to_plugin"):
            note = (
                "\n\nTo share this with a bundled lookup layer, copy the data "
                "file into the plugin folder alongside this export before "
                "zipping it up - the relative path recorded inside it will "
                "then resolve correctly after installation, wherever that "
                "turns out to be."
            )
        elif layer_info:
            note = (
                "\n\nNote: the lookup layer's file is outside this plugin's "
                "own folder, so only its absolute path was recorded - this "
                "will only resolve automatically on a machine with the same "
                "file layout. Move the data file inside the plugin folder "
                "and re-export to make it fully portable."
            )
        QMessageBox.information(self, "Export Settings", f"Settings exported to:\n{path}{note}")

    def _import_settings(self) -> None:
        """Load and apply a configuration exported by ``_export_settings``."""
        start_dir = str(Path(__file__).resolve().parent)
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import Settings", start_dir, "JSON files (*.json);;All files (*)"
        )
        if not path:
            return

        try:
            raw = Path(path).read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as exc:
            QMessageBox.warning(self, "Import Settings", f"Could not read the file:\n{exc}")
            return

        try:
            warnings = Settings.apply_dict(data, plugin_dir=Path(__file__).resolve().parent)
        except SettingsImportError as exc:
            QMessageBox.warning(self, "Import Settings", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "Import Settings", f"Could not apply the settings:\n{exc}")
            return

        self._load()  # refresh every widget from what was just applied

        if warnings:
            QMessageBox.warning(
                self,
                "Import Settings",
                "Settings were imported, but with some issues:\n\n"
                + "\n".join(f"- {w}" for w in warnings),
            )
        else:
            QMessageBox.information(self, "Import Settings", "Settings imported successfully.")

    # ------------------------------------------------------------------ #
    # Developer: run the test suite
    # ------------------------------------------------------------------ #

    @staticmethod
    def _tests_directory() -> Optional[Path]:
        """The tests/ folder, if one is findable next to this install.

        Checked in order:

        * nested inside this same plugin folder, e.g. ``tests/`` copied in
          alongside ``rtl_editor.py`` - the natural result of installing the
          plugin *with* its tests, not just a development checkout;
        * a sibling of ``src/`` one level up - the git-checkout layout. This
          file lives at ``<repo>/src/rtl_settings.py`` there, or the same
          resolved path when a development QGIS profile symlinks its
          plugins folder straight to ``<repo>/src``.

        A normal end-user install that only ships ``src/``'s contents, with
        no ``tests/`` anywhere nearby, matches neither and returns ``None``.
        """
        here = Path(__file__).resolve().parent
        for candidate in (here / "tests", here.parent / "tests"):
            if (candidate / "run_all.py").is_file():
                return candidate
        return None

    def _run_tests(self) -> None:
        """Run the test suite and show a pass/fail summary plus the full log."""
        tests_dir = self._tests_directory()
        if tests_dir is None:
            QMessageBox.information(
                self,
                "Run Tests",
                "No tests/ folder found next to the plugin - this is only "
                "available when running from a development checkout.",
            )
            return

        self.btn_run_tests.setEnabled(False)
        self.btn_run_tests.setText("Running...")
        QApplication.processEvents()  # show the label change before the run blocks

        result = None
        log_text = ""
        try:
            import io
            import sys

            repo_root = str(tests_dir.parent)
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)

            from tests import run_all  # only importable in a dev checkout

            buffer = io.StringIO()
            result = run_all.main(verbosity=2, stream=buffer)
            log_text = buffer.getvalue()
        except Exception as exc:
            # The full traceback, not just str(exc): a bare exception message
            # like "_qgis_app_init_qgis() takes 0 positional arguments but 1
            # was given" gives no clue which call in the chain actually
            # raised it, or from which module - exactly the information
            # needed to diagnose an environment-specific failure (this one
            # only reproduces inside an already-running QGIS session, never
            # in a standalone interpreter).
            import traceback

            log_text = f"Could not run the test suite:\n{traceback.format_exc()}"
            _log(f"Run Tests failed: {exc}")
        finally:
            self.btn_run_tests.setEnabled(True)
            self.btn_run_tests.setText("Run Tests")

        self._show_test_results(result, log_text)

    def _show_test_results(self, result, log_text: str) -> None:
        """A small dialog: a coloured pass/fail summary, then the full log."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Test Results")
        dialog.resize(760, 500)
        layout = QVBoxLayout(dialog)

        if result is None:
            summary, color = "Could not run the test suite - see the log below.", "#b7791f"
        elif result.wasSuccessful():
            summary, color = f"All {result.testsRun} tests passed.", "#2e7d32"
        else:
            failed = len(result.failures) + len(result.errors)
            summary = f"{failed} of {result.testsRun} tests failed - see the log below."
            color = "#c0392b"

        lbl_summary = QLabel(summary, dialog)
        lbl_summary.setStyleSheet(f"font-weight: bold; color: {color};")
        layout.addWidget(lbl_summary)

        log_view = QPlainTextEdit(dialog)
        log_view.setReadOnly(True)
        log_view.setPlainText(log_text)
        log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        log_view.setFont(font)
        layout.addWidget(log_view)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.clicked.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.exec()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _apply_vector_filter(combo) -> None:
        """Restrict the layer combo to vector layers across QGIS versions.

        The filter enum moved between QGIS releases, so try the modern location
        first and fall back rather than hard-failing.
        """
        try:
            from qgis.core import Qgis as _Qgis

            combo.setFilters(_Qgis.LayerFilter.VectorLayer)
            return
        except Exception:
            pass
        try:
            from qgis.core import QgsMapLayerProxyModel

            combo.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        except Exception:
            pass  # unfiltered is acceptable

    def _on_layer_changed(self, layer) -> None:
        """Repopulate every field selector from the newly chosen layer."""
        for combo in self.field_combos.values():
            try:
                combo.setLayer(layer)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Load / save
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        self.chk_enabled.setChecked(Settings.plugin_enabled())
        self.spin_max_values.setValue(Settings.max_suggested_values())
        self.chk_ac.setChecked(Settings.autocomplete_enabled())
        try:
            index = self.cmb_mode.findData(Settings.default_read_mode())
            self.cmb_mode.setCurrentIndex(max(0, index))
        except Exception:
            pass
        self.config.setEnabled(self.chk_ac.isChecked())

        if self.cmb_layer is None:
            return

        missing: List[str] = []
        layer = Settings.autocomplete_layer()
        if layer is not None:
            self.cmb_layer.setLayer(layer)
            self._on_layer_changed(layer)
            try:
                available = {f.name() for f in layer.fields()}
            except Exception:
                available = set()
            for key, combo in self.field_combos.items():
                saved = Settings.field(key)
                if not saved:
                    continue
                if saved in available:
                    combo.setField(saved)
                else:
                    # Field was deleted from the layer since we saved it.
                    missing.append(f"{key} -> '{saved}'")
        elif Settings.layer_id():
            missing.append("the configured layer is no longer in this project")

        if missing:
            self.lbl_warning.setText(
                "Some saved settings could not be restored: "
                + "; ".join(missing)
                + ". Please reselect them."
            )
            self.lbl_warning.setVisible(True)

    def _on_accept(self) -> None:
        """Validate, then persist and broadcast."""
        if self.chk_ac.isChecked() and self.cmb_layer is not None:
            layer = self.cmb_layer.currentLayer()
            if layer is None:
                self._complain("Select the layer that holds the autocomplete definitions.")
                return
            for key, label in (
                ("field_names", "Field names column"),
                ("value", "Values column"),
            ):
                if not self.field_combos[key].currentField():
                    self._complain(f"'{label}' is required.")
                    return
            # Grouping only makes sense with both halves configured; warn rather
            # than block, since a partial choice is harmless (grouping is simply
            # not applied).
            has_code = bool(self.field_combos["group_code"].currentField())
            has_desc = bool(self.field_combos["group_description"].currentField())
            if has_code != has_desc:
                QMessageBox.information(
                    self,
                    "Grouping incomplete",
                    "Grouping uses both the group codes column and the group "
                    "descriptions column. With only one set, results are shown "
                    "ungrouped.",
                )

        self._save()
        BUS.changed.emit()
        self.accept()

    def _complain(self, message: str) -> None:
        QMessageBox.warning(self, "Incomplete settings", message)

    def _save(self) -> None:
        Settings.set_plugin_enabled(self.chk_enabled.isChecked())
        Settings.set_max_suggested_values(self.spin_max_values.value())
        Settings.set_autocomplete_enabled(self.chk_ac.isChecked())
        try:
            Settings.set_default_read_mode(bool(self.cmb_mode.currentData()))
        except Exception:
            pass
        if self.cmb_layer is None:
            return
        layer = self.cmb_layer.currentLayer()
        Settings.set_layer_id(layer.id() if layer is not None else "")
        for key, combo in self.field_combos.items():
            Settings.set_field(key, combo.currentField())

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

from pathlib import Path
from typing import List, Optional

from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
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
            "Ctrl+Space while editing an expression or filter."
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
                "Column listing the field names you want suggestions for, "
                "e.g. NAME, COUNTRY, STATUS."
            )
            self.field_combos["value"].setToolTip(
                "Column holding the values inserted into the expression."
            )
            self.field_combos["table"].setToolTip(
                "Column holding the source table name. Leave empty to offer "
                "values regardless of which layer is being edited."
            )
            self.field_combos["group_code"].setToolTip(
                "Column used to group the suggestions."
            )
            self.field_combos["group_description"].setToolTip(
                "Column shown beside each group heading."
            )
            self.field_combos["description"].setToolTip(
                "Shown beside each value, e.g. 'IL (Israel)'. Only the value is "
                "ever inserted into the expression."
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
    # Developer: run the test suite
    # ------------------------------------------------------------------ #

    @staticmethod
    def _tests_directory() -> Optional[Path]:
        """The repository's tests/ folder, if this is a development checkout.

        This file lives at ``<repo>/src/rtl_settings.py`` when running from a
        git checkout - a common way to develop against a live QGIS is a
        symlink from the QGIS profile's plugins folder straight to
        ``<repo>/src``. A normal end-user install only ships ``src/``'s
        contents, with no such sibling, so this returns ``None`` there.
        """
        candidate = Path(__file__).resolve().parent.parent / "tests"
        return candidate if (candidate / "run_all.py").is_file() else None

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
            log_text = f"Could not run the test suite:\n{exc}"
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

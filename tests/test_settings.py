# -*- coding: utf-8 -*-
"""Settings storage: every getter/setter round-trips through QgsSettings."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qgis.core import QgsProject

from _rtl_plugin.rtl_settings import Settings, SettingsImportError


class SettingsRoundTripTests(unittest.TestCase):
    def setUp(self):
        # QgsSettings persists across tests (and across QGIS sessions) - snapshot
        # and restore, so this suite never leaks into the user's real config.
        self._enabled = Settings.plugin_enabled()
        self._ac_enabled = Settings.autocomplete_enabled()
        self._max_values = Settings.max_suggested_values()
        self._default_read_mode = Settings.default_read_mode()
        self._layer_id = Settings.layer_id()
        self._fields = {key: Settings.field(key) for key in Settings.FIELD_KEYS}

    def tearDown(self):
        Settings.set_plugin_enabled(self._enabled)
        Settings.set_autocomplete_enabled(self._ac_enabled)
        Settings.set_max_suggested_values(self._max_values)
        Settings.set_default_read_mode(self._default_read_mode)
        Settings.set_layer_id(self._layer_id)
        for key, value in self._fields.items():
            Settings.set_field(key, value)

    def test_plugin_enabled_round_trip(self):
        Settings.set_plugin_enabled(False)
        self.assertFalse(Settings.plugin_enabled())
        Settings.set_plugin_enabled(True)
        self.assertTrue(Settings.plugin_enabled())

    def test_autocomplete_enabled_round_trip(self):
        Settings.set_autocomplete_enabled(True)
        self.assertTrue(Settings.autocomplete_enabled())
        Settings.set_autocomplete_enabled(False)
        self.assertFalse(Settings.autocomplete_enabled())

    def test_default_read_mode_round_trip(self):
        Settings.set_default_read_mode(True)
        self.assertTrue(Settings.default_read_mode())
        Settings.set_default_read_mode(False)
        self.assertFalse(Settings.default_read_mode())

    def test_field_round_trip_for_every_key(self):
        for key in Settings.FIELD_KEYS:
            Settings.set_field(key, f"COLUMN_{key.upper()}")
            self.assertEqual(Settings.field(key), f"COLUMN_{key.upper()}")

    def test_max_suggested_values_defaults_to_a_positive_number(self):
        Settings.set_max_suggested_values(0)  # invalid input
        self.assertGreaterEqual(Settings.max_suggested_values(), 1)

    def test_max_suggested_values_round_trip(self):
        Settings.set_max_suggested_values(25)
        self.assertEqual(Settings.max_suggested_values(), 25)

    def test_autocomplete_is_usable_false_with_a_reason_when_disabled(self):
        Settings.set_autocomplete_enabled(False)
        usable, reason = Settings.autocomplete_is_usable()
        self.assertFalse(usable)
        self.assertTrue(reason)

    def test_autocomplete_layer_is_none_when_unconfigured(self):
        Settings.set_layer_id("")
        self.assertIsNone(Settings.autocomplete_layer())


class RunTestsButtonTests(unittest.TestCase):
    """The Settings dialog's "Run Tests" button - meaningful whenever a
    tests/ folder is findable next to the running plugin, which is true for
    this repository's own dev-checkout layout (a sibling of src/) and is
    exactly what makes it possible to test here."""

    def test_tests_directory_finds_the_real_tests_folder(self):
        from _rtl_plugin.rtl_settings import SettingsDialog

        tests_dir = SettingsDialog._tests_directory()
        self.assertIsNotNone(tests_dir)
        self.assertTrue((tests_dir / "run_all.py").is_file())

    def test_tests_directory_prefers_a_tests_folder_nested_in_the_plugin_itself(self):
        """Simulates "installed with tests": a tests/ folder copied directly
        inside the plugin's own folder, alongside rtl_settings.py - the
        layout that answers "which files do I copy to install with tests".
        """
        import tempfile
        from pathlib import Path
        from unittest import mock

        from _rtl_plugin import rtl_settings as settings_module
        from _rtl_plugin.rtl_settings import SettingsDialog

        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "rtl_expression_editor"
            plugin_dir.mkdir()
            (plugin_dir / "rtl_settings.py").write_text("", encoding="utf-8")
            nested_tests = plugin_dir / "tests"
            nested_tests.mkdir()
            (nested_tests / "run_all.py").write_text("", encoding="utf-8")

            fake_file = str(plugin_dir / "rtl_settings.py")
            with mock.patch.object(settings_module, "__file__", fake_file):
                found = SettingsDialog._tests_directory()

        self.assertEqual(found, nested_tests)

    def test_button_is_created_when_tests_directory_is_found(self):
        from _rtl_plugin.rtl_settings import SettingsDialog

        dialog = SettingsDialog()
        try:
            self.assertTrue(hasattr(dialog, "btn_run_tests"))
            self.assertEqual(dialog.btn_run_tests.text(), "Run Tests")
        finally:
            dialog.deleteLater()

    def test_run_tests_passes_the_result_through_and_re_enables_the_button(self):
        """Exercises _run_tests()'s own control flow - disable/run/re-enable,
        handing the result to _show_test_results() - without paying for a
        real, recursive run of the whole suite via a mocked run_all.main().
        """
        from unittest import mock

        from _rtl_plugin.rtl_settings import SettingsDialog

        dialog = SettingsDialog()
        fake_result = mock.Mock(wasSuccessful=lambda: True, testsRun=3, failures=[], errors=[])
        try:
            with mock.patch("tests.run_all.main", return_value=fake_result) as mocked_main, mock.patch.object(
                SettingsDialog, "_show_test_results"
            ) as mocked_show:
                dialog._run_tests()

            mocked_main.assert_called_once()
            mocked_show.assert_called_once()
            self.assertIs(mocked_show.call_args[0][0], fake_result)
            self.assertTrue(dialog.btn_run_tests.isEnabled())
            self.assertEqual(dialog.btn_run_tests.text(), "Run Tests")
        finally:
            dialog.deleteLater()


class _FakeLayer:
    """Stands in for a QgsVectorLayer wherever only name()/providerType()/
    source() are read - see _describe_layer_source() - so path handling can
    be tested without building a real data source."""

    def __init__(self, name: str, provider: str, source: str):
        self._name = name
        self._provider = provider
        self._source = source

    def name(self):
        return self._name

    def providerType(self):
        return self._provider

    def source(self):
        return self._source


_GEOJSON_SAMPLE = json.dumps(
    {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0, 0]},
                "properties": {"field_name": "STATUS", "value": "1"},
            }
        ],
    }
)


class DescribeLayerSourceTests(unittest.TestCase):
    """_describe_layer_source() - how a layer's source is captured for
    export, and specifically whether a plugin-relative path is recorded."""

    def test_a_file_inside_the_plugin_directory_gets_a_relative_path(self):
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            data_dir = plugin_dir / "data"
            data_dir.mkdir()
            data_file = data_dir / "lookup.gpkg"
            data_file.write_text("stub", encoding="utf-8")

            layer = _FakeLayer("lookup", "ogr", str(data_file) + "|layername=lookup")
            fake_module_file = str(plugin_dir / "rtl_settings.py")
            with mock.patch.object(settings_module, "__file__", fake_module_file):
                info = settings_module._describe_layer_source(layer)

        self.assertEqual(info["name"], "lookup")
        self.assertEqual(info["provider"], "ogr")
        self.assertEqual(info["path_relative_to_plugin"], "data/lookup.gpkg")
        self.assertEqual(info["uri_suffix"], "|layername=lookup")
        self.assertTrue(info["path_absolute"])

    def test_a_file_outside_the_plugin_directory_has_no_relative_path(self):
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory() as plugin_tmp, tempfile.TemporaryDirectory() as other_tmp:
            outside_file = Path(other_tmp) / "lookup.gpkg"
            outside_file.write_text("stub", encoding="utf-8")

            layer = _FakeLayer("lookup", "ogr", str(outside_file))
            fake_module_file = str(Path(plugin_tmp) / "rtl_settings.py")
            with mock.patch.object(settings_module, "__file__", fake_module_file):
                info = settings_module._describe_layer_source(layer)

        self.assertIsNone(info["path_relative_to_plugin"])
        self.assertEqual(info["path_absolute"], str(outside_file.resolve()))

    def test_a_non_file_source_records_only_the_raw_value(self):
        from _rtl_plugin import rtl_settings as settings_module

        layer = _FakeLayer("mem", "memory", "Point?crs=EPSG:4326&field=name:string")
        info = settings_module._describe_layer_source(layer)
        self.assertIsNone(info["path_relative_to_plugin"])
        self.assertEqual(info["path_absolute"], "Point?crs=EPSG:4326&field=name:string")


class ResolveLayerFromDescriptionTests(unittest.TestCase):
    """_resolve_layer_from_description() - the import-time counterpart:
    locating and loading (or gracefully failing on) a described layer."""

    def _added_layer_ids(self):
        return set(QgsProject.instance().mapLayers().keys())

    def test_loads_a_bundled_file_from_its_relative_path(self):
        from _rtl_plugin import rtl_settings as settings_module

        before = self._added_layer_ids()
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            data_dir = plugin_dir / "data"
            data_dir.mkdir()
            data_file = data_dir / "lookup.geojson"
            data_file.write_text(_GEOJSON_SAMPLE, encoding="utf-8")

            info = {
                "name": "lookup",
                "provider": "ogr",
                "path_relative_to_plugin": "data/lookup.geojson",
                "path_absolute": None,
                "uri_suffix": "",
            }
            layer_id, warning = settings_module._resolve_layer_from_description(info, plugin_dir)

            self.assertEqual(warning, "")
            self.assertTrue(layer_id)
            layer = QgsProject.instance().mapLayer(layer_id)
            self.assertIsNotNone(layer)
            self.assertTrue(layer.isValid())
            QgsProject.instance().removeMapLayer(layer_id)
        self.assertEqual(self._added_layer_ids(), before)

    def test_reuses_an_already_loaded_layer_with_the_same_source(self):
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            data_file = plugin_dir / "lookup.geojson"
            data_file.write_text(_GEOJSON_SAMPLE, encoding="utf-8")

            from qgis.core import QgsVectorLayer

            existing = QgsVectorLayer(str(data_file), "already loaded", "ogr")
            self.assertTrue(existing.isValid())
            QgsProject.instance().addMapLayer(existing)
            try:
                info = {
                    "name": "lookup",
                    "provider": "ogr",
                    "path_relative_to_plugin": "lookup.geojson",
                    "path_absolute": None,
                    "uri_suffix": "",
                }
                layer_id, warning = settings_module._resolve_layer_from_description(info, plugin_dir)
                self.assertEqual(warning, "")
                self.assertEqual(layer_id, existing.id())
            finally:
                QgsProject.instance().removeMapLayer(existing.id())

    def test_a_missing_file_returns_no_id_and_a_clear_warning(self):
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory() as tmp:
            info = {
                "name": "lookup",
                "provider": "ogr",
                "path_relative_to_plugin": "does_not_exist/lookup.gpkg",
                "path_absolute": None,
                "uri_suffix": "",
            }
            layer_id, warning = settings_module._resolve_layer_from_description(info, Path(tmp))

        self.assertEqual(layer_id, "")
        self.assertIn("could not be found", warning)

    def test_a_description_with_no_path_at_all_returns_no_id_and_a_clear_warning(self):
        from _rtl_plugin import rtl_settings as settings_module

        info = {"name": "lookup", "provider": "memory", "path_relative_to_plugin": None, "path_absolute": None, "uri_suffix": ""}
        layer_id, warning = settings_module._resolve_layer_from_description(info, Path.cwd())
        self.assertEqual(layer_id, "")
        self.assertTrue(warning)


class SettingsExportImportTests(unittest.TestCase):
    """Settings.export_dict() / apply_dict() - the round trip a colleague's
    machine goes through after receiving an exported configuration file."""

    def setUp(self):
        self._enabled = Settings.plugin_enabled()
        self._ac_enabled = Settings.autocomplete_enabled()
        self._max_values = Settings.max_suggested_values()
        self._default_read_mode = Settings.default_read_mode()
        self._layer_id = Settings.layer_id()
        self._fields = {key: Settings.field(key) for key in Settings.FIELD_KEYS}

    def tearDown(self):
        Settings.set_plugin_enabled(self._enabled)
        Settings.set_autocomplete_enabled(self._ac_enabled)
        Settings.set_max_suggested_values(self._max_values)
        Settings.set_default_read_mode(self._default_read_mode)
        Settings.set_layer_id(self._layer_id)
        for key, value in self._fields.items():
            Settings.set_field(key, value)

    def test_export_dict_with_autocomplete_disabled_has_no_layer(self):
        Settings.set_autocomplete_enabled(False)
        Settings.set_layer_id("")
        data = Settings.export_dict()
        self.assertTrue(data["rtl_expression_editor_settings"])
        self.assertFalse(data["autocomplete_enabled"])
        self.assertIsNone(data["autocomplete_layer"])

    def test_apply_dict_rejects_a_file_with_no_marker(self):
        with self.assertRaises(SettingsImportError):
            Settings.apply_dict({"some_other_tool_config": True})

    def test_apply_dict_rejects_a_non_dict_payload(self):
        with self.assertRaises(SettingsImportError):
            Settings.apply_dict(["not", "a", "dict"])

    def test_apply_dict_leaves_settings_untouched_when_it_raises(self):
        Settings.set_plugin_enabled(True)
        Settings.set_max_suggested_values(42)
        try:
            Settings.apply_dict({"not_our_format": True})
        except SettingsImportError:
            pass
        self.assertTrue(Settings.plugin_enabled())
        self.assertEqual(Settings.max_suggested_values(), 42)

    def test_apply_dict_applies_simple_fields_and_flags(self):
        data = {
            "rtl_expression_editor_settings": True,
            "format_version": 1,
            "plugin_enabled": False,
            "max_suggested_values": 33,
            "default_read_mode": True,
            "autocomplete_enabled": False,
            "autocomplete_fields": {"field_names": "F", "value": "V"},
            "autocomplete_layer": None,
        }
        warnings = Settings.apply_dict(data)
        self.assertEqual(warnings, [])
        self.assertFalse(Settings.plugin_enabled())
        self.assertEqual(Settings.max_suggested_values(), 33)
        self.assertTrue(Settings.default_read_mode())
        self.assertFalse(Settings.autocomplete_enabled())
        self.assertEqual(Settings.field("field_names"), "F")
        self.assertEqual(Settings.field("value"), "V")
        self.assertEqual(Settings.field("description"), "")

    def test_apply_dict_warns_but_still_applies_the_rest_on_malformed_fields(self):
        data = {
            "rtl_expression_editor_settings": True,
            "plugin_enabled": True,
            "autocomplete_fields": "not a dict",
            "autocomplete_enabled": False,
        }
        warnings = Settings.apply_dict(data)
        self.assertTrue(any("autocomplete_fields" in w for w in warnings))
        self.assertTrue(Settings.plugin_enabled())

    def test_apply_dict_with_an_unresolvable_layer_warns_and_clears_the_layer_id(self):
        Settings.set_layer_id("some-stale-id")
        data = {
            "rtl_expression_editor_settings": True,
            "autocomplete_enabled": True,
            "autocomplete_fields": {"field_names": "F", "value": "V"},
            "autocomplete_layer": {
                "name": "lookup",
                "provider": "ogr",
                "path_relative_to_plugin": "nowhere/lookup.gpkg",
                "path_absolute": None,
                "uri_suffix": "",
            },
        }
        warnings = Settings.apply_dict(data)
        self.assertTrue(any("could not be found" in w for w in warnings))
        self.assertEqual(Settings.layer_id(), "")

    def test_a_referenced_layer_is_resolved_even_when_autocomplete_enabled_is_false(self):
        """Regression: a file exported before ticking "Enable custom
        autocomplete source" (an easy thing to forget) still has a fully
        configured layer and fields - those must still be loaded, not
        silently dropped just because the enabled flag itself was off."""
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            data_dir = plugin_dir / "data"
            data_dir.mkdir()
            (data_dir / "lookup.geojson").write_text(_GEOJSON_SAMPLE, encoding="utf-8")

            data = {
                "rtl_expression_editor_settings": True,
                "autocomplete_enabled": False,
                "autocomplete_fields": {"field_names": "field_name", "value": "value"},
                "autocomplete_layer": {
                    "name": "lookup",
                    "provider": "ogr",
                    "path_relative_to_plugin": "data/lookup.geojson",
                    "path_absolute": None,
                    "uri_suffix": "",
                },
            }
            warnings = Settings.apply_dict(data, plugin_dir=plugin_dir)

            self.assertEqual(warnings, [])
            self.assertFalse(Settings.autocomplete_enabled())  # respected as exported
            layer = Settings.autocomplete_layer()
            self.assertIsNotNone(layer)  # but still loaded into the project
            self.assertTrue(layer.isValid())
            QgsProject.instance().removeMapLayer(layer.id())

    def test_export_then_apply_round_trips_a_bundled_layer(self):
        """The full distribution scenario: export from one "install"
        location, apply as if on a colleague's machine where the plugin (and
        its bundled data file) live somewhere else entirely."""
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "rtl_expression_editor"
            data_dir = plugin_dir / "data"
            data_dir.mkdir(parents=True)
            data_file = data_dir / "lookup.geojson"
            data_file.write_text(_GEOJSON_SAMPLE, encoding="utf-8")

            from qgis.core import QgsVectorLayer

            layer = QgsVectorLayer(str(data_file), "lookup", "ogr")
            self.assertTrue(layer.isValid())
            QgsProject.instance().addMapLayer(layer)
            Settings.set_autocomplete_enabled(True)
            Settings.set_layer_id(layer.id())
            Settings.set_field("field_names", "field_name")
            Settings.set_field("value", "value")

            fake_module_file = str(plugin_dir / "rtl_settings.py")
            try:
                with mock.patch.object(settings_module, "__file__", fake_module_file):
                    exported = Settings.export_dict()
            finally:
                QgsProject.instance().removeMapLayer(layer.id())

            self.assertEqual(
                exported["autocomplete_layer"]["path_relative_to_plugin"], "data/lookup.geojson"
            )

            # Simulate "a different machine": apply against a NEW plugin_dir
            # that only knows about this same relative layout.
            other_install = Path(tempfile.mkdtemp())
            try:
                (other_install / "data").mkdir()
                (other_install / "data" / "lookup.geojson").write_text(
                    _GEOJSON_SAMPLE, encoding="utf-8"
                )
                warnings = Settings.apply_dict(exported, plugin_dir=other_install)
                self.assertEqual(warnings, [])
                self.assertTrue(Settings.autocomplete_enabled())
                new_layer = Settings.autocomplete_layer()
                self.assertIsNotNone(new_layer)
                self.assertTrue(new_layer.isValid())
                QgsProject.instance().removeMapLayer(new_layer.id())
            finally:
                import shutil

                shutil.rmtree(other_install, ignore_errors=True)


def _row_label_for(dialog, widget):
    """The QFormLayout row label associated with ``widget``, wherever in the
    dialog its form actually lives - avoids assuming which QFormLayout
    instance (there is more than one) owns which widget."""
    from qgis.PyQt.QtWidgets import QFormLayout

    for form in dialog.findChildren(QFormLayout):
        label = form.labelForField(widget)
        if label is not None:
            return label
    return None


class SettingsDialogLabelTooltipTests(unittest.TestCase):
    """A parameter's tooltip must show when hovering its NAME (the
    QFormLayout row label), not only its input control - see
    SettingsDialog._mirror_label_tooltip()."""

    def test_row_labels_carry_the_same_tooltip_as_their_widget(self):
        from qgis.PyQt.QtCore import Qt

        from _rtl_plugin.rtl_settings import SettingsDialog

        dialog = SettingsDialog()
        try:
            widgets = [
                dialog.cmb_layer,
                dialog.cmb_mode,
                dialog.spin_max_values,
                *dialog.field_combos.values(),
            ]
            for widget in widgets:
                label = _row_label_for(dialog, widget)
                self.assertIsNotNone(label, f"no row label found for {widget.toolTip()[:30]!r}")
                self.assertTrue(label.toolTip())
                self.assertEqual(label.toolTip(), widget.toolTip())
                # Most of these widgets start disabled (custom autocomplete
                # is off by default) - the label must still show its tooltip.
                self.assertTrue(label.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips))
        finally:
            dialog.deleteLater()


class BundledConfigAutoImportTests(unittest.TestCase):
    """Settings.apply_bundled_config_if_present() - the auto-import that
    lets a colleague's zip carry a ready-made configuration, applied with no
    Import Settings click of their own. Tracked by the bundled file's own
    content, not a one-shot flag - reinstalling the plugin never clears
    QgsSettings, so a boolean "already done" would permanently block a later
    reinstall carrying a newly re-exported file, which is exactly the bug
    this replaced."""

    def setUp(self):
        self._ac_enabled = Settings.autocomplete_enabled()
        self._layer_id = Settings.layer_id()
        self._fields = {key: Settings.field(key) for key in Settings.FIELD_KEYS}
        self._hash = Settings._bundled_config_hash()
        Settings._set_bundled_config_hash("")
        Settings.set_autocomplete_enabled(False)
        Settings.set_layer_id("")

    def tearDown(self):
        Settings.set_autocomplete_enabled(self._ac_enabled)
        Settings.set_layer_id(self._layer_id)
        for key, value in self._fields.items():
            Settings.set_field(key, value)
        Settings._set_bundled_config_hash(self._hash)

    def test_no_bundled_file_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = Settings.apply_bundled_config_if_present(Path(tmp))
        self.assertIsNone(result)
        self.assertEqual(Settings._bundled_config_hash(), "")

    def test_a_bundled_file_is_applied_even_when_something_is_already_configured(self):
        """Regression: reinstalling the plugin never clears QgsSettings, so
        by the time a colleague (or the same person, re-testing) reinstalls
        with a bundled file, autocomplete is almost always already
        "configured" from before - that must not block the import."""
        from _rtl_plugin import rtl_settings as settings_module

        Settings.set_autocomplete_enabled(True)
        Settings.set_layer_id("some-pre-existing-id")

        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            data_dir = plugin_dir / "data"
            data_dir.mkdir()
            (data_dir / "lookup.geojson").write_text(_GEOJSON_SAMPLE, encoding="utf-8")

            config = {
                "rtl_expression_editor_settings": True,
                "autocomplete_enabled": True,
                "autocomplete_fields": {"field_names": "field_name", "value": "value"},
                "autocomplete_layer": {
                    "name": "lookup",
                    "provider": "ogr",
                    "path_relative_to_plugin": "data/lookup.geojson",
                    "path_absolute": None,
                    "uri_suffix": "",
                },
            }
            (plugin_dir / settings_module.BUNDLED_CONFIG_FILENAME).write_text(
                json.dumps(config), encoding="utf-8"
            )

            warnings = Settings.apply_bundled_config_if_present(plugin_dir)

            self.assertEqual(warnings, [])
            self.assertTrue(Settings.autocomplete_enabled())
            self.assertNotEqual(Settings.layer_id(), "some-pre-existing-id")
            layer = Settings.autocomplete_layer()
            self.assertIsNotNone(layer)
            QgsProject.instance().removeMapLayer(layer.id())

    def test_the_same_unchanged_file_is_not_reapplied_on_a_later_call(self):
        """Regression guard the other way: once a given bundled file's
        content has been applied, running again with THE SAME bytes must be
        a no-op - otherwise a later, unrelated Settings change would be
        silently reverted on every subsequent QGIS startup."""
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            (plugin_dir / settings_module.BUNDLED_CONFIG_FILENAME).write_text(
                json.dumps({"rtl_expression_editor_settings": True, "max_suggested_values": 7}),
                encoding="utf-8",
            )
            first = Settings.apply_bundled_config_if_present(plugin_dir)
            self.assertIsNotNone(first)

            Settings.set_max_suggested_values(99)  # a later, unrelated manual change
            second = Settings.apply_bundled_config_if_present(plugin_dir)

        self.assertIsNone(second)
        self.assertEqual(Settings.max_suggested_values(), 99)  # left untouched

    def test_a_changed_bundled_file_is_applied_again(self):
        """A new export (different content, same filename) - e.g. a second
        reinstall with an updated bundled config - must be picked up, not
        blocked by the previous file having already been applied."""
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            config_path = plugin_dir / settings_module.BUNDLED_CONFIG_FILENAME
            config_path.write_text(
                json.dumps({"rtl_expression_editor_settings": True, "max_suggested_values": 7}),
                encoding="utf-8",
            )
            first = Settings.apply_bundled_config_if_present(plugin_dir)
            self.assertIsNotNone(first)
            self.assertEqual(Settings.max_suggested_values(), 7)

            config_path.write_text(
                json.dumps({"rtl_expression_editor_settings": True, "max_suggested_values": 42}),
                encoding="utf-8",
            )
            second = Settings.apply_bundled_config_if_present(plugin_dir)

        self.assertIsNotNone(second)
        self.assertEqual(Settings.max_suggested_values(), 42)

    def test_a_malformed_bundled_file_is_not_recorded_as_applied(self):
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            (plugin_dir / settings_module.BUNDLED_CONFIG_FILENAME).write_text(
                "{not valid json", encoding="utf-8"
            )
            result = Settings.apply_bundled_config_if_present(plugin_dir)

        self.assertIsNone(result)
        self.assertEqual(Settings._bundled_config_hash(), "")


if __name__ == "__main__":
    unittest.main()

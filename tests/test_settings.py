# -*- coding: utf-8 -*-
"""Settings storage: every getter/setter round-trips through QgsSettings."""

import gc
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QApplication

from _rtl_plugin.rtl_settings import Settings, SettingsImportError

from .utils import make_lookup_layer, reset_plugin_settings


def _release_file_handles() -> None:
    """Give any just-dropped QgsVectorLayer a chance to actually release
    its underlying OGR/GDAL file handle, before removing the directory it
    points into.

    Dropping the last Python reference to a layer (``layer = None``,
    clearing a cache that held it) does not always release that handle
    IMMEDIATELY: PyQt/PyQGIS objects commonly sit in reference cycles that
    plain refcounting cannot collect - only a ``gc.collect()`` pass can -
    and QGIS's own OGR connection pool can keep a file open independently
    of any Python reference to it, released only once pending, queued
    cleanup work runs on the event loop, on a schedule this cannot fully
    control from here. This is therefore only a best-effort nudge, not a
    guarantee - every ``tempfile.TemporaryDirectory()`` in this module is
    also created with ``ignore_cleanup_errors=True``, so a file this could
    not manage to release in time is skipped rather than failing the test
    with ``PermissionError`` on Windows.
    """
    gc.collect()
    for _ in range(3):
        QApplication.processEvents()
    gc.collect()


class SettingsRoundTripTests(unittest.TestCase):
    def setUp(self):
        # QgsSettings persists across tests (and across QGIS sessions) - snapshot
        # and restore, so this suite never leaks into the user's real config.
        self._enabled = Settings.plugin_enabled()
        self._ac_enabled = Settings.autocomplete_enabled()
        self._max_values = Settings.max_suggested_values()
        self._default_read_mode = Settings.default_read_mode()
        self._layer_id = Settings.layer_id()
        self._layer_source = Settings.layer_source()
        self._fields = {key: Settings.field(key) for key in Settings.FIELD_KEYS}

    def tearDown(self):
        Settings.set_plugin_enabled(self._enabled)
        Settings.set_autocomplete_enabled(self._ac_enabled)
        Settings.set_max_suggested_values(self._max_values)
        Settings.set_default_read_mode(self._default_read_mode)
        Settings.set_layer_id(self._layer_id)
        Settings.set_layer_source(self._layer_source)
        Settings.set_layer_for_testing(None)
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
        Settings.set_layer_source(None)
        self.assertIsNone(Settings.autocomplete_layer())

    def test_autocomplete_layer_returns_the_testing_override_when_set(self):
        layer = object()
        Settings.set_layer_for_testing(layer)
        self.assertIs(Settings.autocomplete_layer(), layer)
        Settings.set_layer_for_testing(None)
        self.assertIsNone(Settings.autocomplete_layer())

    def test_layer_source_round_trips_through_settings(self):
        info = {"name": "lookup", "provider": "ogr", "kind": "file", "path_absolute": "/tmp/x.gpkg"}
        Settings.set_layer_source(info)
        self.assertEqual(Settings.layer_source(), info)
        Settings.set_layer_source(None)
        self.assertIsNone(Settings.layer_source())

    def test_has_layer_configured_reflects_either_the_new_or_legacy_key(self):
        Settings.set_layer_id("")
        Settings.set_layer_source(None)
        self.assertFalse(Settings.has_layer_configured())
        Settings.set_layer_source({"name": "x", "provider": "ogr"})
        self.assertTrue(Settings.has_layer_configured())
        Settings.set_layer_source(None)
        Settings.set_layer_id("some-legacy-id")
        self.assertTrue(Settings.has_layer_configured())


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

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            plugin_dir = Path(tmp) / "rtl_expression_editor"
            plugin_dir.mkdir()
            (plugin_dir / "rtl_settings.py").write_text("", encoding="utf-8")
            nested_tests = plugin_dir / "tests"
            nested_tests.mkdir()
            (nested_tests / "run_all.py").write_text("", encoding="utf-8")

            fake_file = str(plugin_dir / "rtl_settings.py")
            with mock.patch.object(settings_module, "__file__", fake_file):
                found = SettingsDialog._tests_directory()

            # Compare canonical ("real") paths, not the raw strings:
            # _tests_directory() derives its result from
            # Path(__file__).resolve(), so on Windows it comes back in
            # the OS's canonical long-name form - but on some setups (an
            # older Windows 11 build, or independently, any temp directory
            # whose name contains a space) tempfile.TemporaryDirectory()
            # itself can hand back an 8.3 short-name alias instead (e.g.
            # "RTL_EX~1" for "rtl_expression_editor"), which is the exact
            # same directory on disk but a different string - resolving
            # both sides here is what makes the comparison robust to that,
            # rather than flakily failing on a spelling difference alone.
            #
            # Both sides must be resolved HERE, while the temporary
            # directory still exists: Path.resolve() can only canonicalise
            # a short-name alias by asking the OS to look the path up on
            # disk, and silently falls back to plain string normalisation
            # once the path no longer exists - which would just reproduce
            # this same flaky mismatch outside the "with" block instead of
            # fixing it.
            self.assertIsNotNone(found)
            self.assertEqual(found.resolve(), nested_tests.resolve())

    def test_button_is_created_when_tests_directory_is_found(self):
        from _rtl_plugin.rtl_settings import SettingsDialog

        dialog = SettingsDialog()
        try:
            self.assertTrue(hasattr(dialog, "btn_run_tests"))
            self.assertEqual(dialog.btn_run_tests.text(), "Run Tests")
        finally:
            dialog.deleteLater()

    def test_run_all_override_is_keyword_only_so_a_real_click_cannot_reach_it(self):
        """Regression: QPushButton.clicked emits clicked(bool checked), and
        PyQt auto-forwards emitted signal arguments into a slot's own
        POSITIONAL parameters. A first version of _run_tests() made
        _run_all_override an ordinary positional parameter, and an actual
        click then passed that bool straight into it - run_all.main()
        ends up being called on a bool instead of the real module
        (AttributeError: 'bool' object has no attribute 'main'). Keeping
        it keyword-only is what makes it invisible to that auto-forwarding
        - checked here directly, structurally, rather than by clicking the
        real button and needing to fake out an entire recursive test run
        to observe the difference."""
        import inspect

        from _rtl_plugin.rtl_settings import SettingsDialog

        sig = inspect.signature(SettingsDialog._run_tests)
        param = sig.parameters["_run_all_override"]
        self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_run_tests_passes_the_result_through_and_re_enables_the_button(self):
        """Exercises _run_tests()'s own control flow - disable/run/re-enable,
        handing the result to _show_test_results() - without paying for a
        real, recursive run of the whole suite.

        Passes a fake module via the ``_run_all_override`` testing seam
        rather than ``mock.patch("tests.run_all.main", ...)``: _run_tests()
        forces a fresh re-import of ``tests`` on every real call (so a fix
        to any test file is picked up without restarting QGIS - see its
        own docstring), which would simply discard a module object patched
        that way before ever calling it.
        """
        from unittest import mock

        from _rtl_plugin.rtl_settings import SettingsDialog

        dialog = SettingsDialog()
        fake_result = mock.Mock(wasSuccessful=lambda: True, testsRun=3, failures=[], errors=[])
        fake_run_all = mock.Mock(main=mock.Mock(return_value=fake_result))
        try:
            with mock.patch.object(SettingsDialog, "_show_test_results") as mocked_show:
                dialog._run_tests(_run_all_override=fake_run_all)

            fake_run_all.main.assert_called_once()
            mocked_show.assert_called_once()
            self.assertIs(mocked_show.call_args[0][0], fake_result)
            self.assertTrue(dialog.btn_run_tests.isEnabled())
            self.assertEqual(dialog.btn_run_tests.text(), "Run Tests")
        finally:
            dialog.deleteLater()

    def test_falls_back_to_a_plain_run_when_resultclass_is_unsupported(self):
        """A stale installed tests/run_all.py that predates the
        ``resultclass`` parameter (or one Python already had cached in
        sys.modules from earlier in the same QGIS session) must not crash
        the button outright - see rtl_settings._run_tests()."""
        from unittest import mock

        from _rtl_plugin.rtl_settings import SettingsDialog

        dialog = SettingsDialog()
        fake_result = mock.Mock(wasSuccessful=lambda: True, testsRun=1, failures=[], errors=[])

        def _fake_main(verbosity=2, stream=None, resultclass=None):
            if resultclass is not None:
                raise TypeError("main() got an unexpected keyword argument 'resultclass'")
            return fake_result

        fake_run_all = mock.Mock(main=mock.Mock(side_effect=_fake_main))

        try:
            with mock.patch.object(SettingsDialog, "_show_test_results") as mocked_show:
                dialog._run_tests(_run_all_override=fake_run_all)

            self.assertEqual(fake_run_all.main.call_count, 2)
            mocked_show.assert_called_once()
            self.assertIs(mocked_show.call_args[0][0], fake_result)
            self.assertTrue(dialog.btn_run_tests.isEnabled())
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

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        self.assertEqual(info["kind"], "file")
        self.assertEqual(info["path_relative_to_plugin"], "data/lookup.gpkg")
        self.assertEqual(info["uri_suffix"], "|layername=lookup")
        self.assertTrue(info["path_absolute"])

    def test_a_file_outside_the_plugin_directory_has_no_relative_path(self):
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as plugin_tmp, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as other_tmp:
            outside_file = Path(other_tmp) / "lookup.gpkg"
            outside_file.write_text("stub", encoding="utf-8")

            layer = _FakeLayer("lookup", "ogr", str(outside_file))
            fake_module_file = str(Path(plugin_tmp) / "rtl_settings.py")
            with mock.patch.object(settings_module, "__file__", fake_module_file):
                info = settings_module._describe_layer_source(layer)

        self.assertEqual(info["kind"], "file")
        self.assertIsNone(info["path_relative_to_plugin"])
        self.assertEqual(info["path_absolute"], str(outside_file.resolve()))

    def test_a_memory_layer_source_is_recorded_as_a_connection_with_no_path(self):
        from _rtl_plugin import rtl_settings as settings_module

        layer = _FakeLayer("mem", "memory", "Point?crs=EPSG:4326&field=name:string")
        info = settings_module._describe_layer_source(layer)
        self.assertEqual(info["kind"], "connection")
        self.assertIsNone(info["path_relative_to_plugin"])
        self.assertEqual(info["path_absolute"], "Point?crs=EPSG:4326&field=name:string")

    def test_a_database_connections_username_and_password_are_never_recorded(self):
        """Regression/security: an exported settings file can be shared or
        bundled into a plugin zip - a database layer's credentials must
        never end up in it, even though everything else about the
        connection (needed to attempt reconnecting) still should."""
        from qgis.core import QgsDataSourceUri

        from _rtl_plugin import rtl_settings as settings_module

        uri = QgsDataSourceUri()
        uri.setConnection("dbhost", "5432", "mydb", "alice", "s3cr3t")
        uri.setDataSource("public", "buildings", "geom")
        layer = _FakeLayer("buildings", "postgres", uri.uri())

        info = settings_module._describe_layer_source(layer)

        self.assertEqual(info["kind"], "connection")
        self.assertNotIn("s3cr3t", info["path_absolute"])
        self.assertNotIn("alice", info["path_absolute"])
        self.assertIn("mydb", info["path_absolute"])


class LoadLayerFromDescriptionTests(unittest.TestCase):
    """_load_layer_from_description() - the import-time (and live-lookup)
    counterpart: locating and opening (or gracefully failing on) a described
    dataset as a standalone layer, never added to any project."""

    def _added_layer_ids(self):
        return set(QgsProject.instance().mapLayers().keys())

    def test_loads_a_bundled_file_from_its_relative_path(self):
        from _rtl_plugin import rtl_settings as settings_module

        before = self._added_layer_ids()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
            layer, warning = settings_module._load_layer_from_description(info, plugin_dir)

            self.assertEqual(warning, "")
            self.assertIsNotNone(layer)
            self.assertTrue(layer.isValid())
            # Release the OGR/GDAL file handle before the temp dir is removed
            # below - see _release_file_handles()'s own docstring.
            del layer
            _release_file_handles()
        # Never added to the project - that is the whole point of this
        # mechanism (see Settings.autocomplete_layer()).
        self.assertEqual(self._added_layer_ids(), before)

    def test_a_missing_file_returns_no_layer_and_a_clear_warning(self):
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            info = {
                "name": "lookup",
                "provider": "ogr",
                "path_relative_to_plugin": "does_not_exist/lookup.gpkg",
                "path_absolute": None,
                "uri_suffix": "",
            }
            layer, warning = settings_module._load_layer_from_description(info, Path(tmp))

        self.assertIsNone(layer)
        self.assertIn("could not be found", warning)

    def test_a_description_with_no_path_at_all_returns_no_layer_and_a_clear_warning(self):
        from _rtl_plugin import rtl_settings as settings_module

        info = {"name": "lookup", "provider": "memory", "path_relative_to_plugin": None, "path_absolute": None, "uri_suffix": ""}
        layer, warning = settings_module._load_layer_from_description(info, Path.cwd())
        self.assertIsNone(layer)
        self.assertTrue(warning)

    def test_a_file_that_exists_but_will_not_load_is_reported_not_accessible(self):
        """Distinct from "not found": the path is there, but the layer still
        fails to open (corrupted/unsupported content, permissions, ...)."""
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            plugin_dir = Path(tmp)
            bad_file = plugin_dir / "lookup.gpkg"
            bad_file.write_text("this is not a real GeoPackage", encoding="utf-8")

            info = {
                "name": "lookup",
                "provider": "ogr",
                "kind": "file",
                "path_relative_to_plugin": "lookup.gpkg",
                "path_absolute": None,
                "uri_suffix": "",
            }
            layer, warning = settings_module._load_layer_from_description(info, plugin_dir)

        self.assertIsNone(layer)
        self.assertIn("not accessible", warning)
        self.assertNotIn("could not be found", warning)

    def test_a_connection_is_opened_directly_without_touching_the_project(self):
        from _rtl_plugin import rtl_settings as settings_module

        before = self._added_layer_ids()
        info = {
            "name": "lookup",
            "provider": "memory",
            "kind": "connection",
            "path_absolute": "Point?crs=EPSG:4326&field=name:string",
            "uri_suffix": "",
        }
        layer, warning = settings_module._load_layer_from_description(info, Path.cwd())
        self.assertEqual(warning, "")
        self.assertIsNotNone(layer)
        self.assertTrue(layer.isValid())
        self.assertEqual(self._added_layer_ids(), before)

    def test_a_connection_that_cannot_be_reached_is_reported_not_accessible(self):
        from _rtl_plugin import rtl_settings as settings_module

        info = {
            "name": "remote_table",
            "provider": "ogr",
            "kind": "connection",
            "path_absolute": "not a real connection string at all",
            "uri_suffix": "",
        }
        layer, warning = settings_module._load_layer_from_description(info, Path.cwd())
        self.assertIsNone(layer)
        self.assertIn("not accessible", warning)

    def test_a_connection_with_no_information_recorded_gives_a_clear_warning(self):
        from _rtl_plugin import rtl_settings as settings_module

        info = {"name": "remote_table", "provider": "postgres", "kind": "connection", "path_absolute": None}
        layer, warning = settings_module._load_layer_from_description(info, Path.cwd())
        self.assertIsNone(layer)
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
        self._layer_source = Settings.layer_source()
        self._fields = {key: Settings.field(key) for key in Settings.FIELD_KEYS}

    def tearDown(self):
        Settings.set_plugin_enabled(self._enabled)
        Settings.set_autocomplete_enabled(self._ac_enabled)
        Settings.set_max_suggested_values(self._max_values)
        Settings.set_default_read_mode(self._default_read_mode)
        Settings.set_layer_id(self._layer_id)
        Settings.set_layer_source(self._layer_source)
        for key, value in self._fields.items():
            Settings.set_field(key, value)

    def test_export_dict_with_autocomplete_disabled_has_no_layer(self):
        Settings.set_autocomplete_enabled(False)
        Settings.set_layer_id("")
        Settings.set_layer_source(None)
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

    def test_apply_dict_with_an_unresolvable_layer_warns_and_clears_the_configured_layer(self):
        Settings.set_layer_id("some-stale-id")
        Settings.set_layer_source({"name": "old", "provider": "ogr", "kind": "connection", "path_absolute": "x"})
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
        self.assertIsNone(Settings.layer_source())

    def test_a_referenced_layer_is_resolved_even_when_autocomplete_enabled_is_false(self):
        """Regression: a file exported before ticking "Enable custom
        autocomplete source" (an easy thing to forget) still has a fully
        configured lookup dataset and fields - those must still be loaded,
        not silently dropped just because the enabled flag itself was off."""
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
            # Settings.autocomplete_layer() itself re-resolves the stored
            # (relative-path) description lazily, against wherever THIS
            # plugin install currently lives (Path(__file__).resolve().parent
            # - see its own docstring) - in real use that is always the same
            # directory apply_dict() was just given, since the dialog passes
            # its own real location for both. Verified directly through
            # _load_layer_from_description() with that same explicit
            # plugin_dir instead of through that lazy __file__-derived path:
            # mocking __file__ to a FILE THAT DOES NOT EXIST (there is no
            # real rtl_settings.py inside this temp dir) is exactly the case
            # Path.resolve() cannot always canonicalise correctly on Windows
            # (see SettingsDialog._tests_directory()'s own docstring on
            # short-name aliasing) - an artifact of faking __file__ for a
            # test, not something a real, already-installed plugin ever hits.
            layer, load_warning = settings_module._load_layer_from_description(
                Settings.layer_source(), plugin_dir
            )
            self.assertEqual(load_warning, "")
            self.assertIsNotNone(layer)  # but still connected, ready to use
            self.assertTrue(layer.isValid())
            # Release the OGR/GDAL file handle before tmp is removed below -
            # see _release_file_handles()'s own docstring.
            layer = None
            _release_file_handles()

    def test_export_then_apply_round_trips_a_bundled_layer(self):
        """The full distribution scenario: export from one "install"
        location, apply as if on a colleague's machine where the plugin (and
        its bundled data file) live somewhere else entirely."""
        from _rtl_plugin import rtl_settings as settings_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            plugin_dir = Path(tmp) / "rtl_expression_editor"
            data_dir = plugin_dir / "data"
            data_dir.mkdir(parents=True)
            data_file = data_dir / "lookup.geojson"
            data_file.write_text(_GEOJSON_SAMPLE, encoding="utf-8")

            from qgis.core import QgsVectorLayer

            layer = QgsVectorLayer(str(data_file), "lookup", "ogr")
            self.assertTrue(layer.isValid())
            Settings.set_autocomplete_enabled(True)
            fake_module_file = str(plugin_dir / "rtl_settings.py")
            with mock.patch.object(settings_module, "__file__", fake_module_file):
                Settings.set_layer_source(settings_module._describe_layer_source(layer))
            Settings.set_field("field_names", "field_name")
            Settings.set_field("value", "value")
            # Release this OGR/GDAL handle on data_file now that its source
            # has been captured - see _release_file_handles()'s own docstring.
            layer = None
            _release_file_handles()

            with mock.patch.object(settings_module, "__file__", fake_module_file):
                exported = Settings.export_dict()
                # export_dict() itself resolves and caches a layer (to run
                # the legacy-layer_id migration, see its docstring) - drop it
                # too, it also points into data_file.
                settings_module._LAYER_CACHE.clear()
            _release_file_handles()

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
                # Verified directly through _load_layer_from_description()
                # with the same explicit plugin_dir apply_dict() itself just
                # used, rather than through Settings.autocomplete_layer()'s
                # own lazy __file__-derived path - see the note on this same
                # pattern in test_a_referenced_layer_is_resolved_even_when_
                # autocomplete_enabled_is_false above.
                new_layer, load_warning = settings_module._load_layer_from_description(
                    Settings.layer_source(), other_install
                )
                self.assertEqual(load_warning, "")
                self.assertIsNotNone(new_layer)
                self.assertTrue(new_layer.isValid())
            finally:
                import shutil

                # Release the OGR/GDAL handle on other_install's file before
                # removing it - see _release_file_handles()'s own docstring.
                new_layer = None
                _release_file_handles()
                shutil.rmtree(other_install, ignore_errors=True)
            # And once more before the OUTER temp dir (tmp, still open here)
            # is removed on the way out of the "with" block above.
            _release_file_handles()


class SettingsDialogLookupDatasetTests(unittest.TestCase):
    """The Browse.../Clear pair that replaced the old project-layer combo -
    see SettingsDialog._browse_layer()/_clear_layer()/_pick_layer_from_browser().
    _pick_layer_from_browser() itself (the real modal Browser dialog) is not
    exercised here - only what happens with whatever it returns."""

    def setUp(self):
        reset_plugin_settings()

    def tearDown(self):
        reset_plugin_settings()

    def test_browsing_a_dataset_updates_the_label_and_field_combos(self):
        from _rtl_plugin.rtl_settings import SettingsDialog

        layer = make_lookup_layer()
        info = {"name": "lookup", "provider": "memory", "kind": "connection", "path_absolute": layer.source(), "uri_suffix": ""}

        dialog = SettingsDialog()
        try:
            with mock.patch.object(SettingsDialog, "_pick_layer_from_browser", return_value=(layer, info)):
                dialog._browse_layer()

            self.assertIs(dialog._selected_layer, layer)
            self.assertIs(dialog._selected_source_info, info)
            self.assertEqual(dialog.lbl_layer.text(), layer.name())
            # Field combos were repopulated from the newly picked dataset.
            dialog.field_combos["field_names"].setField("field_name")
            self.assertEqual(dialog.field_combos["field_names"].currentField(), "field_name")
        finally:
            dialog.deleteLater()

    def test_clear_resets_the_selection(self):
        from _rtl_plugin.rtl_settings import SettingsDialog

        layer = make_lookup_layer()
        info = {"name": "lookup", "provider": "memory", "kind": "connection", "path_absolute": layer.source(), "uri_suffix": ""}

        dialog = SettingsDialog()
        try:
            with mock.patch.object(SettingsDialog, "_pick_layer_from_browser", return_value=(layer, info)):
                dialog._browse_layer()
            dialog._clear_layer()

            self.assertIsNone(dialog._selected_layer)
            self.assertIsNone(dialog._selected_source_info)
            self.assertEqual(dialog.lbl_layer.text(), "(none selected)")
        finally:
            dialog.deleteLater()

    def test_cancelling_the_browser_leaves_the_previous_selection_untouched(self):
        from _rtl_plugin.rtl_settings import SettingsDialog

        dialog = SettingsDialog()
        try:
            with mock.patch.object(SettingsDialog, "_pick_layer_from_browser", return_value=(None, None)):
                dialog._browse_layer()
            self.assertIsNone(dialog._selected_layer)
            self.assertEqual(dialog.lbl_layer.text(), "(none selected)")
        finally:
            dialog.deleteLater()

    def test_accepting_persists_the_picked_source_description(self):
        from _rtl_plugin.rtl_settings import SettingsDialog

        layer = make_lookup_layer()
        info = {"name": "lookup", "provider": "memory", "kind": "connection", "path_absolute": layer.source(), "uri_suffix": ""}

        dialog = SettingsDialog()
        try:
            dialog.chk_ac.setChecked(True)
            with mock.patch.object(SettingsDialog, "_pick_layer_from_browser", return_value=(layer, info)):
                dialog._browse_layer()
            dialog.field_combos["field_names"].setField("field_name")
            dialog.field_combos["value"].setField("value")

            dialog._save()

            self.assertEqual(Settings.layer_source(), info)
            self.assertEqual(Settings.field("field_names"), "field_name")
            # Not, here, a round trip through Settings.autocomplete_layer():
            # info's path_absolute is captured from a "memory" provider's
            # own source() string, which - unlike a real Browser-picked
            # dataset - describes only that layer's schema, not the actual
            # features it holds at runtime (see set_layer_for_testing()'s
            # docstring); reconstructing from it is not guaranteed to
            # reproduce the same layer. What matters here is only that _save()
            # persisted exactly what was picked, asserted above.
        finally:
            dialog.deleteLater()


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
                dialog.layer_row,
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


if __name__ == "__main__":
    unittest.main()

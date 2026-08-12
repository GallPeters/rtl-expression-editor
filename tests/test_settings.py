# -*- coding: utf-8 -*-
"""Settings storage: every getter/setter round-trips through QgsSettings."""

import unittest

from _rtl_plugin.rtl_settings import Settings


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


if __name__ == "__main__":
    unittest.main()

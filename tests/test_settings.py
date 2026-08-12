# -*- coding: utf-8 -*-
"""Settings storage: every getter/setter round-trips through QgsSettings."""

import unittest

from src.rtl_settings import Settings


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


if __name__ == "__main__":
    unittest.main()

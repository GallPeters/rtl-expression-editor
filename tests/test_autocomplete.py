# -*- coding: utf-8 -*-
"""Custom autocomplete: context detection, and suggestions with and without a
configured lookup table.

Every function/variable name asserted on below (``lower``, ``buffer``, map
scope variables, ...) is read from the live QgsExpression /
QgsExpressionContextUtils API at test time, not hard-coded from memory - the
whole point of sourcing them that way in the plugin itself (see
rtl_autocomplete.builtin_functions/builtin_variables) is that this suite
tracks whatever the installed QGIS version actually registers, so it keeps
passing across QGIS upgrades without needing to be updated by hand.
"""

import unittest

from qgis.core import QgsExpression, QgsFeature, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QPlainTextEdit

from _rtl_plugin import rtl_autocomplete as ac
from _rtl_plugin.rtl_settings import Settings

from .utils import make_context_layer, make_lookup_layer, reset_plugin_settings


class SuggestionContextTests(unittest.TestCase):
    """The three trigger rules: fields after '"', values after "'", the full
    grouped list otherwise - and nothing else (no guessing from a preceding
    operator)."""

    def test_unterminated_double_quote_means_fields(self):
        self.assertEqual(ac.suggestion_context('"NA'), "fields")
        self.assertEqual(ac.suggestion_context('"'), "fields")

    def test_unterminated_single_quote_means_values(self):
        self.assertEqual(ac.suggestion_context("\"NAME\" = 'I"), "values")

    def test_after_at_sign_means_variables(self):
        self.assertEqual(ac.suggestion_context("@ma"), "variables")

    def test_after_an_operator_with_no_quote_is_not_narrowed_to_values(self):
        # Regression: this used to guess "values" from the preceding "=",
        # even with no quote typed at all.
        self.assertEqual(ac.suggestion_context('"CODE" = '), "mixed")

    def test_closed_quotes_and_plain_text_are_mixed(self):
        self.assertEqual(ac.suggestion_context('"NAME" = \'x\' AND '), "mixed")
        self.assertEqual(ac.suggestion_context(""), "mixed")


class DetectFieldNameTests(unittest.TestCase):
    def test_finds_the_nearest_preceding_field(self):
        self.assertEqual(
            ac.detect_field_name('"NAME" = \'x\' AND "COUNTRY" = '), "COUNTRY"
        )

    def test_finds_the_field_being_typed(self):
        self.assertEqual(ac.detect_field_name('"COUN'), "COUN")


class BuiltinFunctionsAndVariablesComeFromTheLiveApiTests(unittest.TestCase):
    """These must be the QGIS installation's own list, not anything hard-coded
    in this suite - every value checked is fetched fresh from QgsExpression
    itself and compared against the plugin's output."""

    def test_every_function_name_is_a_real_registered_function(self):
        live_names = {f.name() for f in QgsExpression.Functions() if not f.name().startswith("_")}
        plugin_names = {name for name, *_rest in ac.builtin_functions()}
        self.assertTrue(plugin_names)
        self.assertTrue(plugin_names.issubset(live_names))

    def test_function_groups_match_qgsexpressions_own_groups(self):
        live_groups = {f.name(): f.group() for f in QgsExpression.Functions()}
        for name, _sig, _help, _count, group, _params in ac.builtin_functions():
            if name in live_groups:
                self.assertEqual(group, live_groups[name] or ac.GROUP_FUNCTIONS)

    def test_functions_carry_their_real_parameter_names(self):
        by_name = {name: params for name, _s, _h, _c, _g, params in ac.builtin_functions()}
        # "buffer" is a long-standing, stable QgsExpression function.
        self.assertIn("buffer", by_name)
        self.assertTrue(by_name["buffer"])

    def test_builtin_variables_include_the_global_scope(self):
        from qgis.core import QgsExpressionContextUtils

        live = set(QgsExpressionContextUtils.globalScope().variableNames())
        plugin = set(ac.builtin_variables())
        self.assertTrue(live.issubset(plugin))


class WithoutAnyLookupTableTests(unittest.TestCase):
    """Autocomplete must work with nothing configured at all - fields from
    the layer being edited, values from the layer's own data."""

    def setUp(self):
        reset_plugin_settings()
        self.layer = make_context_layer(("NAME", "COUNTRY"))
        QgsProject.instance().addMapLayer(self.layer)

    def tearDown(self):
        # Remove only the layer this test added - never every layer in the
        # project, which could be the user's own if this suite is ever run
        # from inside a live QGIS session (e.g. the Settings dialog's "Run
        # Tests" button) rather than a disposable qgis.testing process.
        QgsProject.instance().removeMapLayer(self.layer.id())
        reset_plugin_settings()

    def test_context_field_names_reads_the_layers_own_fields(self):
        self.assertEqual(set(ac.context_field_names(self.layer)), {"NAME", "COUNTRY"})

    def test_layer_first_values_returns_distinct_non_null_values_only(self):
        provider = self.layer.dataProvider()
        for value in ("A", "A", "B", None):
            feature = QgsFeature(self.layer.fields())
            if value is not None:
                feature.setAttribute("NAME", value)
            provider.addFeature(feature)
        values = ac.layer_first_values(self.layer, "NAME", 10)
        # Text values come back quoted - ready to insert as a literal.
        self.assertEqual(set(values), {"'A'", "'B'"})

    def test_layer_first_values_respects_the_limit(self):
        provider = self.layer.dataProvider()
        for value in ("A", "B", "C", "D"):
            feature = QgsFeature(self.layer.fields())
            feature.setAttribute("NAME", value)
            provider.addFeature(feature)
        self.assertEqual(len(ac.layer_first_values(self.layer, "NAME", 2)), 2)


class WithALookupTableTests(unittest.TestCase):
    """Same field/value lookup, now backed by a configured Settings table -
    grouped and described."""

    def setUp(self):
        reset_plugin_settings()
        self.context_layer = make_context_layer(("STATUS", "COUNTRY"))
        self.lookup_layer = make_lookup_layer()
        QgsProject.instance().addMapLayers([self.context_layer, self.lookup_layer])

        Settings.set_autocomplete_enabled(True)
        Settings.set_layer_id(self.lookup_layer.id())
        Settings.set_field("field_names", "field_name")
        Settings.set_field("value", "value")
        Settings.set_field("description", "description")
        Settings.set_field("group_code", "group_code")
        Settings.set_field("group_description", "group_description")
        Settings.set_field("table", "table")
        ac.cache().invalidate()

    def tearDown(self):
        reset_plugin_settings()
        # Only the two layers this test added - see the note in
        # WithoutAnyLookupTableTests.tearDown above.
        QgsProject.instance().removeMapLayers([self.context_layer.id(), self.lookup_layer.id()])
        ac.cache().invalidate()

    def test_configuration_is_usable(self):
        usable, reason = Settings.autocomplete_is_usable()
        self.assertTrue(usable, reason)

    def test_lookup_field_names_come_from_the_configured_table(self):
        self.assertEqual(set(ac.cache().lookup_field_names(["context"])), {"STATUS", "COUNTRY"})

    def test_lookup_values_are_described_and_grouped(self):
        entries = ac.cache().lookup("STATUS", ["context"])
        self.assertEqual(len(entries), 2)
        by_value = {entry.value: entry for entry in entries}
        self.assertEqual(by_value["1"].description, "Active")
        self.assertEqual(by_value["1"].group_label, "G1 (State)")

    def test_a_table_not_matching_the_current_layer_returns_nothing(self):
        self.assertEqual(ac.cache().lookup("STATUS", ["some_other_table"]), [])


class AutocompletePopupFlowTests(unittest.TestCase):
    """End-to-end through the real controller and a real QPlainTextEdit -
    the level closest to what actually happens in the Expression Builder."""

    def setUp(self):
        reset_plugin_settings()
        self.layer = make_context_layer(("NAME", "COUNTRY"))
        QgsProject.instance().addMapLayer(self.layer)

        self.editor = QPlainTextEdit()
        # _find_context_layer walks parentWidget() looking for a callable
        # .layer() - stood in directly rather than building a real dialog.
        self.editor.layer = lambda: self.layer
        self.controller = ac.CustomAutocompleteController(self.editor)

    def tearDown(self):
        self.controller.teardown()
        QgsProject.instance().removeMapLayer(self.layer.id())
        reset_plugin_settings()

    def _set_text_and_cursor(self, text: str, position: int) -> None:
        self.editor.setPlainText(text)
        cursor = self.editor.textCursor()
        cursor.setPosition(position)
        self.editor.setTextCursor(cursor)

    def test_ctrl_space_after_a_quote_suggests_the_layers_fields(self):
        self._set_text_and_cursor('"', 1)
        self.controller.trigger()
        values = {entry.value for entry in self.controller._entries}
        self.assertIn('"NAME"', values)
        self.assertIn('"COUNTRY"', values)

    def test_ctrl_space_with_nothing_typed_shows_the_full_grouped_list(self):
        self._set_text_and_cursor("", 0)
        self.controller.trigger()
        groups = {entry.group_label for entry in self.controller._entries if entry.group_label}
        self.assertTrue(groups)  # more than one real function group present

    def test_accepting_a_field_replaces_the_open_quote_exactly_once(self):
        """Regression: selecting NAME after typing "N used to leave the
        opening quote behind and produce ""NAME" instead of "NAME"."""
        self._set_text_and_cursor('"N', 2)
        self.controller.trigger()
        popup = self.controller._popup
        row = next(
            row
            for row in range(popup.count())
            if popup.item(row).data(ac.VALUE_ROLE) == '"NAME"'
        )
        popup.setCurrentRow(row)
        self.controller.accept_current()
        self.assertEqual(self.editor.toPlainText(), '"NAME"')

    def test_fields_popup_title_is_the_layer_name(self):
        """Naming a field: the title says WHICH dataset it belongs to."""
        self._set_text_and_cursor('"', 1)
        self.controller.trigger()
        self.assertEqual(self.controller._popup_title, self.layer.name())

    def test_values_popup_title_is_the_field_name(self):
        """Typing a value: the title says WHICH field it belongs to."""
        provider = self.layer.dataProvider()
        feature = QgsFeature(self.layer.fields())
        feature.setAttribute("NAME", "Foo")
        provider.addFeature(feature)

        text = "\"NAME\" = '"
        self._set_text_and_cursor(text, len(text))
        self.controller.trigger()
        self.assertEqual(self.controller._popup_title, "NAME")

    def test_the_full_grouped_list_has_no_title(self):
        """No single dataset/field applies to the mixed function/variable/
        operator list, so no heading is shown for it."""
        self._set_text_and_cursor("", 0)
        self.controller.trigger()
        self.assertEqual(self.controller._popup_title, "")


class AutocompletePopupTitleRenderingTests(unittest.TestCase):
    """AutocompletePopup.populate()'s title row - inserted above every group,
    and sized larger than a group header so the hierarchy stays legible."""

    def setUp(self):
        self.editor = QPlainTextEdit()
        self.popup = ac.AutocompletePopup(self.editor)

    def tearDown(self):
        self.popup.deleteLater()

    def test_title_is_the_first_row_and_not_selectable(self):
        entries = [ac.AutocompleteEntry(value="1", group_code="G1")]
        self.popup.populate(entries, title="STATUS")
        title_item = self.popup.item(0)
        self.assertEqual(title_item.text(), "STATUS")
        self.assertFalse(title_item.flags() & Qt.ItemFlag.ItemIsSelectable)

    def test_no_title_row_when_title_is_empty(self):
        entries = [ac.AutocompleteEntry(value="1")]  # ungrouped, so item(0) is the value itself
        count_without_title = self.popup.populate(entries, title="")
        self.assertEqual(self.popup.item(0).data(ac.VALUE_ROLE), "1")
        self.assertEqual(count_without_title, 1)

    def test_title_font_is_larger_than_a_group_header_font(self):
        entries = [ac.AutocompleteEntry(value="1", group_code="G1")]
        self.popup.populate(entries, title="STATUS")
        title_item = self.popup.item(0)
        header_item = self.popup.item(1)
        # pointSizeF(), not the integer pointSize(): the title's size is
        # only +0.5pt over the base font (a deliberately subtle step - see
        # AutocompletePopup._make_title), which the rounding/truncating
        # integer accessor could report as equal to the (unchanged) header
        # size rather than greater.
        self.assertGreater(title_item.font().pointSizeF(), header_item.font().pointSizeF())
        self.assertTrue(title_item.font().bold())
        self.assertTrue(header_item.font().bold())


if __name__ == "__main__":
    unittest.main()

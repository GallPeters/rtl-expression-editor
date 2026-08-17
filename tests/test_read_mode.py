# -*- coding: utf-8 -*-
"""Read mode: code/description substitution, ambiguous-code handling, and the
mapping built from a configured lookup layer."""

import unittest

from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QPlainTextEdit

from _rtl_plugin import rtl_readmode as rm
from _rtl_plugin.rtl_settings import Settings

from .utils import make_context_layer, make_lookup_layer, reset_plugin_settings


class NormalizeCodeTests(unittest.TestCase):
    def test_strips_one_matching_pair_of_quotes(self):
        self.assertEqual(rm.normalize_code("'farm'"), "farm")
        self.assertEqual(rm.normalize_code('"farm"'), "farm")
        self.assertEqual(rm.normalize_code(" '610' "), "610")

    def test_leaves_an_unquoted_or_mismatched_value_untouched(self):
        self.assertEqual(rm.normalize_code("farm"), "farm")
        self.assertEqual(rm.normalize_code("'farm\""), "'farm\"")


class SubstituteDescriptionsTests(unittest.TestCase):
    def test_replaces_every_mapped_code_with_its_description(self):
        mapping = {"status": {"1": ["Active"], "2": ["Inactive"]}}
        expr = "\"STATUS\" = '1' OR \"STATUS\" = '2'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping),
            '"STATUS" = Active OR "STATUS" = Inactive',
        )

    def test_leaves_unmapped_text_untouched(self):
        expr = "\"OTHER\" = '610'"
        self.assertEqual(rm.substitute_descriptions(expr, {}), expr)

    def test_only_the_field_the_literal_follows_is_substituted(self):
        # "OTHER" = '610' must stay untouched even though "F_ATT" also has a
        # meaning for 610 elsewhere in the same expression.
        mapping = {"f_att": {"610": ["mosque"]}}
        expr = "\"OTHER\" = '610' AND \"F_ATT\" = '610'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping),
            "\"OTHER\" = '610' AND \"F_ATT\" = mosque",
        )

    def test_an_ambiguous_code_shows_every_meaning_until_one_is_chosen(self):
        mapping = {"code": {"610": ["mosque", "greenhouse"]}}
        expr = "\"CODE\" = '610'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping), '"CODE" = mosque / greenhouse'
        )

    def test_a_remembered_choice_resolves_the_ambiguous_code(self):
        # ChoiceMemory.remember() writes into the ACTIVE QgsProject's custom
        # properties - snapshot and restore that one entry, so this leaves no
        # residue in a real project if run from inside a live QGIS session.
        project = QgsProject.instance()
        original, existed = project.readEntry("rtl_bidi_editor", "value_choices", "")
        try:
            rm.ChoiceMemory.remember("mytable", "code", "610", "greenhouse", 0, "ctx")
            mapping = {"code": {"610": ["mosque", "greenhouse"]}}
            expr = "\"CODE\" = '610'"
            self.assertEqual(
                rm.substitute_descriptions(expr, mapping, "mytable", "ctx"),
                '"CODE" = greenhouse',
            )
        finally:
            if existed:
                project.writeEntry("rtl_bidi_editor", "value_choices", original)
            else:
                project.removeEntry("rtl_bidi_editor", "value_choices")
            rm.ChoiceMemory.invalidate()

    def test_field_names_are_annotated_with_their_configured_description(self):
        expr = "\"TYPE\" = '1'"
        result = rm.substitute_descriptions(
            expr, {}, field_descriptions={"type": "סוג"}
        )
        self.assertEqual(result, "\"TYPE\" (סוג) = '1'")

    def test_a_field_with_no_configured_description_is_left_untouched(self):
        expr = "\"TYPE\" = '1'"
        result = rm.substitute_descriptions(expr, {}, field_descriptions={"other": "x"})
        self.assertEqual(result, expr)

    def test_alt_mode_renders_the_alternative_description(self):
        mapping = {"status": {"1": ["Active"]}}
        alt_mapping = {"status": {"1": {"Active": "פעיל"}}}
        expr = "\"STATUS\" = '1'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping, mode="alt", alt_mapping=alt_mapping),
            '"STATUS" = פעיל',
        )

    def test_alt_mode_falls_back_to_the_primary_description_when_none_is_set(self):
        """A row with no alternative of its own must still render something
        sensible in alternative mode, rather than nothing."""
        mapping = {"status": {"1": ["Active"]}}
        expr = "\"STATUS\" = '1'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping, mode="alt", alt_mapping={}),
            '"STATUS" = Active',
        )

    def test_alt_mode_respects_a_remembered_choice_among_several_meanings(self):
        project = QgsProject.instance()
        original, existed = project.readEntry("rtl_bidi_editor", "value_choices", "")
        try:
            rm.ChoiceMemory.remember("mytable", "code", "610", "greenhouse", 0, "ctx")
            mapping = {"code": {"610": ["mosque", "greenhouse"]}}
            alt_mapping = {"code": {"610": {"mosque": "מסגד", "greenhouse": "חממה"}}}
            expr = "\"CODE\" = '610'"
            self.assertEqual(
                rm.substitute_descriptions(
                    expr, mapping, "mytable", "ctx", mode="alt", alt_mapping=alt_mapping
                ),
                '"CODE" = חממה',
            )
        finally:
            if existed:
                project.writeEntry("rtl_bidi_editor", "value_choices", original)
            else:
                project.removeEntry("rtl_bidi_editor", "value_choices")
            rm.ChoiceMemory.invalidate()


class OccurrenceIndexTests(unittest.TestCase):
    """occurrence_index(text_before, ...) counts matches WITHIN whatever
    slice it is given - callers pass the text up to (not including) the
    literal being resolved, so that slice is what these tests build too."""

    def test_counts_earlier_occurrences_of_the_same_field_and_code(self):
        text = "\"CODE\" = '610' OR \"CODE\" = '610'"
        first_starts_at = text.index("'610'")
        second_starts_at = text.rindex("'610'")
        self.assertEqual(rm.occurrence_index(text[:first_starts_at], "CODE", "610"), 0)
        self.assertEqual(rm.occurrence_index(text[:second_starts_at], "CODE", "610"), 1)

    def test_does_not_count_a_different_field_with_the_same_code(self):
        text = "\"OTHER\" = '610' AND \"CODE\" = '610'"
        second_starts_at = text.rindex("'610'")
        # "OTHER" = '610' precedes the point of insertion, but it must never
        # be mistaken for an earlier "CODE" occurrence.
        self.assertEqual(rm.occurrence_index(text[:second_starts_at], "CODE", "610"), 0)


class DescriptionResolverTests(unittest.TestCase):
    def setUp(self):
        reset_plugin_settings()
        self.layer = make_lookup_layer()
        QgsProject.instance().addMapLayer(self.layer)
        Settings.set_layer_id(self.layer.id())
        Settings.set_field("field_names", "field_name")
        Settings.set_field("value", "value")
        Settings.set_field("description", "description")
        Settings.set_field("table", "table")
        rm.DescriptionResolver.invalidate()

    def tearDown(self):
        reset_plugin_settings()
        # Only the layer this test added - never every layer in the project,
        # which could be the user's own if this suite is run from inside a
        # live QGIS session.
        QgsProject.instance().removeMapLayer(self.layer.id())
        rm.DescriptionResolver.invalidate()

    def test_mapping_builds_field_to_code_to_descriptions(self):
        values, _alt_values, _field_descriptions = rm.DescriptionResolver.mapping(["context"])
        self.assertIn("status", values)
        self.assertEqual(values["status"]["1"], ["Active"])

    def test_mapping_is_empty_without_a_description_or_field_description_configured(self):
        Settings.set_field("description", "")
        Settings.set_field("field_description", "")
        rm.DescriptionResolver.invalidate()
        self.assertEqual(rm.DescriptionResolver.mapping(["context"]), ({}, {}, {}))

    def test_mapping_reads_the_alternative_description_column_when_configured(self):
        Settings.set_field("alt_description", "alt_description")
        rm.DescriptionResolver.invalidate()
        _values, alt_values, _field_descriptions = rm.DescriptionResolver.mapping(["context"])
        self.assertEqual(alt_values["status"]["1"], {"Active": "פעיל"})
        # COUNTRY/IL deliberately has no alt_description in the fixture - it
        # must simply be absent, not present with an empty string.
        self.assertNotIn("Israel", alt_values.get("country", {}).get("il", {}))

    def test_mapping_reads_the_field_description_column_when_configured(self):
        Settings.set_field("field_description", "field_description")
        rm.DescriptionResolver.invalidate()
        _values, _alt_values, field_descriptions = rm.DescriptionResolver.mapping(["context"])
        self.assertEqual(field_descriptions["status"], "מצב")
        self.assertEqual(field_descriptions["country"], "מדינה")

    def test_field_descriptions_are_available_even_with_no_value_description_column(self):
        """Field-name annotation is useful on its own, without a primary
        value-description column configured at all."""
        Settings.set_field("description", "")
        Settings.set_field("field_description", "field_description")
        rm.DescriptionResolver.invalidate()
        values, _alt_values, field_descriptions = rm.DescriptionResolver.mapping(["context"])
        self.assertEqual(values, {})
        self.assertEqual(field_descriptions["status"], "מצב")

    def test_mapping_cache_is_dropped_when_the_lookup_layers_data_changes(self):
        """Regression: editing the lookup table directly (e.g. filling in a
        newly-added alternative-description column) must be reflected
        without needing a settings change or a project reload.

        Edited through the layer's own edit-buffer API (startEditing/
        addFeature/commitChanges), not the data provider directly - that is
        what reliably emits the layer-level signals DescriptionResolver
        listens for, exactly as a real edit in the attribute table would.
        """
        rm.DescriptionResolver.mapping(["context"])  # populate the cache once

        from qgis.core import QgsFeature

        self.layer.startEditing()
        feature = QgsFeature(self.layer.fields())
        feature.setAttribute("field_name", "NEWFIELD")
        feature.setAttribute("value", "9")
        feature.setAttribute("description", "Nine")
        feature.setAttribute("table", "context")
        self.layer.addFeature(feature)
        self.layer.commitChanges()

        values, _alt, _field_desc = rm.DescriptionResolver.mapping(["context"])
        self.assertIn("newfield", values)


class AlternativeDescriptionAutomaticEnrichmentTests(unittest.TestCase):
    """The key requirement: a choice remembered BEFORE an alternative
    description column existed must render correctly in alternative mode as
    soon as the column is configured - with no re-selection and no
    migration, because the alternative is always looked up fresh by the
    remembered PRIMARY description text, never stored alongside the choice
    itself. See ChoiceMemory and rm._pick_label()."""

    def setUp(self):
        reset_plugin_settings()
        self.layer = make_lookup_layer()
        QgsProject.instance().addMapLayer(self.layer)
        Settings.set_layer_id(self.layer.id())
        Settings.set_field("field_names", "field_name")
        Settings.set_field("value", "value")
        Settings.set_field("description", "description")
        Settings.set_field("table", "table")
        rm.DescriptionResolver.invalidate()

        self.project = QgsProject.instance()
        self.original_choices, self.existed = self.project.readEntry(
            "rtl_bidi_editor", "value_choices", ""
        )

    def tearDown(self):
        if self.existed:
            self.project.writeEntry("rtl_bidi_editor", "value_choices", self.original_choices)
        else:
            self.project.removeEntry("rtl_bidi_editor", "value_choices")
        rm.ChoiceMemory.invalidate()
        reset_plugin_settings()
        QgsProject.instance().removeMapLayer(self.layer.id())
        rm.DescriptionResolver.invalidate()

    def test_a_choice_remembered_before_the_alt_column_existed_still_renders_in_alt_mode(self):
        # STATUS has two meanings for a made-up ambiguous code - remember the
        # user's choice exactly as the popup would, with no alt_description
        # column configured at all yet.
        rm.ChoiceMemory.remember("context", "status", "1", "Active", 0, "ctx")
        values, alt_values, _field_desc = rm.DescriptionResolver.mapping(["context"])
        label = rm._pick_label(
            values["status"]["1"], "context", "status", "1", 0, "ctx", mode="alt",
            alt_for_code=alt_values.get("status", {}).get("1", {}),
        )
        # No alt column yet - falls back to the primary description.
        self.assertEqual(label, "Active")

        # Now the alt_description column is configured - no re-selection,
        # no migration, nothing touched in the remembered choice itself.
        Settings.set_field("alt_description", "alt_description")
        rm.DescriptionResolver.invalidate()
        values, alt_values, _field_desc = rm.DescriptionResolver.mapping(["context"])
        label = rm._pick_label(
            values["status"]["1"], "context", "status", "1", 0, "ctx", mode="alt",
            alt_for_code=alt_values.get("status", {}).get("1", {}),
        )
        self.assertEqual(label, "פעיל")

    def test_editing_the_primary_description_changes_the_alt_rendering_too(self):
        """"Look at the alternative description as a meta-description of the
        description" - if the underlying row's own description text is what
        changes, the alternative resolved for it changes right along with it,
        since both are read fresh from the same row every time."""
        Settings.set_field("alt_description", "alt_description")
        rm.DescriptionResolver.invalidate()

        field_index = self.layer.fields().indexOf("description")
        alt_index = self.layer.fields().indexOf("alt_description")
        self.layer.startEditing()
        for feature in self.layer.getFeatures():
            if feature["value"] == "1" and feature["field_name"] == "STATUS":
                self.layer.changeAttributeValue(feature.id(), field_index, "Enabled")
                self.layer.changeAttributeValue(feature.id(), alt_index, "מאופשר")
                break
        self.layer.commitChanges()

        values, alt_values, _field_desc = rm.DescriptionResolver.mapping(["context"])
        self.assertIn("Enabled", values["status"]["1"])
        self.assertEqual(alt_values["status"]["1"]["Enabled"], "מאופשר")


class SlideSwitchTests(unittest.TestCase):
    """The mode-cycling widget itself: 2 or 3 positions, one click always
    advancing to the next, wrapping back to the first after the last."""

    def test_defaults_to_two_modes_and_starts_at_edit(self):
        switch = rm.SlideSwitch()
        try:
            self.assertEqual(switch.mode(), 0)
            switch.click()
            self.assertEqual(switch.mode(), 1)
            switch.click()
            self.assertEqual(switch.mode(), 0)  # wraps back to edit
        finally:
            switch.deleteLater()

    def test_three_modes_cycle_through_all_three_before_wrapping(self):
        switch = rm.SlideSwitch(mode_count=3)
        try:
            seen = [switch.mode()]
            for _ in range(3):
                switch.click()
                seen.append(switch.mode())
            self.assertEqual(seen, [0, 1, 2, 0])
        finally:
            switch.deleteLater()

    def test_set_mode_count_clamps_the_current_mode_down_if_needed(self):
        switch = rm.SlideSwitch(mode_count=3)
        try:
            switch.setMode(2)
            switch.setModeCount(2)
            self.assertEqual(switch.mode(), 1)  # clamped into the new range
        finally:
            switch.deleteLater()

    def test_mode_changed_signal_fires_with_the_new_mode(self):
        switch = rm.SlideSwitch(mode_count=3)
        seen = []
        switch.modeChanged.connect(seen.append)
        try:
            switch.click()
            switch.click()
            self.assertEqual(seen, [1, 2])
        finally:
            switch.deleteLater()


class ReadModeControllerModeCountTests(unittest.TestCase):
    """The switch offers 2 modes normally, 3 once an alternative value
    description column is configured - and clicking through them actually
    swaps which substitution is shown."""

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
        Settings.set_field("table", "table")
        rm.DescriptionResolver.invalidate()

    def tearDown(self):
        rm.DescriptionResolver.invalidate()
        QgsProject.instance().removeMapLayers([self.context_layer.id(), self.lookup_layer.id()])
        reset_plugin_settings()

    def _make_editor(self, text: str) -> QPlainTextEdit:
        editor = QPlainTextEdit()
        editor.layer = lambda: self.context_layer
        editor.setPlainText(text)
        return editor

    def test_two_modes_without_an_alt_description_column(self):
        editor = self._make_editor("\"STATUS\" = '1'")
        controller = rm.ReadModeController(editor)
        try:
            self.assertIsNotNone(controller._switch)
            self.assertEqual(controller._switch._mode_count, 2)
        finally:
            controller.teardown()

    def test_three_modes_once_an_alt_description_column_is_configured(self):
        Settings.set_field("alt_description", "alt_description")
        editor = self._make_editor("\"STATUS\" = '1'")
        controller = rm.ReadModeController(editor)
        try:
            self.assertEqual(controller._switch._mode_count, 3)
        finally:
            controller.teardown()

    def test_cycling_through_the_three_modes_shows_primary_then_alt_then_restores(self):
        Settings.set_field("alt_description", "alt_description")
        original = "\"STATUS\" = '1'"
        editor = self._make_editor(original)
        controller = rm.ReadModeController(editor)
        try:
            controller._switch.click()  # edit -> read
            self.assertIn("Active", editor.toPlainText())
            self.assertTrue(editor.isReadOnly())

            controller._switch.click()  # read -> alternative read
            self.assertIn("פעיל", editor.toPlainText())
            self.assertTrue(editor.isReadOnly())

            controller._switch.click()  # alternative read -> edit
            self.assertEqual(editor.toPlainText(), original)
            self.assertFalse(editor.isReadOnly())
        finally:
            controller.teardown()


if __name__ == "__main__":
    unittest.main()

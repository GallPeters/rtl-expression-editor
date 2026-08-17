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

    def test_field_names_are_replaced_by_their_configured_description(self):
        """Like a value's code, the field name disappears entirely in
        favour of its description - no quotes, no parentheses."""
        expr = "\"TYPE\" = '1'"
        result = rm.substitute_descriptions(
            expr, {}, field_descriptions={"type": "סוג"}
        )
        self.assertEqual(result, "סוג = '1'")

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


class ChoiceMemoryAltDescriptionPersistenceTests(unittest.TestCase):
    """Requirement: a value/description pair a colleague already selected
    with v1.4 must never be deleted or altered by v1.5 - only enriched with
    an automatically-added alternative description, stored as a SIBLING
    entry inside the very same project property (see
    ChoiceMemory._ALT_SUFFIX), so that:

    * v1.4 (with no notion of an alternative at all) keeps reading exactly
      the same primary description it always did, unaware the extra entry
      even exists;
    * v1.4 saving the project again later (e.g. after making some other,
      unrelated new choice) does not drop the alternative - its own
      remember() merges into the whole stored dict rather than replacing
      it, so an entry it does not understand simply survives untouched;
    * a v1.5 install opening the same project later - or the same install,
      right after entering Alternative Read mode - has the alternative
      available with nothing to reselect.
    """

    SCOPE = "rtl_bidi_editor"
    KEY = "value_choices"

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
        self.original_choices, self.existed = self.project.readEntry(self.SCOPE, self.KEY, "")

    def tearDown(self):
        if self.existed:
            self.project.writeEntry(self.SCOPE, self.KEY, self.original_choices)
        else:
            self.project.removeEntry(self.SCOPE, self.KEY)
        rm.ChoiceMemory.invalidate()
        reset_plugin_settings()
        QgsProject.instance().removeMapLayer(self.layer.id())
        rm.DescriptionResolver.invalidate()

    def _v14_style_load(self):
        """Mirrors exactly what v1.4's own ChoiceMemory._load() does - a
        plain, blind ``str(v)`` over every value in the stored dict, with no
        awareness that some keys might carry an alternative description."""
        import json

        raw, ok = self.project.readEntry(self.SCOPE, self.KEY, "")
        if not (ok and raw):
            return {}
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()}

    def _enter_alt_mode_for_status_1(self):
        """Stands in for the user actually switching to Alternative Read
        mode - the trigger that resolves (and persists) the alternative."""
        values, alt_values, _field_desc = rm.DescriptionResolver.mapping(["context"])
        return rm._pick_label(
            values["status"]["1"], "context", "status", "1", 0, "ctx", mode="alt",
            alt_for_code=alt_values.get("status", {}).get("1", {}),
        )

    def test_a_v14_style_choice_survives_untouched_after_alt_is_persisted(self):
        # A colleague on v1.4 picked "Active" for STATUS/1 from the popup -
        # the ordinary remember() call, unchanged since v1.4.
        rm.ChoiceMemory.remember("context", "status", "1", "Active", 0, "ctx")
        self.assertEqual(rm.ChoiceMemory.recall("context", "status", "1", 0, "ctx"), "Active")

        # The same project opened with v1.5, which now has an
        # alt_description column configured - entering Alternative Read
        # mode is what triggers the automatic persistence.
        Settings.set_field("alt_description", "alt_description")
        rm.DescriptionResolver.invalidate()
        label = self._enter_alt_mode_for_status_1()
        self.assertEqual(label, "פעיל")

        # The primary choice is completely unaffected...
        self.assertEqual(rm.ChoiceMemory.recall("context", "status", "1", 0, "ctx"), "Active")
        # ...and the alternative is now persisted too.
        self.assertEqual(rm.ChoiceMemory.recall_alt("context", "status", "1", 0, "ctx"), "פעיל")

    def test_a_v14_install_reading_the_project_sees_only_the_plain_primary_description(self):
        """The critical backward-compatibility guarantee: whatever v1.4's
        own (unmodified) loading code would produce for the primary key must
        still be the plain description text - never a Python dict repr or
        anything else that would corrupt its own read mode."""
        rm.ChoiceMemory.remember("context", "status", "1", "Active", 0, "ctx")
        Settings.set_field("alt_description", "alt_description")
        rm.DescriptionResolver.invalidate()
        self._enter_alt_mode_for_status_1()

        v14_view = self._v14_style_load()
        primary_key = rm.ChoiceMemory._key("context", "status", "1", 0, "ctx")
        self.assertEqual(v14_view[primary_key], "Active")
        self.assertIsInstance(v14_view[primary_key], str)

    def test_a_v14_install_re_saving_the_project_does_not_drop_the_alternative(self):
        """v1.4's remember() loads the WHOLE dict, adds/updates its own key
        and writes the WHOLE dict back - so an entry it does not understand
        (the alternative) must still be there afterwards, unmodified by
        having passed through v1.4's own code."""
        rm.ChoiceMemory.remember("context", "status", "1", "Active", 0, "ctx")
        Settings.set_field("alt_description", "alt_description")
        rm.DescriptionResolver.invalidate()
        self._enter_alt_mode_for_status_1()
        self.assertEqual(rm.ChoiceMemory.recall_alt("context", "status", "1", 0, "ctx"), "פעיל")

        # Simulate v1.4 making an unrelated new choice elsewhere and saving.
        rm.ChoiceMemory.remember("context", "country", "IL", "Israel", 0, "ctx")

        self.assertEqual(rm.ChoiceMemory.recall("context", "status", "1", 0, "ctx"), "Active")
        self.assertEqual(rm.ChoiceMemory.recall_alt("context", "status", "1", 0, "ctx"), "פעיל")
        self.assertEqual(rm.ChoiceMemory.recall("context", "country", "IL", 0, "ctx"), "Israel")

    def test_no_alternative_is_persisted_for_a_value_that_was_never_actually_chosen(self):
        """Only ever enriches an EXISTING remembered pair - never invents
        one for a code the user simply typed by hand or never picked from
        the suggestion list."""
        Settings.set_field("alt_description", "alt_description")
        rm.DescriptionResolver.invalidate()
        self._enter_alt_mode_for_status_1()  # no remember() call beforehand
        self.assertEqual(rm.ChoiceMemory.recall_alt("context", "status", "1", 0, "ctx"), "")


class SlideSwitchTests(unittest.TestCase):
    """The mode-cycling widget itself: a genuine slider, not a click-to-
    advance toggle - tapping anywhere on the track jumps to the nearest
    position (including directly to the far end, skipping the middle one),
    and dragging follows the mouse live, snapping to the nearest position on
    release.

    Widget geometry is fixed at 38x18 (see SlideSwitch.__init__), so x=2 is
    solidly within the leftmost (edit) zone, x=36 the rightmost, and x=19
    the middle one for a 3-position switch - real synthetic QMouseEvents via
    QTest, not switch.click(), since a real slider is driven by mouse
    position, not a plain "clicked" signal.
    """

    @staticmethod
    def _tap(switch, x: int) -> None:
        from qgis.PyQt.QtCore import QPoint, Qt
        from qgis.PyQt.QtTest import QTest

        QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(x, 9))

    @staticmethod
    def _drag(switch, from_x: int, to_x: int) -> None:
        from qgis.PyQt.QtCore import QPoint, Qt
        from qgis.PyQt.QtTest import QTest

        QTest.mousePress(switch, Qt.MouseButton.LeftButton, pos=QPoint(from_x, 9))
        QTest.mouseMove(switch, QPoint(to_x, 9))
        QTest.mouseRelease(switch, Qt.MouseButton.LeftButton, pos=QPoint(to_x, 9))

    def test_defaults_to_two_modes_and_starts_at_edit(self):
        switch = rm.SlideSwitch()
        try:
            self.assertEqual(switch.mode(), 0)
        finally:
            switch.deleteLater()

    def test_tapping_the_right_edge_jumps_directly_to_the_last_mode(self):
        """Not just a forward step: a single tap can go straight from edit
        to alternative, skipping read entirely."""
        switch = rm.SlideSwitch(mode_count=3)
        try:
            self._tap(switch, 36)
            self.assertEqual(switch.mode(), 2)
        finally:
            switch.deleteLater()

    def test_tapping_the_left_edge_returns_directly_to_edit(self):
        switch = rm.SlideSwitch(mode_count=3)
        try:
            switch.setMode(2)
            self._tap(switch, 2)
            self.assertEqual(switch.mode(), 0)
        finally:
            switch.deleteLater()

    def test_dragging_back_and_forth_lands_on_the_nearest_mode_each_time(self):
        switch = rm.SlideSwitch(mode_count=3)
        try:
            self._drag(switch, 2, 36)  # edit -> alternative, in one slide
            self.assertEqual(switch.mode(), 2)
            self._drag(switch, 36, 2)  # alternative -> edit
            self.assertEqual(switch.mode(), 0)
            self._drag(switch, 2, 19)  # edit -> read
            self.assertEqual(switch.mode(), 1)
        finally:
            switch.deleteLater()

    def test_two_modes_only_ever_land_on_edit_or_read(self):
        switch = rm.SlideSwitch(mode_count=2)
        try:
            self._tap(switch, 36)
            self.assertEqual(switch.mode(), 1)
            self._tap(switch, 2)
            self.assertEqual(switch.mode(), 0)
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
            self._tap(switch, 19)
            self._tap(switch, 36)
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
            from qgis.PyQt.QtCore import QPoint, Qt
            from qgis.PyQt.QtTest import QTest

            switch = controller._switch

            QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(19, 9))  # edit -> read
            self.assertIn("Active", editor.toPlainText())
            self.assertTrue(editor.isReadOnly())

            QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(36, 9))  # read -> alt
            self.assertIn("פעיל", editor.toPlainText())
            self.assertTrue(editor.isReadOnly())

            QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(2, 9))  # alt -> edit
            self.assertEqual(editor.toPlainText(), original)
            self.assertFalse(editor.isReadOnly())
        finally:
            controller.teardown()

    def test_dragging_directly_from_edit_to_alternative_skips_read(self):
        """The slider is not limited to a fixed forward step - a single
        slide (or tap) from one end straight to the other must work."""
        Settings.set_field("alt_description", "alt_description")
        editor = self._make_editor("\"STATUS\" = '1'")
        controller = rm.ReadModeController(editor)
        try:
            from qgis.PyQt.QtCore import QPoint, Qt
            from qgis.PyQt.QtTest import QTest

            switch = controller._switch
            QTest.mousePress(switch, Qt.MouseButton.LeftButton, pos=QPoint(2, 9))
            QTest.mouseMove(switch, QPoint(36, 9))
            QTest.mouseRelease(switch, Qt.MouseButton.LeftButton, pos=QPoint(36, 9))

            self.assertEqual(switch.mode(), 2)
            self.assertIn("פעיל", editor.toPlainText())
        finally:
            controller.teardown()


if __name__ == "__main__":
    unittest.main()

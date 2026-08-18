# -*- coding: utf-8 -*-
"""Read mode: code/description substitution, ambiguous-code handling, and the
mapping built from a configured lookup layer."""

import unittest

from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QPlainTextEdit

from _rtl_plugin import rtl_readmode as rm
from _rtl_plugin.rtl_settings import Settings

from .utils import host_in_dialog, make_context_layer, make_lookup_layer, reset_plugin_settings


def iso(text: str) -> str:
    """Wrap ``text`` exactly the way substitute_descriptions() isolates a
    substituted label - see rtl_readmode._isolate() for why every label is
    individually wrapped in a bidi isolate rather than left as plain text."""
    return rm._FSI + text + rm._PDI


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
            f'"STATUS" = {iso("Active")} OR "STATUS" = {iso("Inactive")}',
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
            f"\"OTHER\" = '610' AND \"F_ATT\" = {iso('mosque')}",
        )

    def test_an_ambiguous_code_shows_every_meaning_until_one_is_chosen(self):
        # _pick_label() isolates each candidate before joining with " / ",
        # and substitute_descriptions() isolates its WHOLE returned label
        # again on top - a harmless, valid nested isolate, not a double
        # substitution - see _isolate().
        mapping = {"code": {"610": ["mosque", "greenhouse"]}}
        expr = "\"CODE\" = '610'"
        joined = f'{iso("mosque")} / {iso("greenhouse")}'
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping),
            f'"CODE" = {iso(joined)}',
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
                f'"CODE" = {iso("greenhouse")}',
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
        self.assertEqual(result, f"{iso('סוג')} = '1'")

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
            f'"STATUS" = {iso("פעיל")}',
        )

    def test_alt_mode_falls_back_to_the_primary_description_when_none_is_set(self):
        """A row with no alternative of its own must still render something
        sensible in alternative mode, rather than nothing."""
        mapping = {"status": {"1": ["Active"]}}
        expr = "\"STATUS\" = '1'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping, mode="alt", alt_mapping={}),
            f'"STATUS" = {iso("Active")}',
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
                f'"CODE" = {iso("חממה")}',
            )
        finally:
            if existed:
                project.writeEntry("rtl_bidi_editor", "value_choices", original)
            else:
                project.removeEntry("rtl_bidi_editor", "value_choices")
            rm.ChoiceMemory.invalidate()


class ForceLtrParagraphsTests(unittest.TestCase):
    """force_ltr_paragraphs() - pinning a read-mode preview's overall
    layout to left-to-right regardless of which script its first
    substituted token happens to be in.

    Qt derives a paragraph's bidi base direction from its own first STRONG
    character. A field description in Hebrew replacing what was originally
    the expression's first (LTR) token would otherwise become that first
    strong character and flip the WHOLE line's layout to right-to-left -
    not just that one word (which should read right-to-left - it is
    Hebrew), but the surrounding operator/parenthesis/list structure too,
    silently reordering it relative to the original expression's own
    left-to-right sequence. See RtlOverlayEditor's own comment on Qt's
    per-paragraph bidi resolution for the mechanism this works around.
    """

    def test_prepends_the_left_to_right_mark(self):
        result = rm.force_ltr_paragraphs("ישות = בית כנסת")
        self.assertEqual(result[0], rm._LRM)
        self.assertEqual(result[1:], "ישות = בית כנסת")

    def test_every_line_of_a_multi_line_expression_gets_its_own_mark(self):
        result = rm.force_ltr_paragraphs("ישות = בית כנסת\nמדינה = ישראל")
        lines = result.split("\n")
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(line[0], rm._LRM)

    def test_empty_text_is_left_alone(self):
        self.assertEqual(rm.force_ltr_paragraphs(""), "")

    def test_does_not_alter_any_visible_character(self):
        original = "\"F_CODE\" IN (2300, 2301)"
        result = rm.force_ltr_paragraphs(original)
        self.assertEqual(result.replace(rm._LRM, ""), original)

    def test_applied_after_substitution_the_fields_own_order_is_preserved(self):
        """The end-to-end scenario reported: a Hebrew field description
        replacing the field name must not cause the value description (or
        a whole IN-list) to visually reorder relative to it."""
        mapping = {"f_code": {"2300": ["בית כנסת"], "2301": ["מבנה חקלאי"]}}
        expr = "\"F_CODE\" IN (2300, 2301)"
        substituted = rm.substitute_descriptions(
            expr, mapping, field_descriptions={"f_code": "ישות"}
        )
        # Logical order must already be field-desc, then IN, then the list
        # in its original order - force_ltr_paragraphs() only pins how that
        # order is laid out visually, it must never change it.
        expected = f"{iso('ישות')} IN ({iso('בית כנסת')}, {iso('מבנה חקלאי')})"
        self.assertEqual(substituted, expected)

        preview = rm.force_ltr_paragraphs(substituted)
        self.assertEqual(preview, rm._LRM + expected)


class BidiIsolationTests(unittest.TestCase):
    """A single paragraph-level LRM (force_ltr_paragraphs) pins the overall
    line to left-to-right, but does NOT by itself stop two adjacent RTL
    labels - separated only by a neutral character like a comma or a space -
    from bidi-merging into one run and swapping order relative to each
    other. Each substituted label must be wrapped in its own bidi isolate
    (see rtl_readmode._isolate()) to prevent that, no matter how many labels
    sit next to each other or how deeply the expression nests.
    """

    def test_each_substituted_label_is_individually_isolated(self):
        mapping = {"f_code": {"2300": ["בית כנסת"], "2301": ["מבנה חקלאי"]}}
        result = rm.substitute_descriptions("\"F_CODE\" IN (2300, 2301)", mapping)
        self.assertEqual(
            result, f'"F_CODE" IN ({iso("בית כנסת")}, {iso("מבנה חקלאי")})'
        )

    def test_stripping_the_isolate_markers_recovers_the_original_left_to_right_order(self):
        """The decisive check: whatever the isolates do visually, the
        underlying LOGICAL sequence - field, operator, list in its original
        order - must exactly match the source expression's own order,
        regardless of how many RTL/LTR labels are involved or how they are
        nested."""
        mapping = {
            "f_code": {"2300": ["מבנה דת"], "2301": ["מבנה חקלאי"], "2302": ["בית ספר"]}
        }
        expr = "\"F_CODE\" IN (2300, 2301, 2302)"
        result = rm.substitute_descriptions(
            expr, mapping, field_descriptions={"f_code": "ישות"}
        )
        stripped = result.replace(rm._FSI, "").replace(rm._PDI, "")
        self.assertEqual(stripped, "ישות IN (מבנה דת, מבנה חקלאי, בית ספר)")

    def test_a_nested_expression_keeps_every_label_in_source_order(self):
        """No matter how much the expression is nested: two separate
        field/value clauses joined by AND, each independently substituted,
        must still read in their original left-to-right sequence once the
        isolate markers are stripped."""
        mapping = {
            "f_code": {"2300": ["מבנה דת"]},
            "f_type": {"1": ["פעיל"], "2": ["לא פעיל"]},
        }
        expr = "(\"F_CODE\" = 2300) AND (\"F_TYPE\" IN (1, 2))"
        result = rm.substitute_descriptions(
            expr, mapping, field_descriptions={"f_code": "ישות", "f_type": "סוג"}
        )
        stripped = result.replace(rm._FSI, "").replace(rm._PDI, "")
        self.assertEqual(stripped, "(ישות = מבנה דת) AND (סוג IN (פעיל, לא פעיל))")

    def test_ambiguous_meanings_joined_by_slash_are_each_isolated_too(self):
        mapping = {"code": {"610": ["מסגד", "חממה"]}}
        result = rm.substitute_descriptions("\"CODE\" = '610'", mapping)
        joined = f'{iso("מסגד")} / {iso("חממה")}'
        self.assertEqual(result, f'"CODE" = {iso(joined)}')
        stripped = result.replace(rm._FSI, "").replace(rm._PDI, "")
        self.assertEqual(stripped, '"CODE" = מסגד / חממה')


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


class ChoiceMemoryForgetTests(unittest.TestCase):
    """ChoiceMemory.forget() - the low-level primitive reconcile_choices()
    builds on: removes one occurrence's primary AND alternative entries."""

    def setUp(self):
        self.project = QgsProject.instance()
        self.original, self.existed = self.project.readEntry("rtl_bidi_editor", "value_choices", "")

    def tearDown(self):
        if self.existed:
            self.project.writeEntry("rtl_bidi_editor", "value_choices", self.original)
        else:
            self.project.removeEntry("rtl_bidi_editor", "value_choices")
        rm.ChoiceMemory.invalidate()

    def test_forget_removes_both_the_primary_and_alternative_entry(self):
        rm.ChoiceMemory.remember("t", "f", "1", "Active", 0, "ctx")
        rm.ChoiceMemory.remember_alt("t", "f", "1", 0, "ctx", "פעיל")
        self.assertEqual(rm.ChoiceMemory.recall("t", "f", "1", 0, "ctx"), "Active")
        self.assertEqual(rm.ChoiceMemory.recall_alt("t", "f", "1", 0, "ctx"), "פעיל")

        rm.ChoiceMemory.forget("t", "f", "1", 0, "ctx")

        self.assertEqual(rm.ChoiceMemory.recall("t", "f", "1", 0, "ctx"), "")
        self.assertEqual(rm.ChoiceMemory.recall_alt("t", "f", "1", 0, "ctx"), "")

    def test_forgetting_a_never_remembered_occurrence_is_a_harmless_no_op(self):
        rm.ChoiceMemory.forget("t", "f", "1", 0, "ctx")  # must not raise
        self.assertEqual(rm.ChoiceMemory.recall("t", "f", "1", 0, "ctx"), "")

    def test_forget_does_not_disturb_a_different_occurrence(self):
        rm.ChoiceMemory.remember("t", "f", "1", "Active", 0, "ctx")
        rm.ChoiceMemory.remember("t", "f", "1", "Enabled", 1, "ctx")

        rm.ChoiceMemory.forget("t", "f", "1", 0, "ctx")

        self.assertEqual(rm.ChoiceMemory.recall("t", "f", "1", 0, "ctx"), "")
        self.assertEqual(rm.ChoiceMemory.recall("t", "f", "1", 1, "ctx"), "Enabled")


class ChoiceMemoryPurgeForLayerTests(unittest.TestCase):
    """ChoiceMemory.purge_for_layer() - the complementary cleanup for a
    layer removed from the project entirely, which reconcile_choices() has
    no way to notice on its own (it only runs when a specific expression's
    own dialog is accepted, not when the layer behind it disappears)."""

    def setUp(self):
        self.project = QgsProject.instance()
        self.original, self.existed = self.project.readEntry("rtl_bidi_editor", "value_choices", "")

    def tearDown(self):
        if self.existed:
            self.project.writeEntry("rtl_bidi_editor", "value_choices", self.original)
        else:
            self.project.removeEntry("rtl_bidi_editor", "value_choices")
        rm.ChoiceMemory.invalidate()

    def test_removes_every_entry_whose_context_references_the_layer(self):
        # expression_context_key() always appends the layer's id as the
        # last part of the context - mirrored here without needing a real
        # widget/window, since purge_for_layer() only ever does a plain
        # substring check.
        context = "QgsQueryBuilderBase|Query Builder|QgisApp|layer-abc-123"
        rm.ChoiceMemory.remember("t", "f_att", "610", "mosque", 0, context)
        rm.ChoiceMemory.remember_alt("t", "f_att", "610", 0, context, "مسجد")
        rm.ChoiceMemory.remember("t", "f_att", "610", "mosque", 1, context)

        removed = rm.ChoiceMemory.purge_for_layer("layer-abc-123")

        self.assertEqual(removed, 3)  # 2 primary entries + 1 alternative
        self.assertEqual(rm.ChoiceMemory.recall("t", "f_att", "610", 0, context), "")
        self.assertEqual(rm.ChoiceMemory.recall_alt("t", "f_att", "610", 0, context), "")
        self.assertEqual(rm.ChoiceMemory.recall("t", "f_att", "610", 1, context), "")

    def test_does_not_touch_a_different_layers_entries(self):
        context_a = "QgsQueryBuilderBase|Query Builder|QgisApp|layer-abc-123"
        context_b = "QgsQueryBuilderBase|Query Builder|QgisApp|layer-xyz-999"
        rm.ChoiceMemory.remember("t", "f_att", "610", "mosque", 0, context_a)
        rm.ChoiceMemory.remember("t", "f_att", "610", "greenhouse", 0, context_b)

        rm.ChoiceMemory.purge_for_layer("layer-abc-123")

        self.assertEqual(rm.ChoiceMemory.recall("t", "f_att", "610", 0, context_a), "")
        self.assertEqual(rm.ChoiceMemory.recall("t", "f_att", "610", 0, context_b), "greenhouse")

    def test_an_empty_or_unmatched_layer_id_is_a_harmless_no_op(self):
        rm.ChoiceMemory.remember("t", "f", "1", "Active", 0, "ctx")
        self.assertEqual(rm.ChoiceMemory.purge_for_layer(""), 0)
        self.assertEqual(rm.ChoiceMemory.purge_for_layer("no-such-layer"), 0)
        self.assertEqual(rm.ChoiceMemory.recall("t", "f", "1", 0, "ctx"), "Active")

    def test_forget_does_not_disturb_a_different_occurrence(self):
        rm.ChoiceMemory.remember("t", "f", "1", "Active", 0, "ctx")
        rm.ChoiceMemory.remember("t", "f", "1", "Enabled", 1, "ctx")

        rm.ChoiceMemory.forget("t", "f", "1", 0, "ctx")

        self.assertEqual(rm.ChoiceMemory.recall("t", "f", "1", 0, "ctx"), "")
        self.assertEqual(rm.ChoiceMemory.recall("t", "f", "1", 1, "ctx"), "Enabled")


class ReconcileChoicesTests(unittest.TestCase):
    """reconcile_choices() - rewriting ChoiceMemory so it exactly matches
    the current expression instead of only ever accumulating entries, or
    silently misattributing one occurrence's remembered choice to a
    DIFFERENT literal once editing shifts which one holds a given
    occurrence number.
    """

    CONTEXT = "ctx"
    TABLE = "mytable"

    def setUp(self):
        self.project = QgsProject.instance()
        self.original, self.existed = self.project.readEntry("rtl_bidi_editor", "value_choices", "")

    def tearDown(self):
        if self.existed:
            self.project.writeEntry("rtl_bidi_editor", "value_choices", self.original)
        else:
            self.project.removeEntry("rtl_bidi_editor", "value_choices")
        rm.ChoiceMemory.invalidate()

    def _recall(self, code, occurrence):
        return rm.ChoiceMemory.recall(self.TABLE, "code", code, occurrence, self.CONTEXT)

    def test_identical_text_is_a_no_op(self):
        rm.ChoiceMemory.remember(self.TABLE, "code", "610", "mosque", 0, self.CONTEXT)
        text = "\"CODE\" = 610"
        rm.reconcile_choices(self.CONTEXT, self.TABLE, text, text)
        self.assertEqual(self._recall("610", 0), "mosque")

    def test_an_earlier_insertion_moves_survivors_to_their_new_occurrence(self):
        """The exact scenario reported: inserting a new occurrence of the
        same field/code pair BEFORE existing ones must not leave the
        existing ones' remembered choices stranded under their old,
        now-wrong occurrence numbers."""
        rm.ChoiceMemory.remember(self.TABLE, "code", "610", "mosque", 0, self.CONTEXT)
        rm.ChoiceMemory.remember(self.TABLE, "code", "610", "greenhouse", 1, self.CONTEXT)

        before = '"CODE" = 610 AND "OTHER" = 1 AND "CODE" = 610'
        # A brand-new third occurrence inserted at the very start - both
        # previously-existing ones are still present, unchanged, just later.
        after = '"CODE" = 610 AND "CODE" = 610 AND "OTHER" = 1 AND "CODE" = 610'

        rm.reconcile_choices(self.CONTEXT, self.TABLE, before, after)

        self.assertEqual(self._recall("610", 0), "")  # the new occurrence - nothing chosen yet
        self.assertEqual(self._recall("610", 1), "mosque")  # carried from old occurrence 0
        self.assertEqual(self._recall("610", 2), "greenhouse")  # carried from old occurrence 1

    def test_removing_a_clause_deletes_its_choice_and_shifts_the_survivor_down(self):
        """The other half of the same scenario: removing the first of two
        occurrences must not leave the survivor showing the REMOVED one's
        description."""
        rm.ChoiceMemory.remember(self.TABLE, "code", "610", "mosque", 0, self.CONTEXT)
        rm.ChoiceMemory.remember(self.TABLE, "code", "610", "greenhouse", 1, self.CONTEXT)

        before = '"CODE" = 610 AND "OTHER" = 1 AND "CODE" = 610'
        after = '"OTHER" = 1 AND "CODE" = 610'  # the first "CODE" = 610 clause was deleted

        rm.reconcile_choices(self.CONTEXT, self.TABLE, before, after)

        self.assertEqual(self._recall("610", 0), "greenhouse")  # NOT "mosque" - that clause is gone
        self.assertEqual(self._recall("610", 1), "")  # no stale leftover for a slot that no longer exists

    def test_an_alternative_description_moves_along_with_its_primary(self):
        rm.ChoiceMemory.remember(self.TABLE, "code", "610", "mosque", 0, self.CONTEXT)
        rm.ChoiceMemory.remember_alt(self.TABLE, "code", "610", 0, self.CONTEXT, "مسجد")

        before = '"CODE" = 610'
        after = '"OTHER" = 1 AND "CODE" = 610'  # an unrelated clause inserted before it

        rm.reconcile_choices(self.CONTEXT, self.TABLE, before, after)

        self.assertEqual(self._recall("610", 0), "mosque")
        self.assertEqual(rm.ChoiceMemory.recall_alt(self.TABLE, "code", "610", 0, self.CONTEXT), "مسجد")

    def test_a_fully_removed_occurrence_with_no_survivor_at_all_is_deleted(self):
        rm.ChoiceMemory.remember(self.TABLE, "code", "610", "mosque", 0, self.CONTEXT)

        before = '"CODE" = 610'
        after = '"OTHER" = 1'  # the only occurrence is gone entirely

        rm.reconcile_choices(self.CONTEXT, self.TABLE, before, after)

        self.assertEqual(self._recall("610", 0), "")

    def test_unrelated_fields_are_left_alone(self):
        rm.ChoiceMemory.remember(self.TABLE, "country", "IL", "Israel", 0, self.CONTEXT)
        before = '"COUNTRY" = \'IL\''
        after = '"COUNTRY" = \'IL\' AND "STATUS" = 1'
        rm.reconcile_choices(self.CONTEXT, self.TABLE, before, after)
        self.assertEqual(
            rm.ChoiceMemory.recall(self.TABLE, "country", "IL", 0, self.CONTEXT), "Israel"
        )

    def test_fresh_choices_made_during_the_same_session_are_never_touched(self):
        """Regression: a brand-new filter built from scratch in one dialog
        session (baseline is empty, since nothing existed before it was
        opened) whose 3 values were picked from the popup DURING that same
        session must not have those choices wiped the instant OK is
        pressed, just because none of them existed in the (empty)
        baseline. The old version cleared range(max(old_count, new_count))
        unconditionally, which deleted exactly these."""
        before = ""
        after = "\"F_ATT\" = 'בית כנסת' OR \"F_ATT\" IN ('בית כנסת', 'בית כנסת')"
        # Exactly what accept_current() already wrote live, once per
        # occurrence, before OK is ever pressed.
        for occ in range(3):
            rm.ChoiceMemory.remember(self.TABLE, "f_att", "בית כנסת", "synagogue", occ, self.CONTEXT)

        rm.reconcile_choices(self.CONTEXT, self.TABLE, before, after)

        for occ in range(3):
            self.assertEqual(
                rm.ChoiceMemory.recall(self.TABLE, "f_att", "בית כנסת", occ, self.CONTEXT),
                "synagogue",
            )

    def test_clearing_the_whole_expression_removes_every_one_of_its_entries(self):
        rm.ChoiceMemory.remember(self.TABLE, "f_att", "בית כנסת", "synagogue", 0, self.CONTEXT)
        rm.ChoiceMemory.remember(self.TABLE, "f_att", "בית כנסת", "synagogue", 1, self.CONTEXT)
        rm.ChoiceMemory.remember(self.TABLE, "f_att", "בית כנסת", "synagogue", 2, self.CONTEXT)
        before = "\"F_ATT\" = 'בית כנסת' OR \"F_ATT\" IN ('בית כנסת', 'בית כנסת')"
        after = ""

        rm.reconcile_choices(self.CONTEXT, self.TABLE, before, after)

        for occ in range(3):
            self.assertEqual(
                rm.ChoiceMemory.recall(self.TABLE, "f_att", "בית כנסת", occ, self.CONTEXT), ""
            )

    def test_the_full_reported_sequence_build_clear_then_add_something_else(self):
        """End to end: build a 3-occurrence filter (values chosen live),
        accept it (all 3 must survive); clear the filter entirely, accept
        again (all 3 must be gone); add an unrelated clause, accept again
        (nothing resurrected, no crash)."""
        f_att_before = ""
        f_att_after = "\"F_ATT\" = 'בית כנסת' OR \"F_ATT\" IN ('בית כנסת', 'בית כנסת')"
        for occ in range(3):
            rm.ChoiceMemory.remember(self.TABLE, "f_att", "בית כנסת", "synagogue", occ, self.CONTEXT)
        rm.reconcile_choices(self.CONTEXT, self.TABLE, f_att_before, f_att_after)
        for occ in range(3):
            self.assertEqual(
                rm.ChoiceMemory.recall(self.TABLE, "f_att", "בית כנסת", occ, self.CONTEXT),
                "synagogue",
            )

        cleared = ""
        rm.reconcile_choices(self.CONTEXT, self.TABLE, f_att_after, cleared)
        for occ in range(3):
            self.assertEqual(
                rm.ChoiceMemory.recall(self.TABLE, "f_att", "בית כנסת", occ, self.CONTEXT), ""
            )

        with_new_clause = "\"F_CODE\" = 2301"
        rm.reconcile_choices(self.CONTEXT, self.TABLE, cleared, with_new_clause)
        for occ in range(3):
            self.assertEqual(
                rm.ChoiceMemory.recall(self.TABLE, "f_att", "בית כנסת", occ, self.CONTEXT), ""
            )


class ChoiceReconcilerTests(unittest.TestCase):
    """ChoiceReconciler - the wiring that actually triggers
    reconcile_choices() once, when the surrounding dialog is accepted."""

    def setUp(self):
        reset_plugin_settings()
        self.context_layer = make_context_layer(("CODE", "OTHER"))
        QgsProject.instance().addMapLayer(self.context_layer)
        self.project = QgsProject.instance()
        self.original, self.existed = self.project.readEntry("rtl_bidi_editor", "value_choices", "")

    def tearDown(self):
        if self.existed:
            self.project.writeEntry("rtl_bidi_editor", "value_choices", self.original)
        else:
            self.project.removeEntry("rtl_bidi_editor", "value_choices")
        rm.ChoiceMemory.invalidate()
        QgsProject.instance().removeMapLayer(self.context_layer.id())
        reset_plugin_settings()

    def _make_editor(self, text):
        editor = QPlainTextEdit()
        editor.layer = lambda: self.context_layer
        editor.setPlainText(text)
        dialog = host_in_dialog(editor)
        return editor, dialog

    def test_accepting_the_dialog_reconciles_using_the_attach_time_baseline(self):
        editor, dialog = self._make_editor('"CODE" = 610 AND "OTHER" = 1 AND "CODE" = 610')

        from _rtl_plugin.rtl_autocomplete import resolve_table_candidates

        tables = resolve_table_candidates(editor)
        table = tables[0] if tables else ""
        context = rm.expression_context_key(editor)
        rm.ChoiceMemory.remember(table, "code", "610", "mosque", 0, context)
        rm.ChoiceMemory.remember(table, "code", "610", "greenhouse", 1, context)

        reconciler = rm.ChoiceReconciler(editor)
        try:
            # Edited AFTER the reconciler captured its baseline, exactly as
            # a user would inside the real dialog before pressing OK.
            editor.setPlainText(
                '"CODE" = 610 AND "CODE" = 610 AND "OTHER" = 1 AND "CODE" = 610'
            )
            dialog.accept()  # emits QDialog.accepted

            self.assertEqual(
                rm.ChoiceMemory.recall(table, "code", "610", 1, context), "mosque"
            )
            self.assertEqual(
                rm.ChoiceMemory.recall(table, "code", "610", 2, context), "greenhouse"
            )
        finally:
            reconciler.teardown()
            dialog.deleteLater()

    def test_teardown_disconnects_so_a_later_accept_does_not_reconcile_again(self):
        editor, dialog = self._make_editor('"CODE" = 610')
        reconciler = rm.ChoiceReconciler(editor)
        reconciler.teardown()

        # Should not raise even though the reconciler is torn down and its
        # own editor reference is gone.
        dialog.accept()
        dialog.deleteLater()


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

    def test_read_mode_preview_is_pinned_left_to_right_when_a_field_description_leads(self):
        """End-to-end: a Hebrew field description replacing what was
        originally the expression's first (LTR) token must not flip the
        whole line's bidi layout - see force_ltr_paragraphs()."""
        Settings.set_field("field_description", "field_description")
        editor = self._make_editor("\"STATUS\" = '1'")
        controller = rm.ReadModeController(editor)
        try:
            controller._switch.setMode(1)  # edit -> read
            text = editor.toPlainText()
            self.assertEqual(text[0], rm._LRM)
            self.assertIn("מצב", text)  # STATUS's own field description
            self.assertIn("Active", text)
        finally:
            controller.teardown()


if __name__ == "__main__":
    unittest.main()

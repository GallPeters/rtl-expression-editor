# -*- coding: utf-8 -*-
"""Read mode: code/description substitution, ambiguous-code handling, and the
mapping built from a configured lookup layer."""

import unittest

from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QPlainTextEdit

from _rtl_plugin import rtl_readmode as rm
from _rtl_plugin.rtl_settings import Settings

from .utils import make_context_layer, make_lookup_layer, reset_plugin_settings


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
        mapping = {"status": {"1": [("Active", "")], "2": [("Inactive", "")]}}
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
        mapping = {"f_att": {"610": [("mosque", "")]}}
        expr = "\"OTHER\" = '610' AND \"F_ATT\" = '610'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping),
            f"\"OTHER\" = '610' AND \"F_ATT\" = {iso('mosque')}",
        )

    def test_an_ambiguous_code_shows_every_meaning_until_resolved(self):
        # _pick_label() isolates each candidate before joining with " / ",
        # and substitute_descriptions() isolates its WHOLE returned label
        # again on top - a harmless, valid nested isolate, not a double
        # substitution - see _isolate(). Neither candidate has a group
        # value here, so there is nothing to resolve it from.
        mapping = {"code": {"610": [("mosque", ""), ("greenhouse", "")]}}
        expr = "\"CODE\" = '610'"
        joined = f'{iso("mosque")} / {iso("greenhouse")}'
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping),
            f'"CODE" = {iso(joined)}',
        )

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
        mapping = {"status": {"1": [("Active", "")]}}
        alt_mapping = {"status": {"1": {"Active": "פעיל"}}}
        expr = "\"STATUS\" = '1'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping, mode="alt", alt_mapping=alt_mapping),
            f'"STATUS" = {iso("פעיל")}',
        )

    def test_alt_mode_falls_back_to_the_primary_description_when_none_is_set(self):
        """A row with no alternative of its own must still render something
        sensible in alternative mode, rather than nothing."""
        mapping = {"status": {"1": [("Active", "")]}}
        expr = "\"STATUS\" = '1'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping, mode="alt", alt_mapping={}),
            f'"STATUS" = {iso("Active")}',
        )


class GroupContextScanningTests(unittest.TestCase):
    """_scan_literals_with_scope() / _governing_values() - the low-level
    machinery _pick_label() uses to infer an ambiguous code's meaning from
    its own surrounding "AND" context in the SAME expression, replacing
    the need to remember anything at all. Two literals share a scope
    (one can supply the group for the other) exactly when one's path is a
    PREFIX of the other's - see _governing_values()'s own docstring."""

    def test_flat_and_siblings_share_the_same_scope_path(self):
        leaves = rm._scan_literals_with_scope('"F_CODE" = 2300 AND "F_ATT" = 610')
        by_field = {leaf.field: leaf for leaf in leaves}
        self.assertEqual(by_field["f_code"].path, ())
        self.assertEqual(by_field["f_att"].path, ())

    def test_a_literal_inside_parens_gets_a_deeper_path_than_one_outside(self):
        leaves = rm._scan_literals_with_scope('"F_CODE" = 2300 AND ("F_ATT" = 610)')
        by_field = {leaf.field: leaf for leaf in leaves}
        self.assertEqual(by_field["f_code"].path, ())
        self.assertEqual(len(by_field["f_att"].path), 1)

    def test_governing_values_includes_a_same_scope_sibling(self):
        leaves = rm._scan_literals_with_scope('"F_CODE" = 2300 AND "F_ATT" = 610')
        f_att = next(leaf for leaf in leaves if leaf.field == "f_att")
        governing = rm._governing_values(leaves, "f_att", f_att.path)
        self.assertIn("2300", governing)

    def test_governing_values_includes_an_enclosing_scopes_comparison(self):
        leaves = rm._scan_literals_with_scope('"F_CODE" = 2300 AND ("F_ATT" = 610)')
        f_att = next(leaf for leaf in leaves if leaf.field == "f_att")
        governing = rm._governing_values(leaves, "f_att", f_att.path)
        self.assertIn("2300", governing)

    def test_governing_values_reach_through_multiple_nested_levels(self):
        leaves = rm._scan_literals_with_scope('"F_CODE" = 2300 AND (("F_ATT" = 610))')
        target = next(leaf for leaf in leaves if leaf.field == "f_att")
        governing = rm._governing_values(leaves, "f_att", target.path)
        self.assertIn("2300", governing)

    def test_governing_values_never_includes_a_same_field_comparison(self):
        leaves = rm._scan_literals_with_scope('"F_ATT" = 611 AND "F_ATT" = 610')
        target = leaves[-1]
        governing = rm._governing_values(leaves, "f_att", target.path)
        self.assertNotIn("611", governing)

    def test_sibling_parenthesised_groups_never_govern_each_other(self):
        """Two unrelated parenthesised groups at the SAME level - each a
        distinct paren instance - must not leak context into one another,
        even with no explicit tracking of AND vs. OR: they simply never
        share a scope path at all."""
        leaves = rm._scan_literals_with_scope(
            '("F_CODE" = 2300 AND "F_ATT" = 610) OR ("F_CODE" = 1400 AND "F_ATT" = 611)'
        )
        first_att = next(leaf for leaf in leaves if leaf.field == "f_att" and leaf.code == "610")
        governing = rm._governing_values(leaves, "f_att", first_att.path)
        self.assertIn("2300", governing)
        self.assertNotIn("1400", governing)


class PickLabelGroupResolutionTests(unittest.TestCase):
    """_pick_label() - resolving an ambiguous code from the expression's
    own group context: exactly the two patterns described - a flat "AND"
    sibling comparison, or an enclosing scope's comparison governing
    everything inside a parenthesised group - falling back to showing
    every meaning whenever that is not conclusive."""

    def test_a_single_candidate_needs_no_group_context_at_all(self):
        label = rm._pick_label([("Active", "")], "status", (), [])
        self.assertEqual(label, "Active")

    def test_resolves_via_a_flat_and_sibling_comparison(self):
        text = '"F_CODE" = 2300 AND "F_ATT" = 610'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "f_att")
        candidates = [("mosque", "2300"), ("greenhouse", "1400")]
        label = rm._pick_label(candidates, "f_att", target.path, leaves)
        self.assertEqual(label, "mosque")

    def test_resolves_regardless_of_which_side_of_the_and_the_group_is_on(self):
        text = '"F_ATT" = 610 AND "F_CODE" = 2300'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "f_att")
        candidates = [("mosque", "2300"), ("greenhouse", "1400")]
        label = rm._pick_label(candidates, "f_att", target.path, leaves)
        self.assertEqual(label, "mosque")

    def test_resolves_via_an_enclosing_parenthesised_groups_comparison(self):
        text = '"F_CODE" = 2300 AND ("F_ATT" = 610)'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "f_att")
        candidates = [("mosque", "2300"), ("greenhouse", "1400")]
        label = rm._pick_label(candidates, "f_att", target.path, leaves)
        self.assertEqual(label, "mosque")

    def test_falls_back_to_every_meaning_when_no_group_matches(self):
        text = '"F_ATT" = 610'
        leaves = rm._scan_literals_with_scope(text)
        target = leaves[0]
        candidates = [("mosque", "2300"), ("greenhouse", "1400")]
        label = rm._pick_label(candidates, "f_att", target.path, leaves)
        self.assertEqual(label, f'{iso("mosque")} / {iso("greenhouse")}')

    def test_falls_back_to_every_meaning_when_more_than_one_group_matches(self):
        """Genuinely ambiguous - both candidates' groups happen to be
        present in the surrounding context - never guess between them."""
        text = '"F_CODE" = 2300 AND "OTHER_CODE" = 1400 AND "F_ATT" = 610'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "f_att")
        candidates = [("mosque", "2300"), ("greenhouse", "1400")]
        label = rm._pick_label(candidates, "f_att", target.path, leaves)
        self.assertEqual(label, f'{iso("mosque")} / {iso("greenhouse")}')

    def test_a_candidate_with_no_group_value_never_matches(self):
        text = '"F_CODE" = 2300 AND "F_ATT" = 610'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "f_att")
        candidates = [("mosque", ""), ("greenhouse", "1400")]
        label = rm._pick_label(candidates, "f_att", target.path, leaves)
        # "2300" governs, but neither candidate's own group is "2300".
        self.assertEqual(label, f'{iso("mosque")} / {iso("greenhouse")}')

    def test_alt_mode_resolves_the_same_way_then_renders_its_alternative(self):
        text = '"F_CODE" = 2300 AND "F_ATT" = 610'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "f_att")
        candidates = [("mosque", "2300"), ("greenhouse", "1400")]
        label = rm._pick_label(
            candidates, "f_att", target.path, leaves, mode="alt",
            alt_for_code={"mosque": "מסגד", "greenhouse": "חממה"},
        )
        self.assertEqual(label, "מסגד")


class SubstituteDescriptionsGroupResolutionTests(unittest.TestCase):
    """substitute_descriptions() end to end: an ambiguous code resolved
    straight from the expression's own group context - the mechanism that
    replaces remembering which meaning was chosen. Nothing here is ever
    written anywhere; the same expression re-rendered later resolves the
    same way again, straight from its own text and the lookup table."""

    def test_flat_and_pattern_resolves_the_ambiguous_code(self):
        mapping = {"f_att": {"610": [("mosque", "2300"), ("greenhouse", "1400")]}}
        expr = '"F_CODE" = 2300 AND "F_ATT" = 610'
        result = rm.substitute_descriptions(expr, mapping)
        self.assertEqual(result, f'"F_CODE" = 2300 AND "F_ATT" = {iso("mosque")}')

    def test_parenthesised_group_pattern_resolves_every_code_inside_it(self):
        mapping = {
            "f_att": {
                "610": [("mosque", "2300"), ("greenhouse", "1400")],
                "611": [("church", "2300"), ("barn", "1400")],
            }
        }
        expr = '"F_CODE" = 2300 AND ("F_ATT" = 610 OR "F_ATT" = 611)'
        result = rm.substitute_descriptions(expr, mapping)
        self.assertEqual(
            result,
            f'"F_CODE" = 2300 AND ("F_ATT" = {iso("mosque")} OR "F_ATT" = {iso("church")})',
        )

    def test_no_group_context_falls_back_to_showing_every_meaning(self):
        mapping = {"f_att": {"610": [("mosque", "2300"), ("greenhouse", "1400")]}}
        expr = '"F_ATT" = 610'
        result = rm.substitute_descriptions(expr, mapping)
        joined = f'{iso("mosque")} / {iso("greenhouse")}'
        self.assertEqual(result, f'"F_ATT" = {iso(joined)}')

    def test_two_separate_groups_each_resolve_their_own_ambiguous_code(self):
        """The same code appearing twice, under two different groups in
        two different parenthesised clauses, resolves independently and
        correctly each time - the whole point of reading it straight from
        each occurrence's own context instead of remembering one answer
        per code."""
        mapping = {"f_att": {"610": [("mosque", "2300"), ("greenhouse", "1400")]}}
        expr = (
            '("F_CODE" = 2300 AND "F_ATT" = 610) OR '
            '("F_CODE" = 1400 AND "F_ATT" = 610)'
        )
        result = rm.substitute_descriptions(expr, mapping)
        self.assertEqual(
            result,
            f'("F_CODE" = 2300 AND "F_ATT" = {iso("mosque")}) OR '
            f'("F_CODE" = 1400 AND "F_ATT" = {iso("greenhouse")})',
        )


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
        mapping = {"f_code": {"2300": [("בית כנסת", "")], "2301": [("מבנה חקלאי", "")]}}
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
        mapping = {"f_code": {"2300": [("בית כנסת", "")], "2301": [("מבנה חקלאי", "")]}}
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
            "f_code": {
                "2300": [("מבנה דת", "")],
                "2301": [("מבנה חקלאי", "")],
                "2302": [("בית ספר", "")],
            }
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
            "f_code": {"2300": [("מבנה דת", "")]},
            "f_type": {"1": [("פעיל", "")], "2": [("לא פעיל", "")]},
        }
        expr = "(\"F_CODE\" = 2300) AND (\"F_TYPE\" IN (1, 2))"
        result = rm.substitute_descriptions(
            expr, mapping, field_descriptions={"f_code": "ישות", "f_type": "סוג"}
        )
        stripped = result.replace(rm._FSI, "").replace(rm._PDI, "")
        self.assertEqual(stripped, "(ישות = מבנה דת) AND (סוג IN (פעיל, לא פעיל))")

    def test_ambiguous_meanings_joined_by_slash_are_each_isolated_too(self):
        mapping = {"code": {"610": [("מסגד", ""), ("חממה", "")]}}
        result = rm.substitute_descriptions("\"CODE\" = '610'", mapping)
        joined = f'{iso("מסגד")} / {iso("חממה")}'
        self.assertEqual(result, f'"CODE" = {iso(joined)}')
        stripped = result.replace(rm._FSI, "").replace(rm._PDI, "")
        self.assertEqual(stripped, '"CODE" = מסגד / חממה')


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
        Settings.set_field("group_code", "group_code")
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
        # STATUS/1 in the fixture carries group_code "G1" - see
        # test_mapping_captures_each_rows_group_code_alongside_its_description.
        self.assertEqual(values["status"]["1"], [("Active", "G1")])

    def test_mapping_captures_each_rows_group_code_alongside_its_description(self):
        """The same "Group codes column" already used to head a suggestion-
        list group doubles as what _pick_label() matches an ambiguous
        code's surrounding context against - see its own docstring."""
        values, _alt_values, _field_descriptions = rm.DescriptionResolver.mapping(["context"])
        self.assertEqual(values["status"]["2"], [("Inactive", "G1")])
        # COUNTRY/IL has no group_code in the fixture - recorded as "".
        self.assertEqual(values["country"]["IL"], [("Israel", "")])

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

    def test_read_mode_resolves_an_ambiguous_code_from_its_own_group_context(self):
        """End-to-end through the real controller and a real lookup layer:
        two rows sharing one code, distinguished by group_code, resolved
        correctly straight from the expression's own surrounding
        "F_CODE" = ... context - no choice remembered anywhere."""
        from .utils import make_layer
        from qgis.PyQt.QtCore import QVariant

        ambiguous_layer = make_layer(
            "None",
            "ambiguous_lookup",
            [
                ("field_name", QVariant.String),
                ("value", QVariant.String),
                ("description", QVariant.String),
                ("group_code", QVariant.String),
                ("table", QVariant.String),
            ],
            [
                {"field_name": "F_ATT", "value": "610", "description": "mosque", "group_code": "2300", "table": "context"},
                {"field_name": "F_ATT", "value": "610", "description": "greenhouse", "group_code": "1400", "table": "context"},
            ],
        )
        QgsProject.instance().addMapLayer(ambiguous_layer)
        Settings.set_layer_id(ambiguous_layer.id())
        Settings.set_field("group_code", "group_code")
        rm.DescriptionResolver.invalidate()
        try:
            editor = self._make_editor('"F_CODE" = 2300 AND "F_ATT" = 610')
            controller = rm.ReadModeController(editor)
            try:
                controller._switch.setMode(1)  # edit -> read
                self.assertIn("mosque", editor.toPlainText())
                self.assertNotIn("greenhouse", editor.toPlainText())
            finally:
                controller.teardown()
        finally:
            QgsProject.instance().removeMapLayer(ambiguous_layer.id())


class PurgeLegacyProjectEntriesTests(unittest.TestCase):
    """purge_legacy_project_entries() - the one-time cleanup run when the
    plugin activates (see RtlBidiEditorPlugin.initGui()), removing whatever
    remembered-choice entry an older install may have left behind. Nothing
    writes such an entry any more - an ambiguous code is resolved straight
    from the expression's own group context instead (see
    substitute_descriptions()/_pick_label())."""

    SCOPE = "rtl_bidi_editor"
    KEY = "value_choices"

    def setUp(self):
        self.project = QgsProject.instance()
        self.original, self.existed = self.project.readEntry(self.SCOPE, self.KEY, "")

    def tearDown(self):
        if self.existed:
            self.project.writeEntry(self.SCOPE, self.KEY, self.original)
        else:
            self.project.removeEntry(self.SCOPE, self.KEY)

    def test_removes_an_existing_legacy_entry_and_reports_true(self):
        self.project.writeEntry(self.SCOPE, self.KEY, '{"some": "stale entry"}')

        removed = rm.purge_legacy_project_entries()

        self.assertTrue(removed)
        _raw, still_there = self.project.readEntry(self.SCOPE, self.KEY, "")
        self.assertFalse(still_there)

    def test_a_project_with_no_legacy_entry_is_a_harmless_no_op(self):
        self.project.removeEntry(self.SCOPE, self.KEY)

        removed = rm.purge_legacy_project_entries()

        self.assertFalse(removed)


if __name__ == "__main__":
    unittest.main()

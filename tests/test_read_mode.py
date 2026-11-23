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
        self.assertEqual(rm.normalize_code(" '5' "), "5")

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
        expr = "\"OTHER\" = '5'"
        self.assertEqual(rm.substitute_descriptions(expr, {}), expr)

    def test_only_the_field_the_literal_follows_is_substituted(self):
        # "OTHER" = '5' must stay untouched even though "SUBTYPE" also has a
        # meaning for 5 elsewhere in the same expression.
        mapping = {"subtype": {"5": [("school", "")]}}
        expr = "\"OTHER\" = '5' AND \"SUBTYPE\" = '5'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping),
            f"\"OTHER\" = '5' AND \"SUBTYPE\" = {iso('school')}",
        )

    def test_an_ambiguous_code_shows_every_meaning_until_resolved(self):
        # _pick_label() isolates each candidate before joining with " / ",
        # and substitute_descriptions() isolates its WHOLE returned label
        # again on top - a harmless, valid nested isolate, not a double
        # substitution - see _isolate(). Neither candidate has a group
        # value here, so there is nothing to resolve it from.
        mapping = {"code": {"5": [("school", ""), ("warehouse", "")]}}
        expr = "\"CODE\" = '5'"
        joined = f'{iso("school")} / {iso("warehouse")}'
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
        leaves = rm._scan_literals_with_scope('"TYPE" = 20 AND "SUBTYPE" = 5')
        by_field = {leaf.field: leaf for leaf in leaves}
        self.assertEqual(by_field["type"].path, by_field["subtype"].path)

    def test_a_literal_inside_parens_gets_a_deeper_path_than_one_outside(self):
        leaves = rm._scan_literals_with_scope('"TYPE" = 20 AND ("SUBTYPE" = 5)')
        by_field = {leaf.field: leaf for leaf in leaves}
        self.assertLess(len(by_field["type"].path), len(by_field["subtype"].path))

    def test_a_bare_or_starts_a_fresh_run_at_its_own_scope_only(self):
        """The path element for the CURRENT scope changes across an OR -
        but an ancestor's does not, since the OR sits deeper than it."""
        leaves = rm._scan_literals_with_scope('"TYPE" = 20 OR "SUBTYPE" = 5')
        by_field = {leaf.field: leaf for leaf in leaves}
        self.assertNotEqual(by_field["type"].path, by_field["subtype"].path)

        leaves = rm._scan_literals_with_scope(
            '"TYPE" = 20 AND ("COUNTRY" = 1 OR "COUNTRY" = 2)'
        )
        type = next(leaf for leaf in leaves if leaf.field == "type")
        countries = [leaf for leaf in leaves if leaf.field == "country"]
        self.assertEqual(len(countries), 2)
        for country in countries:
            # Both COUNTRY branches still nest under TYPE's own scope -
            # the OR that tells them apart is fully inside their shared
            # parenthesis, one level deeper than where TYPE itself sits.
            self.assertEqual(country.path[: len(type.path)], type.path)

    def test_governing_values_includes_a_same_scope_sibling(self):
        leaves = rm._scan_literals_with_scope('"TYPE" = 20 AND "SUBTYPE" = 5')
        subtype = next(leaf for leaf in leaves if leaf.field == "subtype")
        governing = rm._governing_values(leaves, "subtype", subtype.path)
        self.assertIn("20", governing)

    def test_governing_values_includes_an_enclosing_scopes_comparison(self):
        leaves = rm._scan_literals_with_scope('"TYPE" = 20 AND ("SUBTYPE" = 5)')
        subtype = next(leaf for leaf in leaves if leaf.field == "subtype")
        governing = rm._governing_values(leaves, "subtype", subtype.path)
        self.assertIn("20", governing)

    def test_governing_values_reach_through_multiple_nested_levels(self):
        leaves = rm._scan_literals_with_scope('"TYPE" = 20 AND (("SUBTYPE" = 5))')
        target = next(leaf for leaf in leaves if leaf.field == "subtype")
        governing = rm._governing_values(leaves, "subtype", target.path)
        self.assertIn("20", governing)

    def test_governing_values_never_includes_a_same_field_comparison(self):
        leaves = rm._scan_literals_with_scope('"SUBTYPE" = 611 AND "SUBTYPE" = 5')
        target = leaves[-1]
        governing = rm._governing_values(leaves, "subtype", target.path)
        self.assertNotIn("611", governing)

    def test_sibling_parenthesised_groups_never_govern_each_other(self):
        """Two unrelated parenthesised groups at the SAME level - each a
        distinct paren instance - must not leak context into one another,
        even with no explicit tracking of AND vs. OR: they simply never
        share a scope path at all."""
        leaves = rm._scan_literals_with_scope(
            '("TYPE" = 20 AND "SUBTYPE" = 5) OR ("TYPE" = 30 AND "SUBTYPE" = 611)'
        )
        first_att = next(leaf for leaf in leaves if leaf.field == "subtype" and leaf.code == "5")
        governing = rm._governing_values(leaves, "subtype", first_att.path)
        self.assertIn("20", governing)
        self.assertNotIn("30", governing)

    def test_a_bare_or_at_the_same_scope_breaks_governance(self):
        """An OR - not an AND - joins the two comparisons, so the first can
        no longer supply the second's group."""
        leaves = rm._scan_literals_with_scope('"TYPE" = 20 OR "SUBTYPE" = 5')
        subtype = next(leaf for leaf in leaves if leaf.field == "subtype")
        governing = rm._governing_values(leaves, "subtype", subtype.path)
        self.assertNotIn("20", governing)

    def test_an_or_fully_inside_an_enclosing_parenthesis_does_not_break_it(self):
        """The OR sits one level deeper than TYPE itself - inside the
        parenthesis - so it never touches the scope TYPE governs."""
        leaves = rm._scan_literals_with_scope(
            '"TYPE" = 20 AND ("COUNTRY" = 1 OR "COUNTRY" = 2)'
        )
        for code in ("1", "2"):
            country = next(leaf for leaf in leaves if leaf.field == "country" and leaf.code == code)
            governing = rm._governing_values(leaves, "country", country.path)
            self.assertIn("20", governing)

    def test_two_or_separated_and_runs_never_govern_each_other(self):
        """"TYPE = 20 AND SUBTYPE = 5 OR TYPE = 21 AND SUBTYPE = 611" -
        no parentheses at all, just a top-level OR splitting two flat AND
        runs - each SUBTYPE still only sees its OWN run's TYPE."""
        leaves = rm._scan_literals_with_scope(
            '"TYPE" = 20 AND "SUBTYPE" = 5 OR "TYPE" = 21 AND "SUBTYPE" = 611'
        )
        first = next(leaf for leaf in leaves if leaf.field == "subtype" and leaf.code == "5")
        second = next(leaf for leaf in leaves if leaf.field == "subtype" and leaf.code == "611")
        self.assertEqual(rm._governing_values(leaves, "subtype", first.path), {"20"})
        self.assertEqual(rm._governing_values(leaves, "subtype", second.path), {"21"})


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
        text = '"TYPE" = 20 AND "SUBTYPE" = 5'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "subtype")
        candidates = [("school", "20"), ("warehouse", "30")]
        label = rm._pick_label(candidates, "subtype", target.path, leaves)
        self.assertEqual(label, "school")

    def test_resolves_regardless_of_which_side_of_the_and_the_group_is_on(self):
        text = '"SUBTYPE" = 5 AND "TYPE" = 20'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "subtype")
        candidates = [("school", "20"), ("warehouse", "30")]
        label = rm._pick_label(candidates, "subtype", target.path, leaves)
        self.assertEqual(label, "school")

    def test_resolves_via_an_enclosing_parenthesised_groups_comparison(self):
        text = '"TYPE" = 20 AND ("SUBTYPE" = 5)'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "subtype")
        candidates = [("school", "20"), ("warehouse", "30")]
        label = rm._pick_label(candidates, "subtype", target.path, leaves)
        self.assertEqual(label, "school")

    def test_falls_back_to_every_meaning_when_no_group_matches(self):
        text = '"SUBTYPE" = 5'
        leaves = rm._scan_literals_with_scope(text)
        target = leaves[0]
        candidates = [("school", "20"), ("warehouse", "30")]
        label = rm._pick_label(candidates, "subtype", target.path, leaves)
        self.assertEqual(label, f'{iso("school")} / {iso("warehouse")}')

    def test_falls_back_to_every_meaning_when_more_than_one_group_matches(self):
        """Genuinely ambiguous - both candidates' groups happen to be
        present in the surrounding context - never guess between them."""
        text = '"TYPE" = 20 AND "OTHER_CODE" = 30 AND "SUBTYPE" = 5'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "subtype")
        candidates = [("school", "20"), ("warehouse", "30")]
        label = rm._pick_label(candidates, "subtype", target.path, leaves)
        self.assertEqual(label, f'{iso("school")} / {iso("warehouse")}')

    def test_a_candidate_with_no_group_value_never_matches(self):
        text = '"TYPE" = 20 AND "SUBTYPE" = 5'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "subtype")
        candidates = [("school", ""), ("warehouse", "30")]
        label = rm._pick_label(candidates, "subtype", target.path, leaves)
        # "20" governs, but neither candidate's own group is "20".
        self.assertEqual(label, f'{iso("school")} / {iso("warehouse")}')

    def test_alt_mode_resolves_the_same_way_then_renders_its_alternative(self):
        text = '"TYPE" = 20 AND "SUBTYPE" = 5'
        leaves = rm._scan_literals_with_scope(text)
        target = next(leaf for leaf in leaves if leaf.field == "subtype")
        candidates = [("school", "20"), ("warehouse", "30")]
        label = rm._pick_label(
            candidates, "subtype", target.path, leaves, mode="alt",
            alt_for_code={"school": "מסגד", "warehouse": "חממה"},
        )
        self.assertEqual(label, "מסגד")


class SubstituteDescriptionsGroupResolutionTests(unittest.TestCase):
    """substitute_descriptions() end to end: an ambiguous code resolved
    straight from the expression's own group context - the mechanism that
    replaces remembering which meaning was chosen. Nothing here is ever
    written anywhere; the same expression re-rendered later resolves the
    same way again, straight from its own text and the lookup table."""

    def test_flat_and_pattern_resolves_the_ambiguous_code(self):
        mapping = {"subtype": {"5": [("school", "20"), ("warehouse", "30")]}}
        expr = '"TYPE" = 20 AND "SUBTYPE" = 5'
        result = rm.substitute_descriptions(expr, mapping)
        self.assertEqual(result, f'"TYPE" = 20 AND "SUBTYPE" = {iso("school")}')

    def test_parenthesised_group_pattern_resolves_every_code_inside_it(self):
        mapping = {
            "subtype": {
                "5": [("school", "20"), ("warehouse", "30")],
                "611": [("church", "20"), ("barn", "30")],
            }
        }
        expr = '"TYPE" = 20 AND ("SUBTYPE" = 5 OR "SUBTYPE" = 611)'
        result = rm.substitute_descriptions(expr, mapping)
        self.assertEqual(
            result,
            f'"TYPE" = 20 AND ("SUBTYPE" = {iso("school")} OR "SUBTYPE" = {iso("church")})',
        )

    def test_no_group_context_falls_back_to_showing_every_meaning(self):
        mapping = {"subtype": {"5": [("school", "20"), ("warehouse", "30")]}}
        expr = '"SUBTYPE" = 5'
        result = rm.substitute_descriptions(expr, mapping)
        joined = f'{iso("school")} / {iso("warehouse")}'
        self.assertEqual(result, f'"SUBTYPE" = {iso(joined)}')

    def test_two_separate_groups_each_resolve_their_own_ambiguous_code(self):
        """The same code appearing twice, under two different groups in
        two different parenthesised clauses, resolves independently and
        correctly each time - the whole point of reading it straight from
        each occurrence's own context instead of remembering one answer
        per code."""
        mapping = {"subtype": {"5": [("school", "20"), ("warehouse", "30")]}}
        expr = (
            '("TYPE" = 20 AND "SUBTYPE" = 5) OR '
            '("TYPE" = 30 AND "SUBTYPE" = 5)'
        )
        result = rm.substitute_descriptions(expr, mapping)
        self.assertEqual(
            result,
            f'("TYPE" = 20 AND "SUBTYPE" = {iso("school")}) OR '
            f'("TYPE" = 30 AND "SUBTYPE" = {iso("warehouse")})',
        )

    def test_a_bare_or_between_the_two_fields_falls_back_to_showing_every_meaning(self):
        """"TYPE" = 20 OR "SUBTYPE" = 5 - an OR, not an AND, joins them,
        so 20 must NOT be treated as 5's group."""
        mapping = {"subtype": {"5": [("school", "20"), ("warehouse", "30")]}}
        expr = '"TYPE" = 20 OR "SUBTYPE" = 5'
        result = rm.substitute_descriptions(expr, mapping)
        joined = f'{iso("school")} / {iso("warehouse")}'
        self.assertEqual(result, f'"TYPE" = 20 OR "SUBTYPE" = {iso(joined)}')

    def test_two_or_separated_and_runs_each_resolve_their_own_code(self):
        """"TYPE" = 20 AND "SUBTYPE" = 5 OR "TYPE" = 21 AND "SUBTYPE" =
        5 - no parentheses at all: a top-level OR alone must split the two
        flat AND runs, exactly as a pair of parenthesised groups would."""
        mapping = {"subtype": {"5": [("school", "20"), ("warehouse", "21")]}}
        expr = '"TYPE" = 20 AND "SUBTYPE" = 5 OR "TYPE" = 21 AND "SUBTYPE" = 5'
        result = rm.substitute_descriptions(expr, mapping)
        self.assertEqual(
            result,
            f'"TYPE" = 20 AND "SUBTYPE" = {iso("school")} OR '
            f'"TYPE" = 21 AND "SUBTYPE" = {iso("warehouse")}',
        )

    def test_an_or_fully_inside_a_parenthesised_group_does_not_break_governance(self):
        """Every value in this expression belongs to TYPE 20's group,
        including two fields joined by an OR - but that OR sits entirely
        inside its OWN parenthesis, never at the same scope as TYPE
        itself, so it must not stop either COUNTRY branch from resolving."""
        mapping = {
            "subtype": {"5": [("school", "20"), ("warehouse", "30")]},
            "sensitivity": {
                "5": [("high", "20"), ("low", "30")],
                "603": [("medium", "20"), ("minimal", "30")],
            },
            "country": {
                "604": [("Israel", "20"), ("Jordan", "30")],
                "603": [("Egypt", "20"), ("Lebanon", "30")],
            },
        }
        expr = (
            '"TYPE" = 20 AND "SUBTYPE" = 5 AND "SENSITIVITY" IN (5,603) '
            'AND ("COUNTRY" = 604 OR "COUNTRY" = 603)'
        )
        result = rm.substitute_descriptions(expr, mapping)
        self.assertEqual(
            result,
            '"TYPE" = 20 AND "SUBTYPE" = '
            f'{iso("school")} AND "SENSITIVITY" IN ({iso("high")},{iso("medium")}) '
            f'AND ("COUNTRY" = {iso("Israel")} OR "COUNTRY" = {iso("Egypt")})',
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
        original = "\"TYPE\" IN (20, 21)"
        result = rm.force_ltr_paragraphs(original)
        self.assertEqual(result.replace(rm._LRM, ""), original)

    def test_applied_after_substitution_the_fields_own_order_is_preserved(self):
        """The end-to-end scenario reported: a Hebrew field description
        replacing the field name must not cause the value description (or
        a whole IN-list) to visually reorder relative to it."""
        mapping = {"type": {"20": [("בית כנסת", "")], "21": [("מבנה חקלאי", "")]}}
        expr = "\"TYPE\" IN (20, 21)"
        substituted = rm.substitute_descriptions(
            expr, mapping, field_descriptions={"type": "ישות"}
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
        mapping = {"type": {"20": [("בית כנסת", "")], "21": [("מבנה חקלאי", "")]}}
        result = rm.substitute_descriptions("\"TYPE\" IN (20, 21)", mapping)
        self.assertEqual(
            result, f'"TYPE" IN ({iso("בית כנסת")}, {iso("מבנה חקלאי")})'
        )

    def test_stripping_the_isolate_markers_recovers_the_original_left_to_right_order(self):
        """The decisive check: whatever the isolates do visually, the
        underlying LOGICAL sequence - field, operator, list in its original
        order - must exactly match the source expression's own order,
        regardless of how many RTL/LTR labels are involved or how they are
        nested."""
        mapping = {
            "type": {
                "20": [("מבנה דת", "")],
                "21": [("מבנה חקלאי", "")],
                "2302": [("בית ספר", "")],
            }
        }
        expr = "\"TYPE\" IN (20, 21, 2302)"
        result = rm.substitute_descriptions(
            expr, mapping, field_descriptions={"type": "ישות"}
        )
        stripped = result.replace(rm._FSI, "").replace(rm._PDI, "")
        self.assertEqual(stripped, "ישות IN (מבנה דת, מבנה חקלאי, בית ספר)")

    def test_a_nested_expression_keeps_every_label_in_source_order(self):
        """No matter how much the expression is nested: two separate
        field/value clauses joined by AND, each independently substituted,
        must still read in their original left-to-right sequence once the
        isolate markers are stripped."""
        mapping = {
            "type": {"20": [("מבנה דת", "")]},
            "f_type": {"1": [("פעיל", "")], "2": [("לא פעיל", "")]},
        }
        expr = "(\"TYPE\" = 20) AND (\"F_TYPE\" IN (1, 2))"
        result = rm.substitute_descriptions(
            expr, mapping, field_descriptions={"type": "ישות", "f_type": "סוג"}
        )
        stripped = result.replace(rm._FSI, "").replace(rm._PDI, "")
        self.assertEqual(stripped, "(ישות = מבנה דת) AND (סוג IN (פעיל, לא פעיל))")

    def test_ambiguous_meanings_joined_by_slash_are_each_isolated_too(self):
        mapping = {"code": {"5": [("מסגד", ""), ("חממה", "")]}}
        result = rm.substitute_descriptions("\"CODE\" = '5'", mapping)
        joined = f'{iso("מסגד")} / {iso("חממה")}'
        self.assertEqual(result, f'"CODE" = {iso(joined)}')
        stripped = result.replace(rm._FSI, "").replace(rm._PDI, "")
        self.assertEqual(stripped, '"CODE" = מסגד / חממה')


class DescriptionResolverTests(unittest.TestCase):
    def setUp(self):
        reset_plugin_settings()
        self.layer = make_lookup_layer()
        # Not added to the project - a real lookup dataset never is any more
        # (see Settings.autocomplete_layer()); set_layer_for_testing() is the
        # test-only seam that injects this standalone memory layer directly.
        Settings.set_layer_for_testing(self.layer)
        Settings.set_field("field_names", "field_name")
        Settings.set_field("value", "value")
        Settings.set_field("description", "description")
        Settings.set_field("table", "table")
        Settings.set_field("group_code", "group_code")
        rm.DescriptionResolver.invalidate()

    def tearDown(self):
        reset_plugin_settings()
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
        # Not added to the project - see the note in DescriptionResolverTests.setUp.
        QgsProject.instance().addMapLayer(self.context_layer)

        Settings.set_autocomplete_enabled(True)
        Settings.set_layer_for_testing(self.lookup_layer)
        Settings.set_field("field_names", "field_name")
        Settings.set_field("value", "value")
        Settings.set_field("description", "description")
        Settings.set_field("table", "table")
        rm.DescriptionResolver.invalidate()

    def tearDown(self):
        rm.DescriptionResolver.invalidate()
        QgsProject.instance().removeMapLayers([self.context_layer.id()])
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
        "TYPE" = ... context - no choice remembered anywhere."""
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
                {"field_name": "SUBTYPE", "value": "5", "description": "school", "group_code": "20", "table": "context"},
                {"field_name": "SUBTYPE", "value": "5", "description": "warehouse", "group_code": "30", "table": "context"},
            ],
        )
        # Not added to the project - see the note in DescriptionResolverTests.setUp.
        Settings.set_layer_for_testing(ambiguous_layer)
        Settings.set_field("group_code", "group_code")
        rm.DescriptionResolver.invalidate()
        editor = self._make_editor('"TYPE" = 20 AND "SUBTYPE" = 5')
        controller = rm.ReadModeController(editor)
        try:
            controller._switch.setMode(1)  # edit -> read
            self.assertIn("school", editor.toPlainText())
            self.assertNotIn("warehouse", editor.toPlainText())
        finally:
            controller.teardown()


class ReadModeSafetyTests(ReadModeControllerModeCountTests):
    """Read mode must never be able to leak description text into the
    real expression, and must follow changes QGIS makes meanwhile."""

    def test_undo_after_leaving_read_mode_does_not_restore_descriptions(self):
        original = "\"STATUS\" = '1'"
        editor = self._make_editor(original)
        controller = rm.ReadModeController(editor)
        try:
            controller._switch.setMode(1)  # edit -> read
            self.assertIn("Active", editor.toPlainText())
            controller._switch.setMode(0)  # read -> edit
            editor.undo()
            editor.undo()
            self.assertNotIn("Active", editor.toPlainText())
            self.assertEqual(editor.toPlainText(), original)
        finally:
            controller.teardown()

    def test_editor_is_writable_again_after_a_refresh_while_reading(self):
        editor = self._make_editor("\"STATUS\" = '1'")
        controller = rm.ReadModeController(editor)
        try:
            controller._switch.setMode(1)
            # Simulates QGIS changing the expression while read mode shows.
            controller._on_sci_text_changed()
            self.assertTrue(editor.isReadOnly())
            controller._switch.setMode(0)
            self.assertFalse(editor.isReadOnly())
        finally:
            controller.teardown()


class FieldContextBoundaryTests(unittest.TestCase):
    """A number only gets a field's description while it still belongs to
    that field's comparison."""

    MAPPING = {"status": {"1": [("Active", "")], "2": [("Inactive", "")]}}

    def test_numbers_inside_a_function_call_are_left_alone(self):
        result = rm.substitute_descriptions('"STATUS" = 1 AND round($area, 2) > 1', self.MAPPING)
        self.assertIn("round($area, 2) > 1", result)
        self.assertEqual(result.count("Active"), 1)
        self.assertNotIn("Inactive", result)

    def test_in_lists_still_substitute_every_value(self):
        result = rm.substitute_descriptions('"STATUS" IN (1, 2)', self.MAPPING)
        self.assertIn("Active", result)
        self.assertIn("Inactive", result)

    def test_between_keeps_both_bounds_on_the_field(self):
        result = rm.substitute_descriptions('"STATUS" BETWEEN 1 AND 2', self.MAPPING)
        self.assertIn("Active", result)
        self.assertIn("Inactive", result)


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

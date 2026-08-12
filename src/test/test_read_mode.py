# -*- coding: utf-8 -*-
"""Read mode: code/description substitution, ambiguous-code handling, and the
mapping built from a configured lookup layer."""

import unittest

from qgis.core import QgsProject

from .. import rtl_readmode as rm
from ..rtl_settings import Settings
from .utils import make_lookup_layer, reset_plugin_settings


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
        rm.ChoiceMemory.remember("mytable", "code", "610", "greenhouse", 0, "ctx")
        mapping = {"code": {"610": ["mosque", "greenhouse"]}}
        expr = "\"CODE\" = '610'"
        self.assertEqual(
            rm.substitute_descriptions(expr, mapping, "mytable", "ctx"),
            '"CODE" = greenhouse',
        )


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
        QgsProject.instance().removeAllMapLayers()
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
        QgsProject.instance().removeAllMapLayers()
        rm.DescriptionResolver.invalidate()

    def test_mapping_builds_field_to_code_to_descriptions(self):
        mapping = rm.DescriptionResolver.mapping(["context"])
        self.assertIn("status", mapping)
        self.assertEqual(mapping["status"]["1"], ["Active"])

    def test_mapping_is_empty_without_a_description_field_configured(self):
        Settings.set_field("description", "")
        rm.DescriptionResolver.invalidate()
        self.assertEqual(rm.DescriptionResolver.mapping(["context"]), {})


if __name__ == "__main__":
    unittest.main()

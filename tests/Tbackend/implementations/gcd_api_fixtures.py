"""Observed GCD REST contracts, not a production adapter or mapping policy."""

import json
from pathlib import Path
from unittest import TestCase

from backend.base.file_extraction import extract_issue_number


class GCDFixtureTests(TestCase):
    def load_fixture(self, name: str) -> dict:
        path = Path(__file__).parents[2] / 'fixtures' / 'gcd' / (name + '.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def test_series_identity_is_hyperlinked(self):
        series = self.load_fixture('series-6139')['response']
        self.assertNotIn('id', series)
        self.assertTrue(series['api_url'].endswith('/6139/'))
        self.assertEqual(len(series['active_issues']), 13)
        self.assertEqual(len(series['issue_descriptors']), 13)

    def test_binding_can_describe_different_children(self):
        series = self.load_fixture('series-6139')['response']
        self.assertEqual(series['binding'],
                         'squarebound (#1 and 13); saddle-stitched (#2-12)')

    def test_variants_are_distinct_unnumbered_publications(self):
        base = self.load_fixture('issue-2267832')['response']
        variant = self.load_fixture('issue-2658754')['response']
        self.assertEqual(base['number'], '[nn]')
        self.assertEqual(base['number'], variant['number'])
        self.assertEqual(variant['variant_of'], base['api_url'])
        self.assertNotEqual(base['isbn'], variant['isbn'])
        self.assertNotEqual(base['on_sale_date'], variant['on_sale_date'])
        self.assertIsNone(variant['page_count'])
        self.assertIsInstance(base['page_count'], str)

    def test_series_includes_variant_but_variant_does_not_expand_base_stories(self):
        series = self.load_fixture('series-176787')['response']
        base = self.load_fixture('issue-2267832')
        variant = self.load_fixture('issue-2658754')
        self.assertEqual(len(series['active_issues']), 2)
        self.assertEqual(base['acquisition']['original_story_count'], 41)
        self.assertEqual(variant['acquisition']['original_story_count'], 1)

    def test_collection_contents_are_not_rest_reprint_edges(self):
        fixture = self.load_fixture('issue-2267832')
        issue = fixture['response']
        self.assertEqual(issue['title'], '')
        self.assertEqual(issue['story_set'][1]['title'], 'Chapter One: Crime')
        self.assertIn('Collects', issue['notes'])
        self.assertNotIn('reprints', fixture['acquisition']['observed_fields'])
        self.assertNotIn('id', fixture['acquisition']['observed_story_fields'])

    def test_partial_dates_and_credit_sentinel_remain_raw(self):
        issue = self.load_fixture('issue-2267832')['response']
        self.assertEqual(issue['key_date'], '2021-12-00')
        self.assertEqual(issue['publication_date'], '[December] 2021')
        self.assertEqual(issue['story_set'][0]['script'], 'None')

    def test_existing_number_parser_is_not_a_gcd_mapping_policy(self):
        self.assertEqual(extract_issue_number('[nn]'), 0.1414)
        self.assertEqual(extract_issue_number('1/2'), (1.0, 2.0))

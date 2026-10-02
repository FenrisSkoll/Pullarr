"""Offline contract tests; synthetic mutations are not live GCD evidence."""

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import TestCase

from backend.implementations.metadata.gcd_staging import (Acquisition,
                                                          Capability,
                                                          GcdIssueNumber,
                                                          GcdIssueSnapshot,
                                                          GcdPartialDate,
                                                          GcdSeriesSnapshot,
                                                          GcdVariantRelation,
                                                          admit_issue,
                                                          admit_series)


class GCDStagingTests(TestCase):
    def issue(self, number='1', date='2021-12-25', identity='10'):
        return GcdIssueSnapshot(identity, '1', GcdIssueNumber(number), None,
                                GcdPartialDate(date), GcdPartialDate(None))

    def series(self, *issues, coherent=True):
        return GcdSeriesSnapshot('1', 'Example', tuple(issues), Acquisition(
            tuple(i.provider_id for i in issues), True,
            coherent_catalog_snapshot='test-catalog-transaction' if coherent else None))

    def test_nn_preserved_and_blocked(self):
        issue = self.issue('[nn]')
        self.assertEqual(issue.number.raw, '[nn]')
        self.assertIsNone(issue.number.calculated)
        result = admit_issue(issue, 'key_date')
        self.assertIn('unsupported_issue_number', result.blockers)
        self.assertFalse(result.neutral_admissible)
        self.assertIn(Capability.DETAIL_SAFE, result.capabilities)

    def test_safe_exact_numbers(self):
        for raw, expected in [('0', 0.0), ('1', 1.0), ('01', 1.0), ('1.5', 1.5)]:
            with self.subTest(raw=raw):
                issue = self.issue(raw)
                self.assertEqual(issue.number.raw, raw)
                self.assertEqual(issue.number.calculated, expected)
                self.assertTrue(admit_issue(issue, 'key_date').neutral_admissible)

    def test_unknown_suffix_fraction_and_inexact_numbers_remain_raw(self):
        values = (None, '', '[nn]', 'nn', '1A', '1-B', '½', '1/2',
                  'Annual', 'Special', 'text', ' 1', '1e2', 'NaN', '1.1',
                  '9007199254740993')
        for raw in values:
            with self.subTest(raw=raw):
                number = GcdIssueNumber(raw)
                self.assertEqual(number.raw, raw)
                self.assertIsNone(number.calculated)

    def test_date_precision(self):
        for raw, components, precision in (
                ('2021-12-25', (2021, 12, 25), 'day'),
                ('2021-12-00', (2021, 12, None), 'month'),
                ('2021-00-00', (2021, None, None), 'year'),
                (None, (None, None, None), 'absent'),
                ('', (None, None, None), 'absent')):
            with self.subTest(raw=raw):
                value = GcdPartialDate(raw)
                self.assertEqual(value.raw, raw)
                self.assertEqual(value.components, components)
                self.assertEqual(value.precision, precision)
                self.assertEqual(value.complete_date, raw if precision == 'day' else None)

    def test_invalid_or_textual_date_not_repaired(self):
        for raw in ('2021-02-29', '2021-13-00', '2021-00-12', '0000-00-00',
                    '[December] 2021', '2021', '2021-1-1'):
            value = GcdPartialDate(raw)
            self.assertEqual(value.raw, raw)
            self.assertEqual(value.precision, 'unsupported')
            self.assertIsNone(value.complete_date)

    def test_partial_dates_cannot_authorize_neutral_or_age_input(self):
        for raw in ('2021-12-00', '2021-00-00'):
            issue = self.issue(date=raw)
            self.assertFalse(admit_issue(issue, 'key_date').neutral_admissible)
            self.assertIsNone(issue.key_date.complete_date)
            self.assertFalse(admit_series(self.series(issue), '1', 'key_date').destructive_complete)

    def test_date_source_must_be_explicit_no_fallback(self):
        issue = replace(self.issue(date='2021-12-00'), on_sale_date=GcdPartialDate('2021-10-12'))
        self.assertFalse(admit_issue(issue, 'key_date').neutral_admissible)
        self.assertTrue(admit_issue(issue, 'on_sale_date').neutral_admissible)
        with self.assertRaises(ValueError):
            admit_issue(issue, 'automatic')

    def test_real_base_variant_fixture_stages_without_inheritance(self):
        staged = []
        for identity in ('2267832', '2658754'):
            path = Path(__file__).parents[2] / 'fixtures' / 'gcd' / ('issue-' + identity + '.json')
            raw = json.loads(path.read_text(encoding='utf-8'))['response']
            relation = (GcdVariantRelation(identity, '2267832', 'gcd:variant_of')
                        if raw['variant_of'] else None)
            staged.append(GcdIssueSnapshot(identity, '176787', GcdIssueNumber(raw['number']),
                                           raw['title'], GcdPartialDate(raw['key_date']),
                                           GcdPartialDate(raw['on_sale_date']), relation,
                                           json.dumps(raw)))
        base, variant = staged
        self.assertNotEqual(base.provider_id, variant.provider_id)
        self.assertEqual(base.number.raw, variant.number.raw)
        self.assertEqual(variant.variant.base_issue_id, base.provider_id)
        self.assertEqual(len(json.loads(variant.bibliography_json)['story_set']), 1)
        self.assertEqual(len(json.loads(base.bibliography_json)['story_set']), 3)
        snapshot = GcdSeriesSnapshot('176787', 'Long Halloween', tuple(staged),
                                     Acquisition(tuple(i.provider_id for i in staged), True))
        result = admit_series(snapshot, '176787', 'key_date')
        self.assertIn(Capability.SEARCH_SAFE, result.capabilities)
        self.assertIn(Capability.DETAIL_SAFE, result.capabilities)
        self.assertIn('variant_policy_unresolved', result.blockers)
        self.assertFalse(result.neutral_admissible)
        self.assertFalse(result.destructive_complete)

    def test_invalid_variant_relationship(self):
        for relation in (GcdVariantRelation('10', '10', 'gcd'),
                         GcdVariantRelation('11', '12', 'gcd')):
            result = admit_issue(replace(self.issue(), variant=relation), 'key_date')
            self.assertIn('malformed_variant_relation', result.blockers)

    def test_full_synthetic_catalog_contract(self):
        result = admit_series(self.series(self.issue()), '1', 'key_date')
        self.assertTrue(result.neutral_admissible)
        self.assertTrue(result.destructive_complete)

    def test_full_rest_acquisition_is_not_coherence_proof(self):
        result = admit_series(self.series(self.issue(), coherent=False), '1', 'key_date')
        self.assertTrue(result.neutral_admissible)
        self.assertFalse(result.destructive_complete)
        self.assertIn('snapshot_coherence_unproven', result.blockers)

    def test_incomplete_wrong_parent_duplicate_and_failed_acquisitions(self):
        good = self.series(self.issue())
        cases = (
            (replace(good, acquisition=replace(good.acquisition, enumeration_exhausted=False)), 'incomplete_snapshot'),
            (replace(good, acquisition=replace(good.acquisition, failures=('page_error',))), 'incomplete_snapshot'),
            (replace(good, issues=()), 'issue_set_mismatch'),
            (replace(good, issues=(good.issues[0], good.issues[0])), 'duplicate_identity'),
            (replace(good, issues=(replace(good.issues[0], parent_series_id='2'),)), 'wrong_parent'),
            (replace(good, acquisition=replace(good.acquisition, scope='overview')), 'variant_scope_mismatch'),
            (replace(good, acquisition=replace(good.acquisition, expected_issue_ids=('10', '10'))), 'duplicate_identity'),
        )
        for snapshot, reason in cases:
            with self.subTest(reason=reason):
                result = admit_series(snapshot, '1', 'key_date')
                self.assertIn(reason, result.blockers)
                self.assertFalse(result.destructive_complete)

    def test_one_unrepresentable_issue_blocks_whole_snapshot(self):
        result = admit_series(self.series(self.issue(), self.issue('[nn]', identity='11')), '1', 'key_date')
        self.assertFalse(result.neutral_admissible)
        self.assertFalse(result.destructive_complete)
        self.assertIn(Capability.SEARCH_SAFE, result.capabilities)

    def test_equal_numbers_do_not_merge_ids_or_authorize_matching(self):
        snapshot = self.series(self.issue('1'), self.issue('01', identity='11'))
        self.assertEqual(len(snapshot.issues), 2)
        self.assertIn('calculated_number_collision', admit_series(snapshot, '1', 'key_date').blockers)

    def test_combinatorial_admission_invariant(self):
        for raw in ('1', '[nn]', '1A', '1.5', None):
            for when in ('2021-12-25', '2021-12-00', None):
                for coherent in (False, True):
                    issue = self.issue(raw, when)
                    snapshot = self.series(issue, coherent=coherent)
                    result = admit_series(snapshot, '1', 'key_date')
                    self.assertEqual(issue.number.raw, raw)
                    self.assertEqual(issue.provider_id, '10')
                    if result.destructive_complete:
                        self.assertTrue(admit_issue(issue, 'key_date').neutral_admissible)
                    if not admit_issue(issue, 'key_date').neutral_admissible:
                        self.assertFalse(result.destructive_complete)

    def test_values_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.issue().provider_id = '11'

    def test_identity_and_unexpected_detail_failures(self):
        good = self.series(self.issue())
        self.assertIn('wrong_series', admit_series(good, '2', 'key_date').blockers)
        malformed = replace(good, issues=(replace(good.issues[0], provider_id=''),))
        self.assertIn('malformed_identity', admit_series(malformed, '1', 'key_date').blockers)
        unexpected = replace(good, issues=good.issues + (self.issue('2', identity='11'),))
        self.assertIn('issue_set_mismatch', admit_series(unexpected, '1', 'key_date').blockers)

    def test_empty_acquisition_requires_explicit_exhaustion_and_coherence(self):
        full = self.series()
        self.assertTrue(admit_series(full, '1', 'key_date').destructive_complete)
        partial = replace(full, acquisition=replace(full.acquisition, enumeration_exhausted=False))
        self.assertFalse(admit_series(partial, '1', 'key_date').destructive_complete)

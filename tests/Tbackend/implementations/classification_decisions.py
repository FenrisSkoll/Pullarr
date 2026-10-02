"""Exact winning reasons, nonwinning evidence, and explicit-clock parity."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from fixtures import classifier_legacy as old
from Tbackend.implementations.classification_parity import (NOW, PHYSICAL,
                                                            PUBLICATION,
                                                            branch_cases,
                                                            generated_cases)

from backend.base.definitions import SpecialVersion as SV
from backend.implementations import classification
from backend.implementations.classification import (POLICY_ID,
                                                    ClassificationIssue,
                                                    ClassificationReason,
                                                    ClassificationSource,
                                                    EvidenceDisposition,
                                                    evaluate_special_version)


def evaluate(**kwargs):
    inputs = dict(title='Unmarked', description=None,
                  issues=(ClassificationIssue(None, None),),
                  stored_value=SV.NORMAL, locked=False, evaluated_at=NOW)
    inputs.update(kwargs)
    return evaluate_special_version(**inputs)


class ClassificationDecisions(TestCase):
    def test_direct_pure_result_matches_frozen_oracle(self):
        with patch.object(old, 'Volume') as volume, patch.object(old, 'datetime', wraps=datetime) as clock:
            for index, (data, issues, physical, publication, now) in enumerate((*branch_cases(), *generated_cases())):
                with self.subTest(index=index):
                    volume.return_value.get_data.return_value = data
                    volume.return_value.get_issues.return_value = issues
                    clock.now.return_value = now
                    inputs = dict(title=data.title, description=data.description,
                                  issues=tuple(ClassificationIssue(i.title, i.date) for i in issues),
                                  stored_value=data.special_version, locked=data.special_version_locked,
                                  format_evidence=physical, publication_evidence=publication, evaluated_at=now)
                    try:
                        expected = old.determine_special_version(1, physical, publication)
                    except ValueError as error:
                        with self.assertRaises(ValueError) as actual:
                            evaluate_special_version(**inputs)
                        self.assertEqual(str(actual.exception), str(error))
                    else:
                        result = evaluate_special_version(**inputs)
                        self.assertEqual(result.value, expected)
                        self.assertEqual(result.evaluated_at, now)
                        self.assertEqual(result.policy_id, POLICY_ID)

    def test_every_automatic_reason_has_a_winning_branch(self):
        cases = [
            (dict(locked=True, stored_value=SV.NORMAL, format_evidence=PHYSICAL[1]), SV.NORMAL,
             'current_lock', 'current_lock_preserved'),
            (dict(issues=(ClassificationIssue('Volume 1', None),)), SV.VOLUME_AS_ISSUE,
             'logical_vai', 'all_issue_titles_volume_numbered'),
            (dict(format_evidence=PHYSICAL[1]), SV.HARD_COVER,
             'provider_physical_format', 'sole_issue_physical_evidence'),
            (dict(publication_evidence=PUBLICATION[1]), SV.ONE_SHOT,
             'provider_publication_kind', 'sole_issue_publication_evidence'),
            (dict(title='Omnibus'), SV.OMNIBUS, 'legacy_volume_title', 'volume_title_omnibus_marker'),
            (dict(title='One-Shot'), SV.ONE_SHOT, 'legacy_volume_title', 'volume_title_one_shot_marker'),
            (dict(title='Hardcover'), SV.HARD_COVER, 'legacy_volume_title', 'volume_title_hardcover_marker'),
            (dict(issues=(ClassificationIssue('omnibus', None),)), SV.OMNIBUS,
             'legacy_issue_label', 'issue_title_omnibus_label'),
            (dict(issues=(ClassificationIssue('HC', None),)), SV.HARD_COVER,
             'legacy_issue_label', 'issue_title_hardcover_label'),
            (dict(issues=(ClassificationIssue('OS', None),)), SV.ONE_SHOT,
             'legacy_issue_label', 'issue_title_one_shot_label'),
            (dict(title='Semiannual'), SV.NORMAL, 'legacy_annual_exclusion', 'volume_title_annual_exclusion'),
            (dict(description='An omnibus.'), SV.OMNIBUS, 'legacy_description', 'description_omnibus_marker'),
            (dict(description='A one-shot.'), SV.ONE_SHOT, 'legacy_description', 'description_one_shot_marker'),
            (dict(description='A hardcover.'), SV.HARD_COVER, 'legacy_description', 'description_hardcover_marker'),
            (dict(description='An annual.'), SV.NORMAL, 'legacy_annual_exclusion', 'description_annual_exclusion'),
            (dict(issues=(ClassificationIssue('TPB', '2000-01-01'),)), SV.TPB,
             'legacy_age_count', 'aged_single_issue_tpb'),
            (dict(), SV.NORMAL, 'default_normal', 'no_rule_matched'),
        ]
        for inputs, value, source, reason in cases:
            with self.subTest(reason=reason):
                result = evaluate(**inputs)
                self.assertEqual((result.value, result.source.value, result.reason.value), (value, source, reason))
        self.assertEqual({case[3] for case in cases}, {reason.value for reason in ClassificationReason})
        self.assertEqual({case[2] for case in cases}, {source.value for source in ClassificationSource})

    def test_both_physical_and_both_publication_values(self):
        for physical, publication, expected in (
                (PHYSICAL[1], None, SV.HARD_COVER), (PHYSICAL[2], None, SV.TPB),
                (None, PUBLICATION[1], SV.ONE_SHOT), (None, PUBLICATION[2], SV.OMNIBUS)):
            result = evaluate(format_evidence=physical, publication_evidence=publication)
            self.assertEqual(result.value, expected)
            winner = result.physical_evidence if physical else result.publication_evidence
            self.assertEqual(winner.disposition, EvidenceDisposition.ACCEPTED)
            self.assertEqual(winner.evidence, physical or publication)

    def test_conflict_disposition_never_replaces_winning_fallback(self):
        for inputs, value, source in (
                (dict(issues=(ClassificationIssue(None, '2000-01-01'),)), SV.TPB, 'legacy_age_count'),
                (dict(title='Omnibus'), SV.OMNIBUS, 'legacy_volume_title'),
                (dict(), SV.NORMAL, 'default_normal')):
            result = evaluate(format_evidence=PHYSICAL[1], publication_evidence=PUBLICATION[1], **inputs)
            self.assertEqual((result.value, result.source.value), (value, source))
            for assessment in (result.physical_evidence, result.publication_evidence):
                self.assertEqual(assessment.disposition, EvidenceDisposition.DECLINED_CROSS_AXIS_CONFLICT)

    def test_lock_short_circuits_even_conflicting_or_unmapped_evidence(self):
        for physical in PHYSICAL[1:]:
            for stored in SV:
                with patch.object(classification, 'single_issue_format', side_effect=AssertionError('late branch')):
                    result = evaluate(locked=True, stored_value=stored, format_evidence=physical,
                                      publication_evidence=PUBLICATION[1],
                                      issues=(ClassificationIssue('Volume 1', 'invalid'),))
                self.assertEqual(result.value, stored)
                self.assertEqual(result.source, ClassificationSource.CURRENT_LOCK)
                self.assertIsNone(result.facts.volume_numbered_count)
                self.assertIsNone(result.facts.issue_age)
                self.assertEqual(result.physical_evidence.disposition, EvidenceDisposition.NOT_EVALUATED_DUE_TO_LOCK)
                self.assertEqual(result.publication_evidence.disposition, EvidenceDisposition.NOT_EVALUATED_DUE_TO_LOCK)

    def test_locked_without_evidence_is_unapplied_candidate_not_lock_receipt(self):
        result = evaluate(locked=True, stored_value=SV.HARD_COVER,
                          issues=(ClassificationIssue(None, '2000-01-01'),))
        self.assertTrue(result.locked)
        self.assertEqual(result.value, SV.TPB)
        self.assertEqual(result.source, ClassificationSource.LEGACY_AGE_COUNT)
        self.assertEqual(result.physical_evidence.disposition, EvidenceDisposition.NOT_SUPPLIED)
        self.assertFalse(hasattr(result, 'applied'))

    def test_vai_facts_and_unreached_evidence(self):
        result = evaluate(issues=(ClassificationIssue('Volume 1', None), ClassificationIssue('Vol. Two', None)),
                          format_evidence=PHYSICAL[1], publication_evidence=PUBLICATION[2])
        self.assertEqual(result.value, SV.VOLUME_AS_ISSUE)
        self.assertEqual((result.facts.issue_count, result.facts.volume_numbered_count), (2, 2))
        self.assertEqual(result.physical_evidence.disposition, EvidenceDisposition.NOT_EVALUATED_DUE_TO_VAI)
        self.assertEqual(result.publication_evidence.disposition, EvidenceDisposition.NOT_EVALUATED_DUE_TO_VAI)
        mixed = evaluate(issues=(ClassificationIssue('Volume 1', None), ClassificationIssue(None, None)))
        self.assertEqual((mixed.facts.issue_count, mixed.facts.volume_numbered_count), (2, 1))
        self.assertEqual(mixed.source, ClassificationSource.DEFAULT_NORMAL)

    def test_cardinality_dispositions_are_not_winning_reasons(self):
        for count, disposition in ((0, EvidenceDisposition.INAPPLICABLE_ZERO_ISSUES),
                                    (2, EvidenceDisposition.INAPPLICABLE_MULTIPLE_ISSUES)):
            result = evaluate(issues=(ClassificationIssue(None, None),) * count,
                              format_evidence=PHYSICAL[1], publication_evidence=PUBLICATION[2])
            self.assertEqual(result.source, ClassificationSource.DEFAULT_NORMAL)
            self.assertEqual(result.reason, ClassificationReason.NO_RULE_MATCHED)
            self.assertEqual(result.physical_evidence.disposition, disposition)
            self.assertEqual(result.publication_evidence.disposition, disposition)

    def test_unknown_raw_evidence_is_preserved_not_accepted(self):
        for raw in ('Graphic Novel', 'Limited Series', 'Single Issue', 'Digital Chapter', 'Unknown'):
            physical = replace(PHYSICAL[3], raw_value=raw)
            publication = replace(PUBLICATION[3], raw_value=raw)
            result = evaluate(format_evidence=physical, publication_evidence=publication)
            self.assertEqual(result.source, ClassificationSource.DEFAULT_NORMAL)
            self.assertEqual(result.physical_evidence.evidence, physical)
            self.assertEqual(result.publication_evidence.evidence, publication)
            self.assertEqual(result.physical_evidence.disposition, EvidenceDisposition.UNMAPPED_VALUE)
            self.assertEqual(result.publication_evidence.disposition, EvidenceDisposition.UNMAPPED_VALUE)
        result = evaluate(format_evidence=PHYSICAL[1], publication_evidence=PUBLICATION[3])
        self.assertEqual(result.value, SV.HARD_COVER)
        self.assertEqual(result.publication_evidence.disposition, EvidenceDisposition.UNMAPPED_VALUE)

    def test_explicit_clock_strict_boundary_and_no_tpb_marker(self):
        for clock, expected in ((NOW, SV.NORMAL), (NOW + timedelta(microseconds=1), SV.TPB)):
            result = evaluate(title='TPB', description='TPB', issues=(ClassificationIssue('TPB', '2026-01-01'),),
                              evaluated_at=clock)
            self.assertEqual(result.value, expected)
            self.assertEqual(result.evaluated_at, clock)
            self.assertIsNone(result.evaluated_at.tzinfo)
            self.assertEqual(result.facts.issue_age, clock - datetime(2026, 1, 1))
        self.assertEqual(evaluate(issues=(ClassificationIssue('TPB', None),)).source, ClassificationSource.DEFAULT_NORMAL)

    def test_invalid_date_failure_only_when_reached(self):
        inputs = dict(issues=(ClassificationIssue(None, 'invalid'),))
        with self.assertRaises(ValueError):
            evaluate(**inputs)
        result = evaluate(title='Omnibus', **inputs)
        self.assertEqual(result.value, SV.OMNIBUS)
        self.assertIsNone(result.facts.issue_age)

    def test_immutable_result_and_evidence_no_input_mutation(self):
        issues = [ClassificationIssue('Story', '2000-01-01')]
        before = list(issues)
        result = evaluate(issues=issues, format_evidence=PHYSICAL[1])
        self.assertEqual(issues, before)
        for obj, field, value in ((result, 'value', SV.TPB), (result.facts, 'issue_count', 99),
                                  (result.physical_evidence, 'disposition', EvidenceDisposition.NOT_SUPPLIED),
                                  (result.physical_evidence.evidence, 'raw_value', 'Changed')):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field, value)
        self.assertEqual(hash(result), hash(evaluate(issues=issues, format_evidence=PHYSICAL[1])))

    def test_pure_thousand_issue_evaluation_no_io_or_implicit_clock(self):
        issues = tuple(ClassificationIssue('Volume %d' % number, None) for number in range(1000))
        with patch('backend.internals.db.get_db', side_effect=AssertionError('DB access')), \
                patch('builtins.open', side_effect=AssertionError('file access')), \
                patch('socket.socket', side_effect=AssertionError('network access')), \
                patch.object(classification, 'datetime', wraps=datetime) as clock:
            clock.now.side_effect = AssertionError('implicit clock')
            result = evaluate(issues=issues, format_evidence=PHYSICAL[1])
            aged = evaluate(issues=(ClassificationIssue(None, '2000-01-01'),))
        self.assertEqual(result.facts.volume_numbered_count, 1000)
        self.assertEqual(aged.source, ClassificationSource.LEGACY_AGE_COUNT)
        self.assertEqual(result.policy_id, 'kapowarr-special-version/v1')

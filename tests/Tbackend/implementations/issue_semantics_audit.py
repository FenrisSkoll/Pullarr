"""Phase 3C: freeze legacy behavior, including quirks, not future semantics."""

import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from unittest import TestCase

from backend.base.definitions import SpecialVersion
from backend.base.file_extraction import extract_issue_number
from backend.base.helpers import check_overlapping_issues
from backend.implementations.classification import (ClassificationIssue,
                                                    evaluate_special_version)
from backend.implementations.metadata.models import IssueMetadata


class IssueSemanticsAuditTests(TestCase):
    def test_parser_taxonomy(self):
        examples = {'1': 1.0, '01': 1.0, '1.0': 1.0, '1.5': 1.5,
                    '0': 0.0, '-1': -1.0, '1A': 1.01, '1AU': 1.0121,
                    '1-B': 1.02, '1.01': 1.01, '1.1': 1.1,
                    '1/2': (1.0, 2.0), '\u00bd': 0.5, '\u00bc': 0.3,
                    '[nn]': 0.1414, '': None}
        for raw, expected in examples.items():
            with self.subTest(raw=raw):
                self.assertEqual(extract_issue_number(raw), expected)

    def test_text_is_encoded_not_recognized_as_bibliography(self):
        for raw in ('Annual', 'Special', 'Preview', 'Ashcan', 'unnumbered'):
            self.assertIsInstance(extract_issue_number(raw), float)

    def test_suffix_decimal_collision_and_dictionary_loss(self):
        values = {extract_issue_number(raw): raw for raw in ('1A', '1.01')}
        self.assertEqual(values, {1.01: '1.01'})

    def test_ranges_include_pseudonumeric_suffix(self):
        self.assertTrue(check_overlapping_issues(1.01, (1.0, 2.0)))
        self.assertTrue(check_overlapping_issues(1.5, (1.0, 2.0)))

    def test_legacy_overlap_is_directional_for_containing_ranges(self):
        self.assertFalse(check_overlapping_issues((2.0, 3.0), (1.0, 4.0)))
        self.assertTrue(check_overlapping_issues((1.0, 4.0), (2.0, 3.0)))

    def test_sql_number_index_is_not_identity_or_unique(self):
        # Minimal reproduction of relevant schema-53 constraints, not migration.
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE issues(id INTEGER PRIMARY KEY, volume_id INTEGER, '
                       'issue_number TEXT NOT NULL, calculated_issue_number REAL NOT NULL, date TEXT)')
            db.execute('CREATE INDEX numbers ON issues(volume_id, calculated_issue_number)')
            db.executemany('INSERT INTO issues VALUES (?,?,?,?,?)', [
                (1, 1, '1A', 1.01, '2022-01-01'),
                (2, 1, '1.01', 1.01, None),
                (3, 1, '2', 2.0, '2021-01-01')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM issues WHERE '
                                        'volume_id=1 AND calculated_issue_number=1.01').fetchone()[0], 2)
            self.assertEqual([r[0] for r in db.execute(
                'SELECT id FROM issues ORDER BY date, calculated_issue_number')], [2, 3, 1])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO issues VALUES (4,1,'[nn]',NULL,NULL)")

    def test_serialization_keeps_legacy_projection_separate_from_label(self):
        for provider in ('comicvine', 'metron'):
            value = asdict(IssueMetadata(provider, '10', '1', '01', 1.0,
                                         None, '2021-12-25', None))
            self.assertEqual(value, dict(provider=provider, provider_id='10',
                                        volume_provider_id='1', issue_number='01',
                                        calculated_issue_number=1.0, title=None,
                                        date='2021-12-25', description=None))

    def test_exact_clock_boundary_and_partial_date_failure(self):
        def classify(raw, clock):
            return evaluate_special_version(
                title='Example', description=None,
                issues=(ClassificationIssue(None, raw),),
                stored_value=SpecialVersion.NORMAL, locked=False,
                evaluated_at=clock).value
        boundary = datetime(2021, 12, 25) + timedelta(days=30)
        self.assertEqual(classify('2021-12-25', boundary), SpecialVersion.NORMAL)
        self.assertEqual(classify('2021-12-25', boundary + timedelta(microseconds=1)), SpecialVersion.TPB)
        # 3D-A deliberately replaces the legacy partial-date exception with
        # abstention. The complete-day age boundary above remains unchanged.
        self.assertEqual(classify('2021-12-00', boundary), SpecialVersion.NORMAL)

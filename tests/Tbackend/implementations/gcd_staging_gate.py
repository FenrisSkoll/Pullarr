"""Freeze the incompatibilities that staging must not repair globally."""

from dataclasses import fields
from datetime import datetime
from unittest import TestCase

from backend.base.file_extraction import extract_issue_number
from backend.implementations.metadata.models import IssueMetadata
from backend.implementations.metadata.registry import PROVIDERS


class GCDCompatibilityGateTests(TestCase):
    def test_legacy_parser_is_not_an_admission_rule(self):
        self.assertEqual(extract_issue_number('[nn]'), 0.1414)
        self.assertEqual(extract_issue_number('1/2'), (1.0, 2.0))

    def test_partial_date_is_not_a_complete_calendar_date(self):
        for value in ('2021-12-00', '2021-00-00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                datetime.strptime(value, '%Y-%m-%d')

    def test_legacy_core_contract_unchanged_with_rich_provider_registered(self):
        self.assertEqual(next(f.type for f in fields(IssueMetadata)
                              if f.name == 'calculated_issue_number'), float)
        self.assertEqual(set(PROVIDERS), {'comicvine', 'metron', 'gcd'})

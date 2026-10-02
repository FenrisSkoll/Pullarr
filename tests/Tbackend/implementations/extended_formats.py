"""Current semantics only. Mutated edition cases below are synthetic controls."""

import json
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures.collected_titles import record
from Tbackend.implementations.format_contract import FormatHarness

from backend.base.definitions import DateType, SpecialVersion
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.volumes import (determine_special_version,
                                             refresh_and_scan)


class ExtendedFormatSemantics(FormatHarness, TestCase):
    def synthetic(self, kind=None, name='Synthetic book', title='A story'):
        series = record('halloween-hc-metron-series')
        issue = record('halloween-hc-metron-issue')
        series.update(name=name, desc='', series_type={'name': kind})
        issue.update(title=title, name=[], cover_date='2000-01-01')
        return MetronMetadataProvider.volume_result(series, [issue], DateType.COVER_DATE)

    def assert_format(self, result, expected):
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, expected)
        return volume

    def test_vai_with_trade_paperback(self):
        self.assert_format(self.synthetic('Trade Paperback', title='Volume One'), SpecialVersion.VOLUME_AS_ISSUE)

    def test_vai_with_hardcover(self):
        self.assert_format(self.synthetic('Hardcover', title='Volume 1'), SpecialVersion.VOLUME_AS_ISSUE)

    def test_real_multi_book_tpb_is_not_one_tpb(self):
        result = self.mapped('invincible', ['invincible%d-metron-issue' % i for i in (1, 2, 3)])
        self.assert_format(result, SpecialVersion.NORMAL)

    def test_omnibus_title_heuristic(self):
        self.assert_format(self.synthetic(name='Synthetic Omnibus'), SpecialVersion.OMNIBUS)

    def test_one_shot_title_heuristic(self):
        self.assert_format(self.synthetic(name='Synthetic One-Shot'), SpecialVersion.ONE_SHOT)

    def test_graphic_novel_is_unmapped_heuristic_tpb_not_evidence(self):
        result = self.synthetic('Graphic Novel')
        self.assertIsNone(result.format_evidence.physical_format)
        volume = self.assert_format(result, SpecialVersion.TPB)
        self.assertEqual(determine_special_version(volume.id), SpecialVersion.TPB)

    def test_limited_series_does_not_become_one_shot(self):
        self.assert_format(self.synthetic('Limited Series'), SpecialVersion.TPB)

    def test_single_old_issue_is_not_automatically_one_shot(self):
        self.assert_format(self.synthetic('Single Issue'), SpecialVersion.TPB)

    def assert_lock(self, locked):
        result = self.synthetic('Hardcover')
        volume = self.add_result(result, locked)
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched',
                          new=AsyncMock(return_value=result)):
            refresh_and_scan(volume.id)
        self.assertEqual(volume.get_data().special_version, locked)
        self.assertTrue(volume.get_data().special_version_locked)

    def test_one_shot_lock_wins(self):
        self.assert_lock(SpecialVersion.ONE_SHOT)

    def test_omnibus_lock_wins(self):
        self.assert_lock(SpecialVersion.OMNIBUS)

    def test_unknown_and_absent_types_preserve_heuristic(self):
        volume = self.add_result(self.synthetic())
        for kind in (None, 'Unknown', 'Digital Chapter', 'Ongoing Series'):
            with self.subTest(kind=kind):
                result = self.synthetic(kind)
                self.assertEqual(determine_special_version(volume.id, result.format_evidence), SpecialVersion.TPB)

    def test_live_vocabulary_not_an_approved_mapping(self):
        path = Path(__file__).resolve().parents[2] / 'fixtures' / 'collected_editions' / 'metron-series-types.json'
        names = [row['name'] for row in json.loads(path.read_text(encoding='utf-8'))['results']]
        self.assertEqual(len(names), 9)
        self.assertIn('One-Shot', names)
        self.assertNotIn('Ongoing Series', names)
        for kind in names:
            evidence = self.synthetic(kind).format_evidence
            self.assertEqual(evidence.physical_format is not None, kind in ('Hardcover', 'Trade Paperback'))

    def test_omnibus_source_without_publication_transport_is_unmapped(self):
        self.assert_format(replace(self.synthetic('Omnibus'), publication_evidence=None), SpecialVersion.TPB)

    def test_one_shot_source_without_publication_transport_is_unmapped(self):
        self.assert_format(replace(self.synthetic('One-Shot'), publication_evidence=None), SpecialVersion.TPB)

    def test_binding_can_currently_override_omnibus_title(self):
        self.assert_format(self.synthetic('Hardcover', name='Synthetic Omnibus'), SpecialVersion.HARD_COVER)

    def test_multi_book_omnibus_title_and_source_do_not_collapse_children(self):
        result = self.mapped('invincible', ['invincible%d-metron-issue' % i for i in (1, 2, 3)])
        result.metadata.title = 'Synthetic Omnibus Series'
        result = replace(result, format_evidence=replace(
            result.format_evidence, raw_value='Omnibus', physical_format=None))
        self.assert_format(result, SpecialVersion.NORMAL)

"""Freeze classification with the pre-format core-metadata-only transport."""

from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures.collected_titles import cv_issue_snapshot, record
from fixtures.comicvine_fetch import issue_response
from fixtures.comicvine_search import volume_response
from Tbackend.implementations.collected_titles import cv_mapped
from Tbackend.implementations.metron_lifecycle import MetronHarness

from backend.base.definitions import DateType, SpecialVersion
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.volumes import Library, Volume, refresh_and_scan


class FormatHarness(MetronHarness):
    def mapped(self, series='halloween-hc', issue_labels=None):
        issue_labels = issue_labels or [series + '-metron-issue']
        return MetronMetadataProvider.volume_result(record(series + '-metron-series'),
            [record(label) for label in issue_labels], DateType.COVER_DATE)

    def add_result(self, result, special=None):
        with patch('backend.implementations.volumes.fetch_volume_result',
                   new=AsyncMock(return_value=result)):
            return Volume(Library.add_metadata(ProviderVolumeIdentity(
                result.metadata.provider, result.metadata.provider_id), 1, True,
                special_version=special))


class LegacyFormatContract(FormatHarness, TestCase):
    def test_hardcover_evidence_is_lost_in_core_only_transport(self):
        result = self.mapped()
        self.assertEqual(record('halloween-hc-metron-series')['series_type']['name'], 'Hardcover')
        volume = self.add_result(VolumeFetchResult(result.metadata, ()))
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)

    def test_trade_paperback_heuristic_and_meaningful_compendium(self):
        result = self.mapped('kickdown')
        self.assertEqual(self.add_result(VolumeFetchResult(result.metadata, ())).get_data().special_version, SpecialVersion.TPB)
        result = self.mapped('invincible', ['invincible%d-metron-issue' % i for i in (1, 2, 3)])
        volume = self.add_result(VolumeFetchResult(result.metadata, ()))
        self.assertEqual(volume.get_data().special_version, SpecialVersion.NORMAL)
        self.assertEqual(volume.get_issues()[0].title, 'Compendium One')

    def test_cv_hc_tpb_and_vai_unchanged(self):
        for label, expected in (
            ('halloween-hc', SpecialVersion.HARD_COVER), ('overture-deluxe', SpecialVersion.HARD_COVER),
            ('rai', SpecialVersion.HARD_COVER), ('cyberpunk-cv-collected', SpecialVersion.TPB),
            ('saga-trades', SpecialVersion.VOLUME_AS_ISSUE), ('invincible', SpecialVersion.VOLUME_AS_ISSUE),
            ('bprd', SpecialVersion.VOLUME_AS_ISSUE), ('descender', SpecialVersion.VOLUME_AS_ISSUE),
            ('revival', SpecialVersion.VOLUME_AS_ISSUE)):
            key = label + ('-volume' if label.endswith('collected') else '-cv-volume')
            details = ['cyberpunk-cv-collected-issue'] if label.endswith('collected') else []
            metadata = cv_mapped(record(key), cv_issue_snapshot(key, details))
            self.assertEqual(self.add_result(VolumeFetchResult(metadata, ())).get_data().special_version, expected)

    def test_omnibus_one_shot_and_vai_priority(self):
        for index, (name, title, expected) in enumerate((
            ('Omnibus', 'Book', SpecialVersion.OMNIBUS),
            ('One-Shot', 'Book', SpecialVersion.ONE_SHOT),
            ('Omnibus Hardcover', 'Volume 1', SpecialVersion.VOLUME_AS_ISSUE))):
            metadata = cv_mapped(volume_response(id=8000 + index, name=name),
                [issue_response(id=9000 + index, volume={'id': 8000 + index}, name=title)])
            self.assertEqual(self.add_result(VolumeFetchResult(metadata, ())).get_data().special_version, expected)

    def test_lock_and_unlocked_refresh(self):
        result = self.mapped()
        core = VolumeFetchResult(result.metadata, ())
        volume = self.add_result(core, SpecialVersion.OMNIBUS)
        self.assertTrue(volume.get_data().special_version_locked)
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched', new=AsyncMock(return_value=core)):
            refresh_and_scan(volume.id)
            self.assertEqual(volume.get_data().special_version, SpecialVersion.OMNIBUS)
            volume.update({'special_version_locked': False})
            refresh_and_scan(volume.id)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)

    def test_absent_unknown_and_conflicting_source_type_does_not_change_core(self):
        baseline = self.mapped().metadata
        for value in (None, {'name': 'Unknown'}, {'name': 'Trade Paperback'}):
            raw = record('halloween-hc-metron-series')
            raw['series_type'] = value
            result = MetronMetadataProvider.volume_result(raw,
                [record('halloween-hc-metron-issue')], DateType.COVER_DATE)
            self.assertEqual(result.metadata, baseline)

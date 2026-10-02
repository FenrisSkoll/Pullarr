"""Selected-source evidence without title, identity, request or schema changes."""

from dataclasses import asdict, replace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures.collected_titles import record
from Tbackend.implementations.format_contract import FormatHarness

from backend.base.definitions import DateType, SpecialVersion
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.format_evidence import PhysicalFormat
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.naming import generate_issue_name
from backend.implementations.volumes import Volume, refresh_and_scan
from backend.internals.provider_identity import MetadataIdentityError
from frontend.metadata import volume_identity_results


class FormatFidelity(FormatHarness, TestCase):
    def refresh_result(self, volume, result):
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched', new=AsyncMock(return_value=result)):
            refresh_and_scan(volume.id)

    def test_real_hardcover_add_and_presentation_without_retitle(self):
        result = self.mapped()
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.HARD_COVER)
        titles = '; '.join(record('halloween-hc-metron-issue')['name'])
        self.assertEqual(volume.get_issues()[0].title, titles)
        public = volume_identity_results([volume.get_public_data()], True)[0]
        self.assertEqual(public['issues'][0]['display_title'], titles)
        self.assertEqual(public['issues'][0]['display_title_source'], 'existing_issue_title')
        self.assertNotIn('format_evidence', public)
        self.assertNotIn('series_type', public)

    def test_raw_provenance_and_normalized_enum_separate_from_metadata(self):
        result = self.mapped()
        self.assertEqual(asdict(result.format_evidence), {
            'provider': 'metron', 'provider_id': '17376', 'source_field': 'series_type.name',
            'raw_value': 'Hardcover', 'physical_format': PhysicalFormat.HARDCOVER})
        raw = record('halloween-hc-metron-series')
        raw.pop('series_type')
        absent = MetronMetadataProvider.volume_result(raw, [record('halloween-hc-metron-issue')], DateType.COVER_DATE)
        self.assertIsNone(absent.format_evidence)
        self.assertEqual(asdict(result.metadata), asdict(absent.metadata))
        self.assertEqual(result.enrichment, absent.enrichment)

    def test_tpb_real_and_unknown_vocabulary_fallback(self):
        self.assertEqual(self.add_result(self.mapped('kickdown')).get_data().special_version, SpecialVersion.TPB)
        for name in ('Limited Series', 'Ongoing Series', 'Digital', 'Graphic Novel',
                     'Omnibus', 'One Shot', 'One-Shot', 'New Type'):
            raw = record('halloween-hc-metron-series')
            raw['series_type'] = {'name': name}
            result = MetronMetadataProvider.volume_result(raw, [record('halloween-hc-metron-issue')], DateType.COVER_DATE)
            self.assertEqual(result.format_evidence.raw_value, name)
            self.assertIsNone(result.format_evidence.physical_format)
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)

    def test_absent_type_uses_original_heuristic(self):
        result = replace(self.mapped(), format_evidence=None)
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.TPB)

    def test_lock_wins_both_directions_on_add_and_refresh(self):
        for result, locked in ((self.mapped(), SpecialVersion.TPB),
                               (self.mapped('kickdown'), SpecialVersion.HARD_COVER)):
            volume = self.add_result(result, locked)
            self.assertTrue(volume.get_data().special_version_locked)
            self.refresh_result(volume, result)
            self.assertEqual(volume.get_data().special_version, locked)

    def test_unlocked_refresh_updates_then_fresh_read_keeps_classification(self):
        result = self.mapped()
        volume = self.add_result(replace(result, format_evidence=None))
        folder = volume.get_data().folder
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)
        self.refresh_result(volume, result)
        self.assertEqual(Volume(volume.id).get_data().special_version, SpecialVersion.HARD_COVER)
        self.assertEqual(volume.get_data().folder, folder)
        self.assertEqual(volume.get_issues()[0].title, result.metadata.issues[0].title)
        # Naming engine is unchanged: uses the corrected existing format label.
        self.assertTrue(generate_issue_name(volume.get_data(), 1.0).endswith(' HC'))

    def test_vai_is_stronger_than_physical_binding(self):
        result = self.mapped('saga-trades', ['saga-trade1-metron-issue', 'saga-trade2-metron-issue'])
        # Explicit synthetic complete subset; not all twelve real Saga details.
        result.metadata.issue_count = 2
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)
        self.refresh_result(volume, result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)

    def test_multi_book_compendium_keeps_logical_normal_model(self):
        result = self.mapped('invincible', ['invincible%d-metron-issue' % i for i in (1, 2, 3)])
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.NORMAL)
        self.refresh_result(volume, result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.NORMAL)

    def test_single_issue_vai_beats_hardcover_but_lock_beats_vai(self):
        result = self.mapped()
        result.metadata.issues[0].title = 'Volume 1'
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)
        volume.update({'special_version': SpecialVersion.TPB, 'special_version_locked': True})
        self.refresh_result(volume, result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)

    def test_foreign_evidence_rejected_before_add_and_refresh_writes(self):
        result = self.mapped()
        for provider, identity in (('comicvine', '139540'), ('metron', '999')):
            invalid = replace(result, format_evidence=replace(result.format_evidence, provider=provider, provider_id=identity))
            with self.assertRaises(MetadataIdentityError):
                self.add_result(invalid)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)
        volume = self.add_result(result)
        before = self.state()
        with self.assertRaises(MetadataIdentityError):
            self.refresh_result(volume, invalid)
        self.assertEqual(self.state(), before)

    def test_refresh_sql_count_parity(self):
        result = self.mapped()
        volume = self.add_result(result)
        counts = []
        for fetch in (replace(result, format_evidence=None), result):
            statements = []
            self.db.set_trace_callback(statements.append)
            try:
                self.refresh_result(volume, fetch)
            finally:
                self.db.set_trace_callback(None)
            counts.append(sum(s.lstrip().upper().startswith('SELECT') for s in statements))
        self.assertEqual(counts[0], counts[1])
        print('Format refresh SELECT parity:', counts)

    def test_real_client_path_manual_and_scheduled_no_extra_requests(self):
        self.series['series_type'] = {'id': 8, 'name': 'Hardcover'}
        self.series['issue_count'] = 1
        self.issues = self.issues[:1]
        local = self.add_metron()
        self.assertEqual(self.http.get.call_count, 3)  # series, list, cold detail
        self.assertEqual(Volume(local).get_data().special_version, SpecialVersion.HARD_COVER)
        for scheduled in (False, True):
            Volume(local).update({'special_version': SpecialVersion.TPB})
            self.http.get.reset_mock()
            refresh_and_scan(None if scheduled else local, allow_skipping=False)
            self.assertEqual(self.http.get.call_count, 2)  # series + list, unchanged detail cache
            self.assertEqual(Volume(local).get_data().special_version, SpecialVersion.HARD_COVER)
        self.session.get.assert_not_called()  # no ComicVine fallback

    def test_provider_tpb_can_override_synthetic_hc_heuristic_when_unlocked(self):
        result = self.mapped('kickdown')
        result.metadata.title = 'Synthetic Hardcover title'
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.TPB)

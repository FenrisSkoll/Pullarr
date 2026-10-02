"""Fetch-scoped publication evidence; no binding/title/identity conflation."""

from dataclasses import asdict, replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures.publication_kinds import mapped, snapshot
from Tbackend.implementations.format_contract import FormatHarness

from backend.base.definitions import (DateType, QueryKeys,
                                      SearchAction, SpecialVersion)
from backend.implementations.file_matching import scan_files
from backend.implementations.metadata.format_evidence import (
    PhysicalFormat, ProviderFormatEvidence)
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.metadata.publication_evidence import \
    PublicationKind
from backend.implementations.naming import generate_issue_name
from backend.implementations.query_builders.DDL import DDLQueryBuilder
from backend.implementations.volumes import Volume, refresh_and_scan
from backend.internals.provider_identity import MetadataIdentityError
from frontend.metadata import volume_identity_results


class PublicationFidelity(FormatHarness, TestCase):
    def refresh(self, volume, result):
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched', new=AsyncMock(return_value=result)):
            refresh_and_scan(volume.id)

    def synthetic(self, kind):
        raw = snapshot(1530)
        raw['series']['series_type'] = {'id': 5, 'name': kind}
        return MetronMetadataProvider.volume_result(raw['series'], raw['issues'], DateType.COVER_DATE)

    def test_exact_one_shot_and_omnibus_evidence_provenance(self):
        for identity, kind, raw in ((1530, PublicationKind.ONE_SHOT, 'One-Shot'),
                                    (17494, PublicationKind.OMNIBUS, 'Omnibus')):
            result = mapped(identity)
            self.assertEqual(asdict(result.publication_evidence), {
                'provider': 'metron', 'provider_id': str(identity),
                'source_field': 'series_type.name', 'raw_value': raw, 'publication_kind': kind})
            self.assertIsNone(result.format_evidence.physical_format)

    def test_core_serialization_and_enrichment_unchanged(self):
        for identity in (1530, 17494):
            raw = snapshot(identity)
            result = mapped(identity)
            raw['series'].pop('series_type')
            absent = MetronMetadataProvider.volume_result(raw['series'], raw['issues'], DateType.COVER_DATE)
            self.assertEqual(asdict(result.metadata), asdict(absent.metadata))
            self.assertEqual(result.enrichment, absent.enrichment)
            self.assertIsNone(absent.publication_evidence)

    def test_real_one_shot_add_and_meaningful_omnibus_title(self):
        for identity, expected in ((10307, SpecialVersion.ONE_SHOT), (1530, SpecialVersion.ONE_SHOT),
                                   (17494, SpecialVersion.OMNIBUS)):
            result = mapped(identity)
            volume = self.add_result(result)
            self.assertEqual(volume.get_data().special_version, expected)
            self.assertFalse(volume.get_data().special_version_locked)
            self.assertEqual(volume.get_issues()[0].title, result.metadata.issues[0].title)

    def test_unlocked_manual_refresh_updates_without_renaming(self):
        for identity, expected in ((1530, SpecialVersion.ONE_SHOT), (17494, SpecialVersion.OMNIBUS)):
            result = mapped(identity)
            volume = self.add_result(replace(result, publication_evidence=None))
            self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)
            before = [(i.id, i.title, i.monitored) for i in volume.get_issues()]
            folder = volume.get_data().folder
            self.refresh(volume, result)
            self.assertEqual(Volume(volume.id).get_data().special_version, expected)
            self.assertEqual([(i.id, i.title, i.monitored) for i in volume.get_issues()], before)
            self.assertEqual(volume.get_data().folder, folder)

    def test_real_multi_book_omnibus_remains_vai(self):
        result = mapped(14110)
        volume = self.add_result(result)
        self.refresh(volume, result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)
        self.assertEqual(len(volume.get_issues()), 3)

    def test_multi_book_without_vai_is_not_collapsed(self):
        result = mapped(14110)
        for i in result.metadata.issues:
            i.title = 'Synthetic book'
        volume = self.add_result(result)
        for kind in (PublicationKind.ONE_SHOT, PublicationKind.OMNIBUS):
            result = replace(result, publication_evidence=replace(result.publication_evidence, publication_kind=kind))
            self.refresh(volume, result)
            self.assertEqual(volume.get_data().special_version, SpecialVersion.NORMAL)

    def test_vai_beats_both_publication_kinds(self):
        for identity in (1530, 17494):
            result = mapped(identity)
            result.metadata.issues[0].title = 'Volume 1'
            volume = self.add_result(result)
            self.refresh(volume, result)
            self.assertEqual(volume.get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)

    def test_locks_beat_publication_including_vai(self):
        for identity in (1530, 17494):
            result = mapped(identity)
            result.metadata.issues[0].title = 'Volume 1'
            volume = self.add_result(result, SpecialVersion.HARD_COVER)
            self.refresh(volume, result)
            self.assertTrue(volume.get_data().special_version_locked)
            self.assertEqual(volume.get_data().special_version, SpecialVersion.HARD_COVER)

    def test_publication_only_envelope_respects_locked_normal(self):
        result = replace(mapped(1530), format_evidence=None)
        volume = self.add_result(result, SpecialVersion.NORMAL)
        self.refresh(volume, result)
        self.assertTrue(volume.get_data().special_version_locked)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.NORMAL)

    def test_exact_only_unknown_graphic_limited_single_digital(self):
        volume = self.add_result(self.synthetic(None))
        for kind in (None, '', 'Graphic Novel', 'Limited Series', 'Single Issue', 'Digital Chapter',
                     'One Shot', 'One-Shot Series', 'one-shot', ' One-Shot ',
                     'Omni', 'Omnibus Edition', 'Unknown'):
            with self.subTest(kind=kind):
                result = self.synthetic(kind)
                if result.publication_evidence:
                    self.assertIsNone(result.publication_evidence.publication_kind)
                self.refresh(volume, result)
                self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)

    def test_one_issue_without_evidence_is_not_one_shot(self):
        result = mapped(1530)
        result = replace(result, publication_evidence=None)
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.TPB)

    def test_evidence_disappearance_falls_back_on_refresh(self):
        result = mapped(1530)
        volume = self.add_result(result)
        self.refresh(volume, replace(result, publication_evidence=None))
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)

    def test_partial_or_duplicate_snapshot_is_rejected_before_writes(self):
        result = mapped(1530)
        result.metadata.issue_count = 2
        with self.assertRaises(MetadataIdentityError):
            self.add_result(result)
        self.assert_empty_library()
        result.metadata.issues *= 2
        with self.assertRaises(MetadataIdentityError):
            self.add_result(result)
        self.assert_empty_library()

    def test_existing_search_queries_change_mode_not_query_builder(self):
        for mode in (SpecialVersion.ONE_SHOT, SpecialVersion.OMNIBUS):
            keys = QueryKeys(['Book'], 2014, 1, mode, None)
            query = DDLQueryBuilder().next_query(SearchAction.SEARCH_VOLUME, keys)
            self.assertEqual(query['query'], 'Book (2014)')
        keys.special_version = SpecialVersion.TPB
        self.assertEqual(DDLQueryBuilder().next_query(SearchAction.SEARCH_VOLUME, keys)['query'],
                         'Book Vol. 1 (2014) TPB')

    def test_hc_tpb_unchanged(self):
        for label, expected in (('halloween-hc', SpecialVersion.HARD_COVER), ('kickdown', SpecialVersion.TPB)):
            result = self.mapped(label)
            self.assertIsNone(result.publication_evidence.publication_kind)
            self.assertEqual(self.add_result(result).get_data().special_version, expected)

    def test_cross_axis_conflict_declines_both_not_arbitrary_precedence(self):
        for identity in (1530, 17494):
            result = mapped(identity)
            result = replace(result, format_evidence=ProviderFormatEvidence(
                'metron', str(identity), 'synthetic.binding', 'Hardcover', PhysicalFormat.HARDCOVER))
            volume = self.add_result(result)
            self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)  # old heuristic, neither hint
            result.metadata.issues[0].title = 'Volume 1'
            self.refresh(volume, result)
            self.assertEqual(volume.get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)
            volume.update({'special_version': SpecialVersion.OMNIBUS, 'special_version_locked': True})
            self.refresh(volume, result)
            self.assertEqual(volume.get_data().special_version, SpecialVersion.OMNIBUS)

    def test_foreign_owner_rejected_before_any_add_or_refresh_write(self):
        result = mapped(1530)
        for provider, identity in (('comicvine', '75888'), ('metron', '999'), ('unknown', '1530')):
            invalid = replace(result, publication_evidence=replace(
                result.publication_evidence, provider=provider, provider_id=identity))
            with self.assertRaises(MetadataIdentityError):
                self.add_result(invalid, SpecialVersion.TPB)  # even a lock cannot admit foreign evidence
            self.assert_empty_library()
        volume = self.add_result(result)
        before = self.state()
        with self.assertRaises(MetadataIdentityError):
            self.refresh(volume, invalid)
        self.assertEqual(self.state(), before)

    def test_title_presentation_api_shape_and_naming_defaults(self):
        defaults = (self.settings.file_naming, self.settings.file_naming_special_version)
        for identity, suffix in ((1530, 'OS'), (17494, 'Omnibus')):
            result = mapped(identity)
            volume = self.add_result(result)
            public = volume_identity_results([volume.get_public_data()], True)[0]
            issue = public['issues'][0]
            self.assertEqual(issue['title'], result.metadata.issues[0].title)
            self.assertEqual(issue['display_title'], issue['title'])
            self.assertNotIn('publication_evidence', public)
            self.assertNotIn('publication_kind', public)
            self.assertTrue(generate_issue_name(volume.get_data(), 1.0).endswith(' ' + suffix))
        self.assertEqual((self.settings.file_naming, self.settings.file_naming_special_version), defaults)

    def test_refresh_select_count_parity(self):
        result = mapped(1530)
        volume = self.add_result(result)
        counts = []
        for fetch in (replace(result, publication_evidence=None), result):
            statements = []
            self.db.set_trace_callback(statements.append)
            try:
                self.refresh(volume, fetch)
            finally:
                self.db.set_trace_callback(None)
            counts.append(sum(s.lstrip().upper().startswith('SELECT') for s in statements))
        self.assertEqual(counts[0], counts[1])
        print('Publication refresh SELECT parity:', counts)

    def test_add_select_count_parity(self):
        result = mapped(1530)
        counts = []
        for fetch in (replace(result, publication_evidence=None), result):
            statements = []
            self.db.set_trace_callback(statements.append)
            try:
                self.add_result(fetch)
            finally:
                self.db.set_trace_callback(None)
            counts.append(sum(s.lstrip().upper().startswith('SELECT') for s in statements))
            # Reset only the isolated in-memory test domain between identical adds.
            self.db.execute('DELETE FROM volumes')
            self.db.commit()
        self.assertEqual(counts[0], counts[1])
        print('Publication add SELECT parity:', counts)

    def test_client_add_manual_and_scheduled_no_extra_requests(self):
        self.series['series_type'] = {'name': 'One-Shot'}
        self.series['issue_count'] = 1
        self.issues = self.issues[:1]
        local = self.add_metron()
        self.assertEqual(self.http.get.call_count, 3)
        self.assertEqual(Volume(local).get_data().special_version, SpecialVersion.ONE_SHOT)
        for kind, expected in (('One-Shot', SpecialVersion.ONE_SHOT), ('Omnibus', SpecialVersion.OMNIBUS)):
            self.series['series_type']['name'] = kind
            for scheduled in (False, True):
                self.http.get.reset_mock()
                refresh_and_scan(None if scheduled else local, allow_skipping=False)
                self.assertEqual(self.http.get.call_count, 2)
                self.assertEqual(Volume(local).get_data().special_version, expected)
        self.session.get.assert_not_called()

    def test_actual_file_association_uses_only_the_sole_local_issue(self):
        # Reuse the production scanner on disposable files, not the harness mock.
        for module in ('backend.implementations.file_matching',):
            self.start_patch(module + '.get_db', side_effect=self.db.cursor)
            self.start_patch(module + '.Settings').return_value.get_settings.return_value = self.settings
        for identity in (1530, 17494):
            volume = self.add_result(mapped(identity))
            path = Path(volume.get_data().folder) / (generate_issue_name(volume.get_data(), 1.0) + '.cbz')
            path.write_bytes(b'disposable test file')
            scan_files(volume.id)
            links = self.db.execute('''SELECT i.id FROM issues_files b JOIN issues i ON i.id=b.issue_id
                WHERE i.volume_id=?''', (volume.id,)).fetchall()
            self.assertEqual(links, [(volume.get_issues()[0].id,)])
            self.assertEqual(path.read_bytes(), b'disposable test file')

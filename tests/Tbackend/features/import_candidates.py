"""Offline candidate evidence, read-only identity and legacy projection contracts."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fixtures.library_import import ImportHarness
from Tbackend.features.library_import_providers import ProviderImportHarness

from backend.base.file_extraction import extract_filename_data
from backend.base.import_candidate import (BibliographicObservation,
                                           CandidateDiagnostic, ClaimRole,
                                           ComicInfoObservation,
                                           CoverageHypothesis, CoverageKind,
                                           DiagnosticCode, DiagnosticKind,
                                           DiscoveryScope, EvidenceSource,
                                           ExistingFileIdentity,
                                           IdentificationState,
                                           InspectionState, LocalAssociation,
                                           MatchHypothesis, NumberSemantics,
                                           Provenance, ProviderIdentityClaim,
                                           ProviderReference, ResourceKind,
                                           ReviewState)
from backend.features.library_import import (import_library,
                                             propose_library_import)
from backend.implementations.import_candidates import (
    legacy_filename_inputs, observe_import_candidate)
from backend.implementations.metadata.registry import PROVIDERS
from backend.internals.import_identity import load_existing_import_identities
from backend.internals.provider_identity import MetadataIdentityError

CLOCK = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
DB = Provenance(EvidenceSource.DATABASE, 'issues_files')
XML = Provenance(EvidenceSource.COMICINFO, 'ComicInfo.xml/Web')


def volume(provider='metron', identity='ABC'):
    return ProviderReference(provider, ResourceKind.VOLUME, identity)


def association(issue=10, forced=False):
    return LocalAssociation(7, issue, forced, volume(), DB)


class CandidateModel(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'Batman 001 (2020).CBZ'
        self.path.write_bytes(b'not an archive')
        self.scope = DiscoveryScope('run-1', str(self.root))
        self.candidate = observe_import_candidate(
            str(self.path), self.scope, observed_at=CLOCK, candidate_id='file-1')

    def test_raw_path_filename_scope_and_freshness(self):
        candidate = self.candidate
        self.assertEqual(candidate.file.path, str(self.path))
        self.assertEqual(candidate.file.raw_name, self.path.name)
        self.assertEqual(candidate.file.extension, '.CBZ')
        self.assertEqual(candidate.file.raw_stem, 'Batman 001 (2020)')
        self.assertEqual(candidate.file.observed_at, CLOCK)
        self.assertEqual(candidate.file.size, self.path.stat().st_size)
        self.assertEqual(candidate.file.mtime_ns, self.path.stat().st_mtime_ns)
        self.assertEqual(candidate.folder.relative_path, self.path.name)
        self.assertTrue(candidate.folder.at_root)
        self.assertEqual(candidate.scope.run_id, 'run-1')

    def test_changed_stat_is_observable_not_permanent_identity(self):
        self.path.write_bytes(b'changed size')
        later = observe_import_candidate(str(self.path), self.scope)
        self.assertNotEqual(later.file.size, self.candidate.file.size)
        self.assertNotEqual(later.candidate_id, self.candidate.candidate_id)

    def test_folder_spelling_cannot_claim_local_ownership(self):
        with self.assertRaises(ValueError):
            replace(self.candidate.folder, local_volume_ids=(7,))
        folder = replace(self.candidate.folder, local_volume_ids=(7,), provenance=DB)
        self.assertEqual(replace(self.candidate, folder=folder).folder.provenance, DB)

    def test_filename_is_heuristic_projection_and_detached(self):
        parsed = extract_filename_data(str(self.path), prefer_folder_year=True)
        candidate = observe_import_candidate(str(self.path), self.scope, parsed=parsed)
        self.assertEqual(candidate.filename.provenance.source, EvidenceSource.LEGACY_PARSER)
        self.assertEqual(candidate.filename.number_semantics, NumberSemantics.LEGACY_PROJECTION)
        before = candidate.filename.to_legacy()
        parsed['series'] = 'mutated'
        projected = candidate.filename.to_legacy()
        projected['issue_number'] = 999
        self.assertEqual(candidate.filename.to_legacy(), before)
        self.assertEqual(candidate.identification, IdentificationState.UNIDENTIFIED)

    def test_frozen_evidence_and_mutable_container_rejected(self):
        with self.assertRaises(FrozenInstanceError):
            self.candidate.file.path = 'changed'
        with self.assertRaises(TypeError):
            replace(self.candidate, claims=[])
        with self.assertRaises(TypeError):
            replace(self.candidate, comicinfo=ComicInfoObservation(
                InspectionState.PRESENT, fields=(['Number', '1'],), provenance=XML))

    def test_known_identity_bypasses_heuristic_rediscovery(self):
        known = ExistingFileIdentity(42, (association(10, True), association(11)))
        with patch('backend.implementations.import_candidates.extract_filename_data') as parser:
            candidate = observe_import_candidate(str(self.path), self.scope, existing=known)
        parser.assert_not_called()
        self.assertEqual(candidate.existing.file_id, 42)
        self.assertEqual([a.issue_id for a in candidate.existing.associations], [10, 11])
        self.assertTrue(candidate.existing.associations[0].forced)
        self.assertEqual(candidate.existing.associations[0].selected_volume, volume())
        self.assertEqual(candidate.identification, IdentificationState.IDENTIFIED)
        self.assertEqual(candidate.file.raw_name, self.path.name)

    def test_namespaces_and_resource_kinds_are_identity(self):
        self.assertNotEqual(volume('comicvine', '123'), volume('metron', '123'))
        self.assertNotEqual(volume('metron', '123'), ProviderReference(
            'metron', ResourceKind.ISSUE, '123'))
        for provider, identity in (('', '1'), ('metron', ''), ('metron', 123)):
            with self.assertRaises(ValueError):
                volume(provider, identity)

    def test_cross_reference_never_becomes_authority(self):
        cross = ProviderIdentityClaim(volume('comicvine', '123'), ClaimRole.CROSS_REFERENCE, DB)
        known = ExistingFileIdentity(42, (association(),), (cross,))
        candidate = replace(self.candidate, existing=known, claims=(replace(cross, role=ClaimRole.EMBEDDED),))
        self.assertFalse(candidate.conflicts)
        self.assertEqual(candidate.existing.associations[0].selected_volume, volume())

    def test_unverified_cross_provider_embedded_claim_requires_review(self):
        claim = ProviderIdentityClaim(volume('comicvine', '123'), ClaimRole.EMBEDDED, XML)
        candidate = replace(self.candidate, existing=ExistingFileIdentity(42, (association(),)), claims=(claim,))
        self.assertEqual(candidate.review, ReviewState.REQUIRED)
        self.assertEqual(candidate.conflicts[0].code, DiagnosticCode.UNVERIFIED_IDENTITY_RELATION)
        self.assertEqual(candidate.existing.associations[0].selected_volume, volume())

    def test_same_provider_contradictions_retained_deterministically(self):
        claims = tuple(ProviderIdentityClaim(volume('metron', identity), ClaimRole.EMBEDDED, XML)
                       for identity in ('A', 'B'))
        candidate = replace(self.candidate, claims=claims)
        self.assertEqual(candidate.claims, claims)
        self.assertEqual(candidate.identification, IdentificationState.CONFLICTED)
        self.assertEqual(candidate.conflicts, candidate.conflicts)
        self.assertEqual(candidate.conflicts[0].code, DiagnosticCode.IDENTITY_DISAGREEMENT)

    def test_issue_claim_parent_conflict_and_many_issue_claims(self):
        issues = tuple(ProviderIdentityClaim(
            ProviderReference('metron', ResourceKind.ISSUE, identity),
            ClaimRole.EMBEDDED, XML, volume()) for identity in ('1', '2'))
        candidate = replace(self.candidate, existing=ExistingFileIdentity(42, (association(),)), claims=issues)
        self.assertFalse(candidate.conflicts)
        candidate = replace(candidate, claims=(replace(issues[0], parent=volume(identity='OTHER')),))
        self.assertEqual(candidate.identification, IdentificationState.CONFLICTED)

    def test_invalid_parent_namespace_rejected(self):
        with self.assertRaises(ValueError):
            ProviderIdentityClaim(ProviderReference('metron', ResourceKind.ISSUE, '1'),
                                  ClaimRole.EMBEDDED, XML, volume('comicvine', '2'))

    def test_alternatives_do_not_select_a_winner(self):
        coverage = CoverageHypothesis(CoverageKind.UNKNOWN, XML)
        first = MatchHypothesis(volume(identity='A'), XML, coverage, reasons=('exact-title',))
        one = replace(self.candidate, alternatives=(first,))
        self.assertEqual(one.identification, IdentificationState.UNIDENTIFIED)
        two = replace(one, alternatives=(first, replace(first, volume=volume(identity='B'))))
        self.assertEqual(two.review, ReviewState.REQUIRED)
        self.assertEqual(two.identification, IdentificationState.AMBIGUOUS)
        self.assertEqual(len(two.alternatives), 2)

    def test_warning_does_not_erase_identification_or_conflict(self):
        warning = CandidateDiagnostic(DiagnosticKind.WARNING,
                                      DiagnosticCode.BIBLIOGRAPHIC_DISAGREEMENT, XML)
        known = replace(self.candidate, existing=ExistingFileIdentity(42, (association(),)), diagnostics=(warning,))
        self.assertEqual(known.identification, IdentificationState.IDENTIFIED)
        conflict = replace(warning, kind=DiagnosticKind.CONFLICT, code=DiagnosticCode.IDENTITY_DISAGREEMENT)
        self.assertEqual(replace(known, diagnostics=(warning, conflict)).review, ReviewState.REQUIRED)

    def test_raw_opaque_suffix_partial_dates_never_projected(self):
        for number in ('[nn]', '1A', '', None, '1', '01', '1.5'):
            for date in ('2021-12-00', '2021-00-00', None):
                fact = BibliographicObservation(number, date, XML)
                candidate = replace(self.candidate, bibliography=(fact,))
                self.assertEqual(candidate.bibliography[0].raw_number, number)
                self.assertEqual(candidate.bibliography[0].raw_date, date)
                self.assertEqual(fact.number_semantics, NumberSemantics.UNINTERPRETED)
                self.assertFalse(hasattr(fact, 'calculated_issue_number'))
                self.assertFalse(hasattr(fact, 'complete_date'))

    def test_uninspected_is_distinct_from_absent_and_failed(self):
        self.assertEqual(self.candidate.comicinfo.state, InspectionState.NOT_INSPECTED)
        self.assertEqual(self.candidate.archive_state, InspectionState.NOT_INSPECTED)
        absent = ComicInfoObservation(InspectionState.ABSENT, provenance=XML)
        self.assertNotEqual(absent, self.candidate.comicinfo)
        with self.assertRaises(ValueError):
            ComicInfoObservation(InspectionState.ABSENT, raw_bytes=b'<ComicInfo/>', provenance=XML)

    def test_future_comicinfo_can_retain_unknown_fields_and_bytes(self):
        xml = b'<ComicInfo><Unknown>value</Unknown><Number>[nn]</Number></ComicInfo>'
        info = ComicInfoObservation(InspectionState.PRESENT,
                                   (('Unknown', 'value'), ('Number', '[nn]')), xml, XML)
        enriched = replace(self.candidate, comicinfo=info)
        self.assertEqual(enriched.comicinfo.raw_bytes, xml)
        self.assertEqual(self.candidate.comicinfo.state, InspectionState.NOT_INSPECTED)

    def test_failed_path_is_explicitly_blocked_not_fabricated_stat(self):
        with patch('backend.implementations.import_candidates.stat', side_effect=PermissionError):
            candidate = observe_import_candidate(str(self.path), self.scope)
        self.assertEqual(candidate.file.stat_state, InspectionState.FAILED)
        self.assertIsNone(candidate.file.size)
        self.assertIsNone(candidate.file.mtime_ns)
        self.assertEqual(candidate.review, ReviewState.BLOCKED)
        self.assertEqual(candidate.diagnostics[0].code, DiagnosticCode.PATH_UNREADABLE)

    def test_missing_path_and_directory_are_not_successful_file_observations(self):
        for path, code in ((self.root / 'missing', DiagnosticCode.PATH_MISSING),
                           (self.root, DiagnosticCode.PATH_NOT_FILE)):
            candidate = observe_import_candidate(str(path), self.scope)
            self.assertEqual(candidate.review, ReviewState.BLOCKED)
            self.assertEqual(candidate.diagnostics[0].code, code)

    def test_multiple_local_volumes_require_review_without_dropping_associations(self):
        known = ExistingFileIdentity(42, (association(), replace(association(11), volume_id=8)))
        candidate = replace(self.candidate, existing=known)
        self.assertEqual(candidate.review, ReviewState.REQUIRED)
        self.assertEqual(len(candidate.existing.associations), 2)

    def test_unavailable_operation_is_not_a_negative_match(self):
        diagnostic = CandidateDiagnostic(DiagnosticKind.UNAVAILABLE,
                                         DiagnosticCode.RANGE_SEMANTICS_UNAVAILABLE, XML)
        candidate = replace(self.candidate, diagnostics=(diagnostic,))
        self.assertEqual(candidate.diagnostics[0].kind, DiagnosticKind.UNAVAILABLE)
        self.assertEqual(candidate.identification, IdentificationState.UNIDENTIFIED)
        self.assertFalse(candidate.conflicts)

    def test_parser_failure_preserves_raw_path_and_warning(self):
        with patch('backend.implementations.import_candidates.extract_filename_data',
                   side_effect=ValueError('synthetic parser failure')):
            candidate = observe_import_candidate(str(self.path), self.scope)
        self.assertEqual(candidate.file.raw_name, self.path.name)
        self.assertIsNone(candidate.filename)
        self.assertEqual(candidate.diagnostics[0].code, DiagnosticCode.PARSE_FAILED)
        self.assertEqual(candidate.review, ReviewState.CONTINUE)

    def test_construction_has_no_archive_network_or_filesystem_writes(self):
        with patch('builtins.open', side_effect=AssertionError('open')), \
                patch('socket.create_connection', side_effect=AssertionError('network')), \
                patch('os.rename', side_effect=AssertionError('rename')), \
                patch('os.remove', side_effect=AssertionError('delete')), \
                patch('os.mkdir', side_effect=AssertionError('mkdir')), \
                patch('shutil.move', side_effect=AssertionError('move')), \
                patch('shutil.copy', side_effect=AssertionError('copy')):
            candidate = observe_import_candidate(str(self.path), self.scope)
        self.assertEqual(candidate.file.stat_state, InspectionState.PRESENT)
        self.assertEqual(self.path.read_bytes(), b'not an archive')

    def test_legacy_projection_parity_matrix(self):
        for name in ('Hero #1.cbz', 'Hero #1-2 (2020).cbz', 'Hero #1A.cbz',
                     'Hero Vol. 2 HC.cbz', 'Hero TPB.cbz', 'Hero Omnibus.cbz',
                     'Hero Annual.cbz', 'Hero [nn].cbz', 'Hero 01.jpg'):
            path = str(self.root / name)
            parsed = extract_filename_data(path, prefer_folder_year=True)
            candidate = observe_import_candidate(path, self.scope, parsed=parsed)
            self.assertEqual(legacy_filename_inputs({path: candidate})[path], parsed)
            if isinstance(parsed['issue_number'], tuple):
                self.assertEqual(candidate.coverage[0].legacy_endpoints, parsed['issue_number'])


class CandidateIdentityDatabase(ImportHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('backend.internals.import_identity.get_db', side_effect=self.db.cursor)

    def test_real_multi_issue_forced_associations_read_without_writes(self):
        path = self.comic_file(name='Example Hero v2 #1-2 (2021).cbz')
        import_library([{'id': 2127, 'filepath': path}])
        self.db.execute('UPDATE issues_files SET forced=1 WHERE issue_id=1')
        before = self.db.total_changes
        statements = []
        self.db.set_trace_callback(statements.append)
        known = load_existing_import_identities([path] * 1000, PROVIDERS.keys())[path]
        self.db.set_trace_callback(None)
        self.assertEqual([a.issue_id for a in known.associations], [1, 2])
        self.assertEqual([a.forced for a in known.associations], [True, False])
        self.assertEqual(known.associations[0].selected_volume, volume('comicvine', '2127'))
        self.assertEqual(known.associations[0].selected_issue,
                         ProviderReference('comicvine', ResourceKind.ISSUE, '301'))
        self.assertEqual(known.associations[0].issue_number, '1')
        self.assertIsNotNone(known.associations[0].volume_title)
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(len(statements), 1)

    def test_no_association_is_not_invented(self):
        self.assertEqual(load_existing_import_identities(['unknown'], PROVIDERS.keys()), {})

    def test_batched_identity_reads_have_no_per_path_queries(self):
        statements = []
        self.db.set_trace_callback(statements.append)
        result = load_existing_import_identities(
            [str(self.root / str(index)) for index in range(1001)], PROVIDERS.keys())
        self.db.set_trace_callback(None)
        self.assertEqual(result, {})
        self.assertEqual(len(statements), 3)

    def test_general_file_binding_keeps_volume_and_forced_flag_without_issue(self):
        path = self.comic_file()
        import_library([{'id': 2127, 'filepath': path}])
        file_id = self.db.execute('SELECT id FROM files WHERE filepath=?', (path,)).fetchone()[0]
        self.db.execute('DELETE FROM issues_files WHERE file_id=?', (file_id,))
        self.db.execute('INSERT INTO volume_files(file_id,volume_id,file_type,forced) VALUES(?,1,?,1)',
                        (file_id, 'metadata'))
        known = load_existing_import_identities([path], PROVIDERS.keys())[path]
        self.assertEqual(known.associations[0].volume_id, 1)
        self.assertIsNone(known.associations[0].issue_id)
        self.assertTrue(known.associations[0].forced)

    def test_legacy_proposal_does_not_change_on_new_stat_diagnostic(self):
        self.comic_file()
        original = propose_library_import(auto_match=False)
        with patch('backend.implementations.import_candidates.stat', side_effect=PermissionError):
            self.assertEqual(propose_library_import(auto_match=False), original)

    def test_library_import_creates_candidate_and_preserves_proposal(self):
        path = self.comic_file()
        with patch('backend.features.library_import.observe_import_candidate',
                   wraps=observe_import_candidate) as observer:
            rows = propose_library_import(auto_match=False)
        observer.assert_called_once()
        self.assertEqual(observer.call_args.args[0], path)
        self.assertTrue(observer.call_args.args[1].manual_only)
        self.assertEqual(set(rows[0]), {'filepath', 'file_title', 'cv', 'metadata_source', 'group_number'})
        self.assertEqual(rows[0]['filepath'], path)
        self.assertIsNone(rows[0]['metadata_source'])

    def test_image_folder_projection_keeps_real_file_observation(self):
        path = self.comic_file(folder='Hero #1 (2021)', name='01.jpg')
        self.comic_file(folder='Hero #1 (2021)', name='02.jpg')
        observed = []

        def capture(*args, **kwargs):
            candidate = observe_import_candidate(*args, **kwargs)
            observed.append(candidate)
            return candidate

        with patch('backend.features.library_import.observe_import_candidate', side_effect=capture):
            rows = propose_library_import(auto_match=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['filepath'], str(Path(path).parent))
        self.assertTrue(all(Path(c.file.path).is_file() for c in observed))


class CandidateMetronIdentity(ProviderImportHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('backend.internals.import_identity.get_db', side_effect=self.db.cursor)

    def test_selected_metron_preserved_without_search(self):
        self.series['cv_id'] = 2127
        path = self.metron_file()
        import_library([self.mapping(path)])
        self.session.get.reset_mock()
        known = load_existing_import_identities([path], PROVIDERS.keys())[path]
        candidate = observe_import_candidate(str(path), DiscoveryScope('run', str(self.root)), existing=known)
        self.assertEqual(candidate.existing.associations[0].selected_volume, volume('metron', '700'))
        self.assertTrue(any(c.reference == volume('comicvine', '2127') for c in known.references))
        self.session.get.assert_not_called()

    def test_missing_selected_identity_fails_without_repair(self):
        path = self.metron_file()
        import_library([self.mapping(path)])
        self.db.execute("DELETE FROM volume_external_ids WHERE provider='metron'")
        before = self.db.total_changes
        with self.assertRaises(MetadataIdentityError):
            load_existing_import_identities([path], PROVIDERS.keys())
        self.assertEqual(self.db.total_changes, before)

"""Policy contracts: synthetic observations, no archives, no providers."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock, patch

from fixtures.library_import import ImportHarness

from backend.base.definitions import SpecialVersion
from backend.base.identification import (LocalMatchIssue, LocalMatchVolume,
                                         MatchReason, MatchState)
from backend.base.import_candidate import (CandidateDiagnostic, ClaimRole,
                                           DiagnosticCode, DiagnosticKind,
                                           DiscoveryScope, EvidenceSource,
                                           ExistingFileIdentity,
                                           FilenameObservation,
                                           FileObservation, FolderObservation,
                                           ImportCandidate, InspectionState,
                                           LocalAssociation, Provenance,
                                           ProviderIdentityClaim,
                                           ProviderReference, ResourceKind)
from backend.features.library_identification import (ProviderPublication,
                                                     ProviderReply,
                                                     identify_many,
                                                     local_matching_snapshot)
from backend.features.library_import import import_library
from backend.implementations.comicinfo import parse_comicinfo
from backend.implementations.identification import (MatchingSnapshot, identify,
                                                    numeric_label, title_key)

STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
DB = Provenance(EvidenceSource.DATABASE, 'test')
CI = Provenance(EvidenceSource.COMICINFO, 'ComicInfo.xml')
FN = Provenance(EvidenceSource.LEGACY_PARSER, 'filename')


def reference(pid='123', provider='comicvine', kind=ResourceKind.VOLUME):
    return ProviderReference(provider, kind, pid)


def volume(vid=1, **kwargs):
    return replace(LocalMatchVolume(vid, reference(str(vid)), 'Batman', 2020, 1, 'DC'), **kwargs)


def issue(iid=1, raw='1', calculated=1.0, vid=1, **kwargs):
    return replace(LocalMatchIssue(iid, vid, raw, calculated), **kwargs)


def candidate(number=1.0, stem='Batman 001 (2020)', **kwargs):
    return replace(ImportCandidate('test', DiscoveryScope('run', '/incoming'),
        FileObservation('/incoming/' + stem + '.cbz', STAMP, 10, 100, InspectionState.PRESENT),
        FolderObservation('/incoming', stem + '.cbz', True),
        FilenameObservation(stem + '.cbz', stem, '.cbz', 'Batman', 2020, None, None, number, False, FN)), **kwargs)


def comic(candidate_value, xml):
    document = parse_comicinfo(('<ComicInfo>' + xml + '</ComicInfo>').encode())
    return replace(candidate_value, comicinfo=replace(candidate_value.comicinfo,
        state=InspectionState.PRESENT, raw_bytes=document.raw_bytes, document=document, provenance=CI))


def claimed(c, *refs):
    return replace(c, claims=tuple(ProviderIdentityClaim(r, ClaimRole.EMBEDDED, CI) for r in refs))


class IdentificationTests(TestCase):
    def setUp(self):
        self.snapshot = MatchingSnapshot.build([volume()], [issue()])

    def test_exact_title_year_issue_automatic_and_reasons(self):
        result = identify(candidate(), self.snapshot)
        self.assertEqual(result.state, MatchState.AUTOMATIC)
        self.assertEqual(result.selected.local_issue_ids, (1,))
        self.assertEqual(result.selected.score, 140)
        self.assertEqual([c.reason for c in result.selected.contributions],
                         [MatchReason.TITLE, MatchReason.YEAR, MatchReason.ISSUE_NUMERIC])
        self.assertEqual(result.policy_id, 'kapowarr-identification/v1')

    def test_title_normalization_preserves_meaning(self):
        self.assertEqual(title_key(' BATMAN  Beyond '), 'batman beyond')
        self.assertNotEqual(title_key('Batman'), title_key('Batman Incorporated'))
        self.assertNotEqual(title_key("Batman '89"), title_key('Batman'))

    def test_similar_title_unresolved(self):
        result = identify(candidate(), MatchingSnapshot.build([volume(title='Batman Beyond')], [issue()]))
        self.assertEqual(result.state, MatchState.UNRESOLVED)

    def test_alias_generates_and_explains(self):
        result = identify(candidate(), MatchingSnapshot.build([volume(title='Other', aliases=('Batman',))], [issue()]))
        self.assertEqual(result.state, MatchState.AUTOMATIC)
        self.assertEqual(result.selected.contributions[0].reason, MatchReason.ALIAS)

    def test_year_mismatch_review_not_hard_rejection(self):
        result = identify(candidate(), MatchingSnapshot.build([volume(year=2021)], [issue()]))
        self.assertEqual(result.state, MatchState.REVIEW)
        self.assertIn(MatchReason.YEAR_CONFLICT, result.reasons)
        self.assertFalse(result.alternatives[0].rejections)

    def test_title_issue_without_year_insufficient(self):
        c = candidate()
        c = replace(c, filename=replace(c.filename, year=None))
        self.assertEqual(identify(c, self.snapshot).state, MatchState.REVIEW)

    def test_tie_never_uses_local_id_to_accept(self):
        snapshot = MatchingSnapshot.build([volume(2), volume()], [issue(), issue(2, vid=2)])
        result = identify(candidate(), snapshot)
        self.assertEqual(result.state, MatchState.REVIEW)
        self.assertIn(MatchReason.MULTIPLE, result.reasons)
        self.assertIsNone(result.selected)

    def test_reboot_year_disambiguates(self):
        snapshot = MatchingSnapshot.build([volume(2, year=1989), volume()], [issue(), issue(2, vid=2)])
        self.assertEqual(identify(candidate(), snapshot).selected.local_volume_id, 1)

    def test_exact_provider_ref_retains_selected_authority(self):
        snapshot = MatchingSnapshot.build([volume(authority=reference('abc', 'metron'), references=(reference('g', 'gcd'),))], [issue()])
        result = identify(claimed(candidate(), reference('g', 'gcd')), snapshot)
        self.assertEqual(result.state, MatchState.AUTOMATIC)
        self.assertEqual(result.selected.provider_identity.provider, 'metron')

    def test_namespace_is_not_bare_id(self):
        result = identify(claimed(candidate(), reference('1', 'metron')), self.snapshot)
        self.assertEqual(result.state, MatchState.REVIEW)
        self.assertIn(MatchReason.UNKNOWN_IDENTITY, result.reasons)

    def test_known_identity_rejects_high_scoring_wrong_volume(self):
        snapshot = MatchingSnapshot.build([volume(), volume(2, title='Other', year=1989)], [issue(), issue(2, vid=2)])
        result = identify(claimed(candidate(), reference('2')), snapshot)
        wrong = next(m for m in result.alternatives if m.local_volume_id == 1)
        self.assertIn(MatchReason.IDENTITY_CONFLICT, wrong.rejections)
        self.assertNotEqual(result.state, MatchState.AUTOMATIC)

    def test_conflicting_same_provider_claims(self):
        snapshot = MatchingSnapshot.build([volume(), volume(2)], [issue(), issue(2, vid=2)])
        result = identify(claimed(candidate(), reference('1'), reference('2')), snapshot)
        self.assertEqual(result.state, MatchState.CONFLICTED)

    def test_unknown_same_provider_volume_is_still_different_identity(self):
        result = identify(claimed(candidate(), reference('not-in-library')), self.snapshot)
        self.assertEqual(result.state, MatchState.CONFLICTED)
        self.assertIn(MatchReason.IDENTITY_CONFLICT, result.alternatives[0].rejections)

    def test_numeric_labels(self):
        for raw in ('1', '01', '1.0', '1.5', '0'):
            with self.subTest(raw=raw):
                value = float(raw)
                snapshot = MatchingSnapshot.build([volume()], [issue(raw=str(value), calculated=value)])
                result = identify(comic(candidate(), '<Number>' + raw + '</Number>'), snapshot)
                self.assertEqual(result.state, MatchState.AUTOMATIC)

    def test_suffix_exact_raw_not_numeric(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(raw='1A', calculated=1.01)])
        result = identify(comic(candidate(), '<Number>1A</Number>'), snapshot)
        self.assertEqual(result.state, MatchState.AUTOMATIC)
        self.assertEqual(result.selected.contributions[-1].reason, MatchReason.ISSUE_RAW)

    def test_opaque_unknown_never_zero(self):
        for label in ('[nn]', 'Annual', '1A', ''):
            with self.subTest(label=label):
                result = identify(comic(candidate(), '<Number>' + label + '</Number>'), self.snapshot)
                self.assertEqual(result.state, MatchState.REVIEW)
                self.assertIn(MatchReason.NUMBER_UNAVAILABLE, result.reasons)
                self.assertIsNone(numeric_label(label))

    def test_unnumbered_marker_is_not_exact_identity(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(raw='[nn]', calculated=.1414)])
        self.assertEqual(identify(comic(candidate(), '<Number>[nn]</Number>'), snapshot).state, MatchState.REVIEW)

    def test_numeric_collision_requires_review(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(raw='1A', calculated=1.01), issue(2, '1.01', 1.01)])
        result = identify(candidate(1.01, 'Batman 1.01 (2020)'), snapshot)
        self.assertIn(MatchReason.ISSUE_AMBIGUOUS, result.reasons)

    def test_exact_raw_resolves_numeric_collision(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(raw='1A', calculated=1.01), issue(2, '1.01', 1.01)])
        result = identify(comic(candidate(), '<Number>1.01</Number>'), snapshot)
        self.assertEqual(result.selected.local_issue_ids, (2,))

    def test_duplicate_raw_is_ambiguous(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(), issue(2)])
        result = identify(comic(candidate(), '<Number>1</Number>'), snapshot)
        self.assertIn(MatchReason.ISSUE_AMBIGUOUS, result.reasons)

    def test_filename_suffix_projection_not_accepted(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(raw='1.01', calculated=1.01)])
        result = identify(candidate(1.01, 'Batman 1A (2020)'), snapshot)
        self.assertIn(MatchReason.NUMBER_UNAVAILABLE, result.reasons)

    def test_suffix_projection_cannot_borrow_another_decimal_token(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(raw='1.01', calculated=1.01)])
        result = identify(candidate(1.01, 'Batman 1A (1.01) (2020)'), snapshot)
        self.assertIn(MatchReason.NUMBER_UNAVAILABLE, result.reasons)

    def test_range_inclusive_multi_issue(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(), issue(2, '2', 2), issue(3, '1.5', 1.5)])
        result = identify(candidate((1., 2.), 'Batman 001-002 (2020)'), snapshot)
        self.assertEqual(result.selected.local_issue_ids, (1, 2, 3))

    def test_range_missing_endpoint_review(self):
        self.assertEqual(identify(candidate((1., 3.), 'Batman 1-3 (2020)'), self.snapshot).state, MatchState.REVIEW)

    def test_range_unsafe_member_not_silently_omitted(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(), issue(2, '2', 2), issue(3, '1A', 1.01)])
        self.assertIn(MatchReason.NUMBER_UNAVAILABLE,
                      identify(candidate((1., 2.), 'Batman 1-2 (2020)'), snapshot).reasons)

    def test_exact_issue_reference(self):
        ref = reference('i', 'metron', ResourceKind.ISSUE)
        snapshot = MatchingSnapshot.build([volume()], [issue(references=(ref,))])
        result = identify(claimed(candidate(), ref), snapshot)
        self.assertEqual(result.selected.local_issue_ids, (1,))

    def test_existing_multi_issue_short_circuit(self):
        snapshot = MatchingSnapshot.build([volume()], [issue(), issue(2, '2', 2)])
        existing = ExistingFileIdentity(3, tuple(LocalAssociation(1, i, False, reference('1'), DB) for i in (1, 2)))
        result = identify(candidate(None, 'Wrong 999', existing=existing), snapshot)
        self.assertEqual(result.state, MatchState.AUTOMATIC)
        self.assertEqual(result.selected.local_issue_ids, (1, 2))

    def test_forced_not_score_bonus(self):
        c = candidate(existing=ExistingFileIdentity(3, (LocalAssociation(1, 1, True, reference('1'), DB),)))
        result = identify(c, self.snapshot)
        self.assertEqual(result.selected.contributions[0].reason, MatchReason.FORCED)
        self.assertEqual(result.selected.score, 0)

    def test_existing_identity_retained_on_comicinfo_conflict(self):
        c = candidate(existing=ExistingFileIdentity(3, (LocalAssociation(1, 1, False, reference('1'), DB),)))
        result = identify(claimed(c, reference('unknown', 'metron')), self.snapshot)
        self.assertEqual(result.state, MatchState.REVIEW)
        self.assertEqual(result.selected.local_volume_id, 1)

    def test_wrong_existing_parent_blocked(self):
        snapshot = MatchingSnapshot.build([volume(), volume(2)], [issue(2, vid=2)])
        c = candidate(existing=ExistingFileIdentity(3, (LocalAssociation(1, 2, False, reference('1'), DB),)))
        self.assertEqual(identify(c, snapshot).state, MatchState.BLOCKED)

    def test_folder_ownership_corroborates(self):
        c = candidate()
        c = replace(c, filename=replace(c.filename, year=None), folder=replace(c.folder, local_volume_ids=(1,), provenance=DB))
        self.assertEqual(identify(c, self.snapshot).state, MatchState.AUTOMATIC)

    def test_vai_volume_number_coverage(self):
        c = candidate(None, 'Batman Volume 1 (2020)')
        c = replace(c, filename=replace(c.filename, volume_number=1))
        snapshot = MatchingSnapshot.build([volume(special_version=SpecialVersion.VOLUME_AS_ISSUE)], [issue()])
        self.assertEqual(identify(c, snapshot).selected.local_issue_ids, (1,))

    def test_special_sole_issue_only(self):
        c = candidate(None, 'Batman HC (2020)')
        c = replace(c, filename=replace(c.filename, special_version='hard-cover'))
        for count in (1, 2):
            snapshot = MatchingSnapshot.build([volume(special_version=SpecialVersion.HARD_COVER)], [issue(i) for i in range(1, count + 1)])
            self.assertEqual(identify(c, snapshot).state, MatchState.AUTOMATIC if count == 1 else MatchState.REVIEW)

    def test_comicinfo_format_not_classification(self):
        c = comic(candidate(), '<Format>Hardcover</Format>')
        self.assertIn(MatchReason.SPECIAL_CONFLICT, identify(c, self.snapshot).reasons)

    def test_fatal_is_blocked_and_preserved(self):
        d = CandidateDiagnostic(DiagnosticKind.FATAL, DiagnosticCode.STAT_FAILED, FN)
        c = candidate(diagnostics=(d,))
        result = identify(c, self.snapshot)
        self.assertEqual(result.state, MatchState.BLOCKED)
        self.assertIs(result.candidate, c)

    def test_partial_date_only_year_evidence(self):
        c = comic(candidate(), '<Year>2020</Year><Month>12</Month><Number>1</Number>')
        result = identify(c, self.snapshot)
        self.assertEqual(result.state, MatchState.AUTOMATIC)
        self.assertIsNone(result.candidate.comicinfo.document.date.complete_date)

    def test_determinism_order_independent(self):
        vs = [volume(), volume(2)]
        ins = [issue(), issue(2, vid=2)]
        a = MatchingSnapshot.build(vs, ins)
        b = MatchingSnapshot.build(reversed(vs), reversed(ins))
        self.assertEqual(identify(candidate(), a), identify(candidate(), b))

    def test_snapshot_immutable(self):
        with self.assertRaises(TypeError):
            self.snapshot.volumes[3] = volume(3)

    def test_duplicate_snapshot_id_rejected(self):
        with self.assertRaises(ValueError):
            MatchingSnapshot.build([volume(), volume()], [])

    def test_provider_optional_and_never_for_resolved(self):
        adapter = Mock()
        bulk = identify_many([candidate()] * 1000, self.snapshot, acquisition=adapter, provider='metron', max_operations=1)
        adapter.acquire.assert_not_called()
        self.assertEqual(dict(bulk.counts)['automatic_match'], 1000)

    def test_provider_cache_budget_attribution_no_auto_add(self):
        adapter = Mock()
        adapter.acquire.return_value = ProviderReply((ProviderPublication(reference('a', 'metron'), 'Batman', 2020),))
        empty = MatchingSnapshot.build([], [])
        bulk = identify_many([candidate()] * 100, empty, acquisition=adapter, provider='metron', max_operations=1)
        self.assertEqual(adapter.acquire.call_count, 1)
        self.assertTrue(all(r.state == MatchState.REVIEW for r in bulk.results))
        self.assertIsNone(bulk.results[0].alternatives[0].local_volume_id)
        self.assertEqual(bulk.results[0].alternatives[0].title, 'Batman')

    def test_provider_budget_exhaustion_not_no_match(self):
        adapter = Mock()
        result = identify_many([candidate()], MatchingSnapshot.build([], []), acquisition=adapter, provider='metron').results[0]
        self.assertEqual(result.state, MatchState.BLOCKED)
        adapter.acquire.assert_not_called()

    def test_provider_failure_not_no_match(self):
        adapter = Mock()
        adapter.acquire.return_value = ProviderReply(unavailable=True, failure='rate_limit')
        result = identify_many([candidate()], MatchingSnapshot.build([], []), acquisition=adapter, provider='metron', max_operations=1).results[0]
        self.assertEqual(result.state, MatchState.BLOCKED)

    def test_provider_direct_id_not_search(self):
        adapter = Mock()
        adapter.acquire.return_value = ProviderReply()
        identify_many([claimed(candidate(), reference('a', 'metron'))], MatchingSnapshot.build([], []), acquisition=adapter, provider='metron', max_operations=1)
        request = adapter.acquire.call_args.args[0]
        self.assertEqual(request.reference, reference('a', 'metron'))
        self.assertIsNone(request.query)

    def test_bulk_broken_candidate_isolated(self):
        bad = candidate(file=FileObservation('missing', STAMP, None, None, InspectionState.FAILED))
        results = identify_many([bad, candidate()], self.snapshot).results
        self.assertEqual([r.state for r in results], [MatchState.BLOCKED, MatchState.AUTOMATIC])

    def test_pure_matching_has_no_io(self):
        with patch('builtins.open', side_effect=AssertionError('IO')), patch('os.rename', side_effect=AssertionError('mutation')), patch('os.remove', side_effect=AssertionError('mutation')), patch('socket.socket', side_effect=AssertionError('network')):
            self.assertEqual(identify(candidate(), self.snapshot).state, MatchState.AUTOMATIC)

    def test_close_margin_not_overcome_by_publisher(self):
        snapshot = MatchingSnapshot.build([volume(), volume(2, publisher='Other')], [issue(), issue(2, vid=2)])
        result = identify(comic(candidate(), '<Publisher>DC</Publisher>'), snapshot)
        self.assertEqual(result.state, MatchState.REVIEW)
        self.assertIn(MatchReason.MULTIPLE, result.reasons)

    def test_invalid_comicinfo_year_does_not_fall_back_silently(self):
        result = identify(comic(candidate(), '<Year>banana</Year>'), self.snapshot)
        self.assertEqual(result.state, MatchState.REVIEW)
        self.assertIn(MatchReason.EVIDENCE_CONFLICT, result.reasons)

    def test_duplicate_number_does_not_fall_back_to_filename(self):
        result = identify(comic(candidate(), '<Number>1</Number><Number>2</Number>'), self.snapshot)
        self.assertIn(MatchReason.NUMBER_UNAVAILABLE, result.reasons)

    def test_issue_parent_claim_conflict_is_hard_gate(self):
        ref = reference('issue', kind=ResourceKind.ISSUE)
        snapshot = MatchingSnapshot.build([volume(), volume(2)], [issue(references=(ref,)), issue(2, vid=2)])
        claim = ProviderIdentityClaim(ref, ClaimRole.EMBEDDED, CI, reference('2'))
        result = identify(candidate(claims=(claim,)), snapshot)
        self.assertEqual(result.state, MatchState.CONFLICTED)
        self.assertTrue(all(m.rejections for m in result.alternatives))

    def test_existing_selected_issue_must_still_agree(self):
        association = LocalAssociation(1, 1, False, reference('1'), DB,
                                       selected_issue=reference('wrong', kind=ResourceKind.ISSUE))
        result = identify(candidate(existing=ExistingFileIdentity(3, (association,))), self.snapshot)
        self.assertEqual(result.state, MatchState.BLOCKED)

    def test_same_cross_reference_multiple_local_owners_review(self):
        shared = reference('g', 'gcd')
        snapshot = MatchingSnapshot.build([volume(references=(shared,)), volume(2, references=(shared,))], [issue(), issue(2, vid=2)])
        result = identify(claimed(candidate(), shared), snapshot)
        self.assertEqual(result.state, MatchState.REVIEW)

    def test_reference_order_does_not_change_snapshot(self):
        refs = (reference('g', 'gcd'), reference('m', 'metron'))
        a = MatchingSnapshot.build([volume(references=refs)], [issue()])
        b = MatchingSnapshot.build([volume(references=tuple(reversed(refs)))], [issue()])
        self.assertEqual(a.snapshot_id, b.snapshot_id)

    def test_cross_provider_search_not_implicit(self):
        adapter = Mock()
        result = identify_many([claimed(candidate(), reference('g', 'gcd'))], MatchingSnapshot.build([], []),
                               acquisition=adapter, provider='metron', max_operations=1).results[0]
        self.assertEqual(result.state, MatchState.REVIEW)
        adapter.acquire.assert_not_called()

    def test_wrong_direct_result_rejected(self):
        adapter = Mock()
        adapter.acquire.return_value = ProviderReply((ProviderPublication(reference('wrong', 'metron'), 'Batman', 2020),))
        with self.assertRaises(ValueError):
            identify_many([claimed(candidate(), reference('expected', 'metron'))], MatchingSnapshot.build([], []),
                          acquisition=adapter, provider='metron', max_operations=1)

    def test_provider_failure_category_preserved(self):
        adapter = Mock()
        adapter.acquire.return_value = ProviderReply(unavailable=True, failure='authentication')
        result = identify_many([candidate()], MatchingSnapshot.build([], []), acquisition=adapter,
                               provider='metron', max_operations=1).results[0]
        self.assertEqual(result.acquisition_failure, 'authentication')

    def test_no_year_precision_is_invented(self):
        for xml in ('<Year>2020</Year>', '<Year>2020</Year><Month>12</Month>'):
            c = comic(candidate(), xml)
            result = identify(c, self.snapshot)
            self.assertIsNone(result.candidate.comicinfo.document.date.complete_date)

    def test_repeated_matching_does_not_mutate_input(self):
        c = candidate()
        before = repr(c)
        a = identify(c, self.snapshot)
        b = identify(c, self.snapshot)
        self.assertEqual(a, b)
        self.assertEqual(repr(c), before)


class MatchingDatabaseTests(ImportHarness, TestCase):
    def test_snapshot_query_count_independent_of_candidates(self):
        path = self.comic_file()
        import_library([{'id': 2127, 'filepath': path}])
        with patch('backend.internals.identification.get_db', side_effect=self.db.cursor):
            statements = []
            self.db.set_trace_callback(statements.append)
            before = self.db.total_changes
            snapshot = local_matching_snapshot(('comicvine', 'metron'))
            self.assertEqual(snapshot.volumes[1].authority, reference('2127'))
            self.assertEqual(len(snapshot.issues), 2)
            for size in (1, 100, 1000):
                identify_many([candidate()] * size, snapshot)
            self.db.set_trace_callback(None)
            self.assertEqual(sum(s.lstrip().upper().startswith('SELECT') for s in statements), 4)
            self.assertEqual(self.db.total_changes, before)

"""Offline policy/legacy characterization, adversarial compatibility and ranking."""

import json
import random
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from time import perf_counter
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fixtures.library_import import ImportHarness

from backend.base.definitions import SpecialVersion
from backend.base.identification import LocalMatchIssue, LocalMatchVolume
from backend.base.import_candidate import ProviderReference, ResourceKind
from backend.base.release_candidate import (AcquisitionMechanism, CoverageKind,
                                            LocatorKind, ObservationOrigin,
                                            PackKind, ReleaseCoverage,
                                            ReleaseIdentity,
                                            ReleaseObservation,
                                            ReleaseVolumeKind, ReleaseYearKind,
                                            SourceKind)
from backend.base.release_evaluation import (Compatibility as State,
                                             CoverageBand as Band, PackPolicy,
                                             Rule, ScoringPolicy,
                                             SourcePriority, TargetKind,
                                             WantedIssue, WantedTarget)
from backend.features.library_identification import local_matching_snapshot
from backend.features.library_import import import_library
from backend.features.search_full import SearchCoordinator
from backend.implementations.identification import MatchingSnapshot
from backend.implementations.matching import check_search_result_match
from backend.implementations.release_candidates import adapt_ddl_result
from backend.implementations.release_scoring import (
    build_wanted_target, evaluate_release, evaluate_releases,
    policy_from_format_preference, preview_evaluation, rank_evaluations)
from tests.Tbackend.features.release_candidates import ddl


def reference(pid='series', kind=ResourceKind.VOLUME, provider='metron'):
    return ProviderReference(provider, kind, pid)


def target(label='5', *, kind=TargetKind.ISSUES, special=SpecialVersion.NORMAL,
           catalog=None, ids=None, **kwargs):
    issue = WantedIssue(5, label, (reference('issue-5', ResourceKind.ISSUE),), 2016, False)
    publication = LocalMatchVolume(1, reference(), 'Batman', 2016, 2, 'DC', special)
    return WantedTarget(publication, catalog or (issue,), ids or (5,), kind, **kwargs)


def candidate(title='Batman #5 (2016).cbz', **kwargs):
    return replace(adapt_ddl_result(ddl(title)), **kwargs)


def structured(c=None, **kwargs):
    # Deliberately no extra legacy/title interpretations in structured fixtures.
    return replace(c or candidate(), observations=(ReleaseObservation(
        ObservationOrigin.STRUCTURED, 'fixture', **kwargs),), diagnostics=())


def fact_candidate(label='5', **kwargs):
    return structured(series='Batman', year=2016,
                      coverage=ReleaseCoverage(CoverageKind.SINGLE, (label,)), **kwargs)


class LegacyScoringCharacterization(TestCase):
    """The production matcher/ranker remains unchanged by the new engine."""

    def legacy(self, title, special=SpecialVersion.NORMAL, number=5.0):
        volume = SimpleNamespace(title='Batman', alt_title=None, year=2016,
                                 volume_number=2, special_version=special)
        issues = [SimpleNamespace(calculated_issue_number=float(i)) for i in range(1, 11)]
        result = ddl(title)
        with patch('backend.implementations.matching.blocklist_contains', return_value=False):
            match = check_search_result_match(result, volume, issues,
                       {float(i): 2016 for i in range(1, 11)}, number)
        coordinator = object.__new__(SearchCoordinator)
        coordinator.volume_data = volume
        return match, coordinator._rank_search_result({**result, **match}, 2016, number)

    def test_exact_wrong_and_range_gate(self):
        self.assertTrue(self.legacy('Batman #5 (2016)')[0]['match'])
        self.assertFalse(self.legacy('Batman #6 (2016)')[0]['match'])
        self.assertFalse(self.legacy('Batman #1-10 (2016)')[0]['match'])

    def test_year_wiggle_and_reboot(self):
        self.assertTrue(self.legacy('Batman #5 (2017)')[0]['match'])
        self.assertFalse(self.legacy('Batman #5 (2011)')[0]['match'])

    def test_special_and_implicit_tpb(self):
        self.assertTrue(self.legacy('Batman TPB (2016)', SpecialVersion.TPB)[0]['match'])
        self.assertTrue(self.legacy('Batman (2016)', SpecialVersion.HARD_COVER)[0]['match'])

    def test_vai_volume_as_issue(self):
        self.assertTrue(self.legacy('Batman Volume 5 (2016)', SpecialVersion.VOLUME_AS_ISSUE)[0]['match'])

    def test_extension_not_legacy_gate(self):
        self.assertTrue(self.legacy('Batman #5 (2016).exe')[0]['match'])

    def test_match_precedes_other_legacy_rank_axes(self):
        exact = self.legacy('Batman #5 (2016)')[1]
        wrong = self.legacy('Batman #6 (2016)')[1]
        self.assertLess(exact, wrong)


class ReleaseScoringTests(TestCase):
    def evaluate(self, c=None, t=None, p=None):
        return evaluate_release(t or target(), c or candidate(), p or ScoringPolicy())

    def assert_state(self, state, c=None, t=None, p=None):
        result = self.evaluate(c, t, p)
        self.assertEqual(result.state, state, result.components)
        if state != State.COMPATIBLE:
            self.assertIsNone(result.score)
            self.assertTrue(all(c.points == 0 for c in result.components))
        return result

    def test_exact_issue(self):
        result = self.assert_state(State.COMPATIBLE)
        self.assertEqual(result.band, Band.EXACT)
        self.assertEqual(result.score, 280)

    def test_wrong_issue(self):
        result = self.assert_state(State.REJECTED, candidate('Batman #6 (2016)'))
        self.assertIn(Rule.ISSUE_WRONG, result.rejections)

    def test_range_contains_below_exact(self):
        result = self.assert_state(State.COMPATIBLE, candidate('Batman #1-10 (2016)'))
        self.assertEqual(result.band, Band.CONTAINING)
        self.assertLess(result.score, self.evaluate().score)

    def test_range_excludes(self):
        self.assert_state(State.REJECTED, candidate('Batman #1-4 (2016)'))

    def test_decimal_range(self):
        self.assert_state(State.COMPATIBLE, candidate('Batman #1.5-3 (2016)'), target('2.5'))

    def test_invalid_ranges_review(self):
        for value in ('5-1', '1A-5A', '1,3-5'):
            with self.subTest(value=value):
                self.assert_state(State.REVIEW, candidate(f'Batman #{value} (2016)'))

    def test_direct_structured_descending_range(self):
        c = structured(series='Batman', coverage=ReleaseCoverage(CoverageKind.RANGE, ('5', '1')))
        self.assert_state(State.REVIEW, c)

    def test_noncontiguous_set(self):
        c = candidate('Batman #1,3,5 (2016)')
        self.assert_state(State.COMPATIBLE, c)
        self.assert_state(State.REJECTED, c, target('4'))

    def test_opaque_labels_exact(self):
        for label in ('1A', '[nn]', 'Annual', 'Special'):
            with self.subTest(label=label):
                self.assert_state(State.COMPATIBLE, fact_candidate(label), target(label))

    def test_no_fake_suffix_projection(self):
        self.assert_state(State.REJECTED, fact_candidate('1.01'), target('1A'))
        self.assert_state(State.REJECTED, fact_candidate('1A'), target('1.01'))

    def test_opaque_mismatch(self):
        self.assert_state(State.REJECTED, fact_candidate('Special'), target('Annual'))

    def test_opaque_range_abstains(self):
        self.assert_state(State.REVIEW, candidate('Batman #1-4 (2016)'), target('1A'))

    def test_numeric_equivalence_matrix(self):
        for left in ('1', '01', '1.0'):
            for right in ('1', '01', '1.0'):
                with self.subTest(left=left, right=right):
                    result = self.assert_state(State.COMPATIBLE, fact_candidate(left), target(right))
                    self.assertIn(Rule.ISSUE_RAW if left == right else Rule.ISSUE_NUMERIC,
                                  [c.rule for c in result.components])
        self.assert_state(State.COMPATIBLE, fact_candidate('1.5'), target('1.5'))

    def test_numeric_collision_review_but_unique_raw_wins(self):
        t = target('1', catalog=(WantedIssue(5, '1'), WantedIssue(6, '01')))
        self.assert_state(State.REVIEW, fact_candidate('1.0'), t)
        self.assert_state(State.COMPATIBLE, fact_candidate('1'), t)

    def test_duplicate_raw_label_review(self):
        t = target(catalog=(WantedIssue(5, '[nn]'), WantedIssue(6, '[nn]')))
        self.assert_state(State.REVIEW, fact_candidate('[nn]'), t)

    def test_range_numeric_collision(self):
        t = target(catalog=(WantedIssue(5, '1'), WantedIssue(6, '01')))
        self.assert_state(State.REVIEW, candidate('Batman #1-4 (2016)'), t)

    def test_multi_target_exact_set(self):
        t = target(catalog=(WantedIssue(5, '1'), WantedIssue(6, '3')), ids=(5, 6))
        result = self.assert_state(State.COMPATIBLE, candidate('Batman #1,3 (2016)'), t)
        self.assertEqual(result.band, Band.EXACT)
        self.assert_state(State.REJECTED, candidate('Batman #1 (2016)'), t)

    def test_series_pack_default_review_and_explicit_allow(self):
        c = structured(series='Batman', year=2016, pack=PackKind.SERIES,
                       coverage=ReleaseCoverage(CoverageKind.PACK))
        self.assert_state(State.REVIEW, c)
        result = self.assert_state(State.COMPATIBLE, c, p=ScoringPolicy(packs=PackPolicy.ALLOW))
        self.assertEqual(result.band, Band.CLAIMED_PACK)
        self.assertLess(result.score, self.evaluate().score)

    def test_volume_pack(self):
        self.assert_state(State.COMPATIBLE, structured(series='Batman', pack=PackKind.VOLUME,
                          coverage=ReleaseCoverage(CoverageKind.PACK)),
                          p=ScoringPolicy(packs=PackPolicy.ALLOW))

    def test_legacy_pack_title_disagreement_not_hidden(self):
        for title in ('Batman Complete Series (2016)', 'Batman Volume Pack (2016)'):
            self.assert_state(State.REVIEW, candidate(title), p=ScoringPolicy(packs=PackPolicy.ALLOW))

    def test_generic_pack_not_proof_of_membership(self):
        self.assert_state(State.REVIEW, candidate('Batman Pack (2016)'),
                          p=ScoringPolicy(packs=PackPolicy.ALLOW))

    def test_forbid_surplus(self):
        p = ScoringPolicy(packs=PackPolicy.FORBID)
        for title in ('Batman #1-10 (2016)', 'Batman Complete Series (2016)'):
            self.assert_state(State.REJECTED, candidate(title), p=p)
        self.assert_state(State.COMPATIBLE, p=p)

    def test_whole_volume_no_fake_range(self):
        t = target(kind=TargetKind.WHOLE_VOLUME, catalog=(WantedIssue(5, '1'), WantedIssue(6, '3')), ids=(5, 6))
        result = self.assert_state(State.COMPATIBLE, candidate('Batman #1,3 (2016)'), t)
        self.assertNotIn(Rule.PACK_PENALTY, [c.rule for c in result.components])

    def test_same_title_reboots_hard_gate_with_known_issue_year(self):
        for year in (1940, 2011, 2017):
            self.assert_state(State.REJECTED, candidate(f'Batman #5 ({year})'))

    def test_typed_series_year_conflict(self):
        c = fact_candidate(year_kind=ReleaseYearKind.SERIES)
        c = replace(c, observations=(replace(c.observations[0], year=2011),))
        self.assert_state(State.REJECTED, c)

    def test_issue_year_not_series_start(self):
        t = replace(target(), catalog=(replace(target().catalog[0], year=2024),))
        c = replace(fact_candidate(year_kind=ReleaseYearKind.ISSUE),
                    observations=(replace(fact_candidate().observations[0], year=2024, year_kind=ReleaseYearKind.ISSUE),))
        self.assert_state(State.COMPATIBLE, c, t)

    def test_untyped_year_mismatch_without_issue_year_reviews(self):
        t = replace(target(), catalog=(replace(target().catalog[0], year=None),))
        self.assert_state(State.REVIEW, candidate('Batman #5 (2024)'), t)

    def test_missing_optional_year_neutral(self):
        c = fact_candidate()
        c = replace(c, observations=(replace(c.observations[0], year=None),))
        result = self.assert_state(State.COMPATIBLE, c)
        self.assertEqual(next(c.points for c in result.components if c.rule == Rule.YEAR_UNKNOWN), 0)

    def test_similar_title_not_fuzzy(self):
        self.assert_state(State.REJECTED, candidate('Batman Incorporated #5 (2016)'))

    def test_alias(self):
        t = target()
        t = replace(t, publication=replace(t.publication, aliases=('The Batman',)))
        result = self.assert_state(State.COMPATIBLE, candidate('The Batman #5 (2016)'), t)
        self.assertIn(Rule.SERIES_ALIAS, [c.rule for c in result.components])

    def test_unicode_normalization_reuses_identification(self):
        t = target()
        t = replace(t, publication=replace(t.publication, title='Café'))
        c = structured(series='CAFE\u0301', coverage=ReleaseCoverage(CoverageKind.SINGLE, ('5',)))
        self.assert_state(State.COMPATIBLE, c, t)

    def test_missing_required_series_or_coverage(self):
        self.assert_state(State.UNDETERMINED, structured(series='Batman'))
        self.assert_state(State.UNDETERMINED, structured(coverage=ReleaseCoverage(CoverageKind.SINGLE, ('5',))))

    def test_identity_exact(self):
        c = fact_candidate(identities=(ReleaseIdentity(reference()),))
        result = self.assert_state(State.COMPATIBLE, c)
        self.assertEqual(sum(c.points for c in result.components if c.axis == 'series'), 120)

    def test_identity_conflict_cannot_be_rescued(self):
        c = fact_candidate(identities=(ReleaseIdentity(reference('wrong')),), extension='.cbz', language='en', release_group='fav')
        p = ScoringPolicy(archive_order=('.cbz',), preferred_groups=('fav',),
                          source_priorities=(SourcePriority(c.source.kind, c.source.key, 999999),))
        result = self.assert_state(State.REJECTED, c, target(language='en'), p)
        self.assertIn(Rule.IDENTITY_CONFLICT, result.rejections)
        self.assertEqual(result.source_priority, 0)

    def test_cross_provider_not_bare_id_equality(self):
        c = fact_candidate(identities=(ReleaseIdentity(reference(provider='comicvine')),))
        self.assert_state(State.REVIEW, c)

    def test_known_cross_reference_does_not_switch_authority(self):
        t = target()
        ref = reference('cv', provider='comicvine')
        t = replace(t, publication=replace(t.publication, references=(ref,)))
        result = self.assert_state(State.COMPATIBLE, fact_candidate(identities=(ReleaseIdentity(ref),)), t)
        self.assertEqual(result.target.publication.authority.provider, 'metron')

    def test_issue_identity_and_parent(self):
        claim = ReleaseIdentity(reference('issue-5', ResourceKind.ISSUE), reference())
        self.assert_state(State.COMPATIBLE, fact_candidate(identities=(claim,)))
        self.assert_state(State.REJECTED, fact_candidate(identities=(replace(claim, parent=reference('other')),)))

    def test_unknown_issue_identity_same_provider_rejects(self):
        self.assert_state(State.REJECTED, fact_candidate(identities=(ReleaseIdentity(reference('other', ResourceKind.ISSUE)),)))

    def test_issue_identity_conflicting_label_review(self):
        c = fact_candidate('6', identities=(ReleaseIdentity(reference('issue-5', ResourceKind.ISSUE)),))
        self.assert_state(State.REVIEW, c)

    def test_identity_can_supply_absent_bibliography(self):
        c = structured(identities=(ReleaseIdentity(reference('issue-5', ResourceKind.ISSUE)),))
        self.assert_state(State.COMPATIBLE, c)

    def test_canonical_volume_mismatch_vs_raw_ambiguity(self):
        self.assert_state(State.REJECTED, fact_candidate(volume='3', volume_kind=ReleaseVolumeKind.CANONICAL))
        self.assert_state(State.REVIEW, fact_candidate(volume='3'))
        self.assert_state(State.COMPATIBLE, fact_candidate(volume='02', volume_kind=ReleaseVolumeKind.CANONICAL))

    def test_vai_volume_coverage(self):
        self.assert_state(State.COMPATIBLE, candidate('Batman Volume 5 (2016)'), target(special=SpecialVersion.VOLUME_AS_ISSUE))

    def test_vai_volume_and_issue_disagree(self):
        self.assert_state(State.REVIEW, candidate('Batman Volume 4 #5 (2016)'), target(special=SpecialVersion.VOLUME_AS_ISSUE))

    def test_collected_targets(self):
        for sv, marker in ((SpecialVersion.TPB, 'TPB'), (SpecialVersion.HARD_COVER, 'HC'),
                           (SpecialVersion.OMNIBUS, 'Omnibus'), (SpecialVersion.ONE_SHOT, 'One-Shot')):
            with self.subTest(sv=sv):
                self.assert_state(State.COMPATIBLE, candidate(f'Batman {marker} (2016)'),
                                  target('1', special=sv, kind=TargetKind.COLLECTION))

    def test_tpb_not_an_ordinary_issue(self):
        self.assert_state(State.REJECTED, candidate('Batman #1 (2016)'),
                          target('1', special=SpecialVersion.TPB, kind=TargetKind.COLLECTION))
        self.assert_state(State.REJECTED, candidate('Batman Omnibus (2016)'))

    def test_sole_special_numbered_compatibility(self):
        for sv in (SpecialVersion.HARD_COVER, SpecialVersion.ONE_SHOT, SpecialVersion.OMNIBUS):
            self.assert_state(State.COMPATIBLE, candidate('Batman #1 (2016)'), target('1', special=sv))

    def test_physical_publication_axes_independent(self):
        c = structured(series='Batman', physical_format='Hardcover', publication_kind='Omnibus',
                       coverage=ReleaseCoverage(CoverageKind.COLLECTION))
        t = target('1', special=SpecialVersion.OMNIBUS, kind=TargetKind.COLLECTION, physical_format='HC')
        self.assert_state(State.COMPATIBLE, c, t)
        self.assert_state(State.REJECTED, c, replace(t, physical_format='TPB'))

    def test_graphic_novel_not_new_classification(self):
        self.assert_state(State.REVIEW, fact_candidate(publication_kind='Graphic Novel'))

    def test_language_matrix(self):
        self.assert_state(State.COMPATIBLE, fact_candidate(language='en'), target(language='en'))
        self.assert_state(State.REJECTED, fact_candidate(language='fr'), target(language='en'))
        self.assert_state(State.COMPATIBLE, fact_candidate(), target(language='en'))
        self.assert_state(State.COMPATIBLE, fact_candidate(language='fr'))

    def test_archive_support_not_automatic_quality(self):
        scores = []
        for extension in ('.cbz', '.CBR', '.pdf', None):
            scores.append(self.assert_state(State.COMPATIBLE, fact_candidate(extension=extension)).score)
        self.assertEqual(len(set(scores)), 1)
        self.assert_state(State.REJECTED, fact_candidate(extension='.exe'))

    def test_format_preferences_opt_in(self):
        p = policy_from_format_preference(('cbz', 'cbr', 'folder'))
        self.assertEqual(p.archive_order, ('.cbz', '.cbr'))
        self.assertGreater(self.evaluate(fact_candidate(extension='.cbz'), p=p).score,
                           self.evaluate(fact_candidate(extension='.cbr'), p=p).score)

    def test_size_not_quality(self):
        self.assert_state(State.REJECTED, candidate(size_bytes=0))
        self.assertEqual(self.evaluate(candidate(size_bytes=1)).score, self.evaluate(candidate(size_bytes=10**12)).score)

    def test_unknown_size_date_no_penalty(self):
        self.assertEqual(self.evaluate(candidate(size_bytes=None, published_at=None)).score, self.evaluate().score)

    def test_publication_recency_not_bibliography(self):
        self.assertEqual(self.evaluate(candidate(published_at=datetime(2030, 1, 1, tzinfo=timezone.utc))).score, self.evaluate().score)

    def test_owned_state_supplied_not_queried(self):
        t = replace(target(), catalog=(replace(target().catalog[0], owned=True),))
        self.assert_state(State.REJECTED, t=t)
        self.assert_state(State.COMPATIBLE, t=t, p=ScoringPolicy(reject_owned=False))

    def test_acquisition_unavailable(self):
        c = candidate()
        c = replace(c, acquisition=replace(c.acquisition, kind=LocatorKind.UNAVAILABLE, key=None))
        self.assert_state(State.REJECTED, c)

    def test_neutral_download_protocols(self):
        c = candidate()
        for mechanism in (AcquisitionMechanism.NZB, AcquisitionMechanism.TORRENT):
            self.assertEqual(self.evaluate(replace(c, acquisition=replace(c.acquisition, mechanism=mechanism))).score,
                             self.evaluate(c).score)

    def test_conflicting_observations_review(self):
        c = fact_candidate()
        other = replace(c.observations[0], year=2017, locator='other')
        self.assert_state(State.REVIEW, replace(c, observations=(*c.observations, other)))

    def test_no_double_counting(self):
        c = fact_candidate()
        repeated = replace(c, observations=tuple(replace(c.observations[0], origin=origin) for origin in ObservationOrigin))
        self.assertEqual(self.evaluate(c).score, self.evaluate(repeated).score)

    def test_observation_order_component_determinism(self):
        c = candidate()
        self.assertEqual(self.evaluate(c).components, self.evaluate(replace(c, observations=tuple(reversed(c.observations)))).components)

    def test_source_priority_never_rescues_wrong_issue(self):
        good, bad = candidate(), candidate('Batman #6 (2016)')
        bad = replace(bad, source=replace(bad.source, key='fav'))
        p = ScoringPolicy(source_priorities=(SourcePriority(bad.source.kind, 'fav', 100000),))
        ordered = rank_evaluations(evaluate_releases(target(), (bad, good), p))
        self.assertEqual(ordered[0].candidate, good)

    def test_source_priority_after_score_and_coverage(self):
        exact = candidate()
        pack = candidate('Batman #1-10 (2016)')
        pack = replace(pack, source=replace(pack.source, key='fav'))
        p = ScoringPolicy(source_priorities=(SourcePriority(pack.source.kind, 'fav', 100000),))
        self.assertEqual(rank_evaluations(evaluate_releases(target(), (pack, exact), p))[0].candidate, exact)

    def test_source_tie_preference(self):
        a = candidate()
        b = replace(a, source=replace(a.source, key='preferred'))
        p = ScoringPolicy(source_priorities=(SourcePriority(b.source.kind, b.source.key, 5),))
        self.assertEqual(rank_evaluations(evaluate_releases(target(), (a, b), p))[0].candidate, b)

    def test_shuffle_stable_ties(self):
        candidates = tuple(replace(candidate(), result_id=str(i)) for i in range(12))
        values = list(evaluate_releases(target(), candidates))
        expected = rank_evaluations(values)
        for seed in range(10):
            random.Random(seed).shuffle(values)
            self.assertEqual(rank_evaluations(values), expected)

    def test_cannot_rank_across_targets_or_policies(self):
        with self.assertRaises(ValueError):
            rank_evaluations((self.evaluate(), self.evaluate(t=target('6'))))
        with self.assertRaises(ValueError):
            rank_evaluations((self.evaluate(), self.evaluate(p=ScoringPolicy(packs=PackPolicy.ALLOW))))

    def test_preview_has_components_no_secrets_no_winner(self):
        value = preview_evaluation(self.evaluate())
        self.assertEqual(json.loads(json.dumps(value)), value)
        self.assertNotIn(candidate().acquisition.key, json.dumps(value))
        self.assertNotIn('winner', value)
        self.assertTrue(value['components'])

    def test_immutability(self):
        result = self.evaluate()
        with self.assertRaises(FrozenInstanceError):
            result.score = 999
        with self.assertRaises(ValueError):
            replace(target(), catalog=[])
        with self.assertRaises(ValueError):
            ScoringPolicy(archive_order=['.cbz'])

    def test_invalid_target_and_policy(self):
        with self.assertRaises(ValueError):
            replace(target(), issue_ids=(999,))
        with self.assertRaises(ValueError):
            ScoringPolicy(policy_id='future')
        with self.assertRaises(ValueError):
            replace(target(), issue_ids=())

    def test_snapshot_builder(self):
        t = target()
        snapshot = MatchingSnapshot.build((t.publication,), (LocalMatchIssue(5, 1, '1A', 1.01),))
        built = build_wanted_target(snapshot, 1, (5,), issue_years={5: 2016}, owned_issue_ids=())
        self.assertEqual(built.catalog[0].raw_number, '1A')
        self.assertFalse(built.catalog[0].owned)
        self.assert_state(State.REJECTED, fact_candidate('1.01'), built)

    def test_bulk_pure_and_one_context(self):
        import backend.implementations.release_scoring as scoring
        t, c = target(), candidate()
        with patch.object(scoring, '_TargetContext', wraps=scoring._TargetContext) as contexts, \
             patch('socket.socket', side_effect=AssertionError('network')), \
             patch('sqlite3.connect', side_effect=AssertionError('DB')), \
             patch('builtins.open', side_effect=AssertionError('file IO')):
            for count in (1, 100, 1000, 10000):
                before = contexts.call_count
                started = perf_counter()
                results = evaluate_releases(t, (c for _ in range(count)))
                self.assertEqual(len(results), count)
                self.assertEqual(contexts.call_count, before + 1)
                self.assertTrue(all(r.state == State.COMPATIBLE for r in results))
                if count == 10000:
                    print(f'10,000 release evaluations: {perf_counter() - started:.3f}s; one context, zero IO')

    def test_year_roles_do_not_conflict_or_double_count(self):
        t = replace(target(), catalog=(replace(target().catalog[0], year=2024),))
        c = fact_candidate(year_kind=ReleaseYearKind.SERIES)
        issue_date = replace(c.observations[0], year=2024, year_kind=ReleaseYearKind.ISSUE,
                             locator='issue-date')
        result = self.assert_state(State.COMPATIBLE, replace(c, observations=(*c.observations, issue_date)), t)
        self.assertEqual(sum(v.points for v in result.components if v.axis == 'year'), 40)

    def test_identity_does_not_hide_unavailable_range(self):
        c = structured(series='Batman', identities=(ReleaseIdentity(reference('issue-5', ResourceKind.ISSUE)),),
                       coverage=ReleaseCoverage(CoverageKind.RANGE, ('1A', '9A')))
        self.assert_state(State.REVIEW, c)

    def test_identity_does_not_erase_surplus_range(self):
        c = structured(series='Batman', identities=(ReleaseIdentity(reference('issue-5', ResourceKind.ISSUE)),),
                       coverage=ReleaseCoverage(CoverageKind.RANGE, ('1', '10')))
        result = self.assert_state(State.COMPATIBLE, c)
        self.assertEqual(result.band, Band.CONTAINING)
        self.assertIn(Rule.PACK_PENALTY, tuple(v.rule for v in result.components))
        pack = replace(c, observations=(replace(c.observations[0],
                       coverage=ReleaseCoverage(CoverageKind.PACK), pack=PackKind.SERIES),))
        self.assertEqual(self.assert_state(State.COMPATIBLE, pack).band, Band.CONTAINING)
        self.assert_state(State.REJECTED, pack, p=ScoringPolicy(packs=PackPolicy.FORBID))

    def test_identity_hooks_require_structured_qualified_evidence(self):
        claim = ReleaseIdentity(reference())
        with self.assertRaises(ValueError):
            ReleaseObservation(ObservationOrigin.TITLE, 'title', identities=(claim,))
        with self.assertRaises(ValueError):
            ReleaseIdentity(reference('https://example.test/?apikey=private'))
        with self.assertRaises(ValueError):
            ReleaseIdentity(reference('issue', ResourceKind.ISSUE), reference(provider='comicvine'))
        with self.assertRaises((TypeError, ValueError)):
            ReleaseIdentity(reference(), 'invalid')


class ScoringDatabaseTests(ImportHarness, TestCase):
    def test_one_snapshot_four_reads_for_all_batch_sizes(self):
        path = self.comic_file()
        import_library([{'id': 2127, 'filepath': path}])
        with patch('backend.internals.identification.get_db', side_effect=self.db.cursor):
            statements = []
            self.db.set_trace_callback(statements.append)
            before = self.db.total_changes
            snapshot = local_matching_snapshot(('comicvine', 'metron'))
            wanted = build_wanted_target(snapshot, 1, (snapshot.children[1][0].id,))
            for count in (1, 100, 1000):
                evaluate_releases(wanted, (candidate() for _ in range(count)))
            self.db.set_trace_callback(None)
            self.assertEqual(sum(s.lstrip().upper().startswith('SELECT') for s in statements), 4)
            self.assertEqual(self.db.total_changes, before)

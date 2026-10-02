"""Coverage membership comes exclusively from per-ID Phase 5B receipts."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase

from Tbackend.features import release_scoring as facts
from Tbackend.internals import wanted as persistence

from backend.base.release_candidate import (CoverageKind, ObservationOrigin,
                                            ReleaseCoverage,
                                            ReleaseObservation)
from backend.base.release_evaluation import ScoringPolicy, WantedIssue
from backend.base.release_search import SearchState
from backend.features.wanted_automation import WantedAutomation
from backend.implementations.release_scoring import evaluate_releases
from backend.internals.wanted import WantedConflict


class CoverageTests(TestCase):
    setUp = persistence.WantedPersistenceTests.setUp
    migrate = persistence.WantedPersistenceTests.migrate

    def context(self, exact=False):
        self.migrate()
        service = WantedAutomation(self.path)
        self.addCleanup(service.close)
        target = facts.target(catalog=tuple(WantedIssue(i, str(i), year=2016, owned=False) for i in range(1, 7)))
        containing = facts.candidate('Batman #1-6 (2016).cbz')
        candidates = [containing]
        if exact:
            other = facts.candidate('Batman #6 (2016).cbz')
            candidates.append(replace(other, source=replace(other.source, key='exact-source')))
        session = SimpleNamespace(target=target, policy=ScoringPolicy(), state=SearchState.COMPLETE,
            evaluations=evaluate_releases(target, candidates))
        return service, session

    def test_range_reserves_existing_local_members_only(self):
        service, session = self.context()
        self.assertEqual(service.covered_ids(session, session.evaluations[0], frozenset()), (1, 2, 3, 4, 5, 6))

    def test_different_exact_winner_for_another_member_abstains_entire_pack(self):
        service, session = self.context(exact=True)
        with self.assertRaisesRegex(WantedConflict, 'coverage_preference_conflict'):
            service.covered_ids(session, session.evaluations[0], frozenset())

    def test_manual_containing_selection_reserves_proven_members_without_auto_choice(self):
        service, session = self.context(exact=True)
        self.assertEqual(service.covered_ids(session, session.evaluations[0],
            frozenset(), automatic=False), (1, 2, 3, 4, 5, 6))

    def test_active_reservation_for_another_member_abstains_entire_pack(self):
        service, session = self.context()
        target = replace(session.target, issue_ids=(6,))
        evaluation = evaluate_releases(target, (facts.candidate('Batman #6 (2016).cbz'),))[0]
        run = service.store.begin_search(target, 'automatic_missing')
        service.store.reserve(run, evaluation)
        with self.assertRaisesRegex(WantedConflict, 'coverage_reserved'):
            service.covered_ids(session, session.evaluations[0], frozenset())

    def test_explicit_noncontiguous_source_set_never_reserves_gaps(self):
        service, session = self.context()
        candidate = replace(facts.candidate(), observations=(ReleaseObservation(
            ObservationOrigin.STRUCTURED, 'fixture.explicit-set', series='Batman', year=2016,
            coverage=ReleaseCoverage(CoverageKind.SET, ('1', '3', '5'))),))
        session.evaluations = evaluate_releases(session.target, (candidate,))
        self.assertEqual(service.covered_ids(session, session.evaluations[0], frozenset()), (1, 3, 5))

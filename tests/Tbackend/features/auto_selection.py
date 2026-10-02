"""Automation preference is not deterministic presentation order."""

from dataclasses import replace
from unittest import TestCase

from backend.base.auto_selection import (AutoSelectionPolicy,
                                         SelectionReason as R)
from backend.base.release_evaluation import ScoringPolicy, SourcePriority
from backend.base.release_search import SearchState
from backend.implementations.auto_selection import select_automatically
from backend.implementations.release_scoring import evaluate_releases
from tests.Tbackend.features.release_scoring import candidate, target


class AutomaticSelectionTests(TestCase):
    def decide(self, candidates, **kwargs):
        scoring = kwargs.pop('scoring', ScoringPolicy())
        wanted = kwargs.pop('wanted', target())
        state = kwargs.pop('search_state', SearchState.COMPLETE)
        return select_automatically(evaluate_releases(wanted, candidates, scoring), search_state=state, **kwargs)

    def test_unique_exact(self):
        decision = self.decide([candidate()])
        self.assertEqual(decision.reason, R.UNIQUE_BEST)
        self.assertEqual(decision.selected.candidate, candidate())

    def test_exact_beats_range_unchanged_rank(self):
        exact = candidate()
        self.assertEqual(self.decide([candidate('Batman #1-10 (2016).cbz'), exact]).selected.candidate, exact)

    def test_containing_range_is_eligible(self):
        self.assertEqual(self.decide([candidate('Batman #1-10 (2016).cbz')]).reason, R.UNIQUE_BEST)

    def test_presentation_keys_never_break_quality_tie(self):
        first = candidate()
        second = replace(first, source=replace(first.source, key='another'))
        for values in ([first, second], [second, first]):
            decision = self.decide(values)
            self.assertEqual(decision.reason, R.QUALITY_TIE)
            self.assertIsNone(decision.selected)

    def test_explicit_priority_can_break_semantic_tie(self):
        first = candidate()
        second = replace(first, source=replace(first.source, key='preferred'))
        policy = ScoringPolicy(source_priorities=(SourcePriority(second.source.kind, second.source.key, 10),))
        self.assertEqual(self.decide([first, second], scoring=policy).selected.candidate, second)

    def test_priority_cannot_rescue_wrong_issue(self):
        wrong = candidate('Batman #6 (2016).cbz')
        wrong = replace(wrong, source=replace(wrong.source, key='preferred'))
        policy = ScoringPolicy(source_priorities=(SourcePriority(wrong.source.kind, wrong.source.key, 1000),))
        self.assertEqual(self.decide([wrong, candidate()], scoring=policy).selected.candidate, candidate())

    def test_partial_and_failed_search_abstain(self):
        for state in (SearchState.PARTIAL, SearchState.FAILED, SearchState.DISABLED):
            self.assertEqual(self.decide([candidate()], search_state=state).reason, R.SEARCH_INCOMPLETE)

    def test_rejected_and_review_never_select(self):
        for title in ('Batman #6 (2016).cbz', 'Batman Complete Series (2016).cbz', 'Batman (2016).cbz'):
            self.assertIsNone(self.decide([candidate(title)]).selected)

    def test_blocked_exact_allows_eligible_range(self):
        exact, containing = candidate(), candidate('Batman #1-10 (2016).cbz')
        containing = replace(containing, source=replace(containing.source, key='range-source'))
        self.assertEqual(self.decide([exact, containing], unavailable={exact.candidate_id}).selected.candidate, containing)

    def test_all_blocked_is_not_no_match(self):
        self.assertEqual(self.decide([candidate()], unavailable={candidate().candidate_id}).reason, R.OPERATIONALLY_BLOCKED)

    def test_empty_result(self):
        self.assertEqual(self.decide([]).reason, R.NO_ACCEPTABLE_RELEASE)

    def test_opaque_label_no_numeric_fiction(self):
        self.assertEqual(self.decide([candidate('Batman #1A (2016).cbz')], wanted=target('1A')).reason, R.UNIQUE_BEST)

    def test_owned_target_no_upgrade(self):
        wanted = target()
        wanted = replace(wanted, catalog=tuple(replace(i, owned=True) for i in wanted.catalog))
        self.assertIsNone(self.decide([candidate()], wanted=wanted).selected)

    def test_policy_is_versioned(self):
        self.assertEqual(AutoSelectionPolicy().fingerprint, AutoSelectionPolicy().fingerprint)
        with self.assertRaises(ValueError):
            AutoSelectionPolicy('future')

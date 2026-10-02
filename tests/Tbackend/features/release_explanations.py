"""Presentation receipts, frozen scorer behavior, no IO and no decision authority."""

import json
import random
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from fixtures.release_explanations import evaluations, receipt_digest

from backend.base.definitions import SpecialVersion
from backend.base.release_candidate import (CoverageKind, ObservationOrigin,
                                            PackKind, ReleaseCoverage,
                                            ReleaseIdentity, ReleaseYearKind)
from backend.base.release_evaluation import (SCORING_POLICY,
                                             Compatibility as State,
                                             CoverageBand as Band, PackPolicy,
                                             RankingTier, ReleaseEvaluation,
                                             Rule, RuleOutcome as Outcome,
                                             ScoreComponent, ScoringPolicy,
                                             SourcePriority, TargetKind,
                                             WantedIssue)
from backend.base.release_explanation import (EXPLANATION_POLICY, EntryKind,
                                              InvalidEvaluationReceipt,
                                              UnsupportedExplanation)
from backend.implementations.release_explanations import (BAND_LABELS,
                                                          RULE_MESSAGES,
                                                          STATE_LABELS,
                                                          evaluation_identity,
                                                          explain_ranking,
                                                          explain_release,
                                                          explain_releases,
                                                          explanation_counts,
                                                          preview_explanation,
                                                          preview_ranking)
from backend.implementations.release_scoring import (compare_evaluations,
                                                     evaluate_release,
                                                     rank_evaluations)
from tests.Tbackend.features.release_scoring import (candidate, fact_candidate,
                                                     reference, structured,
                                                     target)

BASELINE_DIGEST = '213e685bd368dc13610aed8d4d3a351d21a7dc129a5c3411686b9e7e60273c85'


def receipt(components=None, state=State.COMPATIBLE, band=Band.EXACT, **kwargs):
    """Direct, precomputed inputs; deliberately no evaluator invocation."""
    components = components if components is not None else (
        ScoreComponent('series', Rule.SERIES_EXACT, Outcome.MATCH, 100),
        ScoreComponent('year', Rule.YEAR_MATCH, Outcome.MATCH, 40),
        ScoreComponent('coverage', Rule.ISSUE_RAW, Outcome.MATCH, 140),
        ScoreComponent('language', Rule.LANGUAGE_UNKNOWN, Outcome.UNKNOWN),
    )
    return ReleaseEvaluation(target(), candidate(), state, band,
        sum(c.points for c in components) if state == State.COMPATIBLE else None,
        tuple(components), 0, SCORING_POLICY, ScoringPolicy().fingerprint, **kwargs)


class ReleaseExplanationTests(TestCase):
    def explain(self, c=None, t=None, p=None):
        return explain_release(evaluate_release(t or target(), c or candidate(), p or ScoringPolicy()))

    def test_frozen_5b_receipts_and_ranking(self):
        before = evaluations()
        self.assertEqual(receipt_digest(before), BASELINE_DIGEST)
        for e in before:
            explain_release(e)
        self.assertEqual(receipt_digest(before), BASELINE_DIGEST)
        self.assertEqual(receipt_digest(evaluations()), BASELINE_DIGEST)

    def test_exhaustive_rules_states_bands(self):
        self.assertEqual(set(RULE_MESSAGES), set(Rule))
        self.assertEqual(set(STATE_LABELS), set(State))
        self.assertEqual(set(BAND_LABELS), set(Band))
        for rule in Rule:
            with self.subTest(rule=rule):
                c = ScoreComponent('test', rule, Outcome.NEUTRAL)
                entry = explain_release(receipt((c,))).entries[0]
                self.assertEqual(entry.key, rule.value)
                self.assertTrue(entry.message)

    def test_every_rule_can_represent_a_retained_rejection_gate(self):
        # Rejection codes are Rule values selected by gates, not another enum.
        for rule in Rule:
            with self.subTest(rule=rule):
                e = receipt((ScoreComponent('test', rule, Outcome.MISMATCH, gate=State.REJECTED),), State.REJECTED)
                x = explain_release(e)
                self.assertEqual(tuple(v.rule for v in x.entries if v.kind == EntryKind.REJECTION), e.rejections)
                self.assertIsNone(x.score)

    def test_state_and_band_matrix(self):
        for state in State:
            for band in Band:
                gate = state if state != State.COMPATIBLE else None
                e = receipt((ScoreComponent('coverage', Rule.ISSUE_UNKNOWN, Outcome.UNKNOWN, gate=gate),), state, band)
                x = explain_release(e)
                self.assertEqual((x.state, x.band), (state, band))
                self.assertTrue(x.headline.startswith(STATE_LABELS[state]))

    def test_exact_score_receipt(self):
        x = explain_release(receipt())
        self.assertEqual(x.headline, 'Compatible — exact issue coverage')
        self.assertEqual(x.score, 280)
        self.assertEqual(tuple(v.points for v in x.entries), (100, 40, 140, 0))
        self.assertNotIn('confidence', x.headline.lower())

    def test_range_penalty_not_rejection(self):
        x = self.explain(candidate('Batman #1-10 (2016)'))
        self.assertIn('containing issue range', x.headline)
        self.assertEqual(next(v.points for v in x.entries if v.rule == Rule.ISSUE_RANGE), 70)
        self.assertEqual(next(v.points for v in x.entries if v.kind == EntryKind.PENALTY), -20)
        self.assertEqual(preview_explanation(x)['rejections'], [])

    def test_wrong_issue_no_score(self):
        x = self.explain(candidate('Batman #6 (2016)'))
        self.assertEqual(x.state, State.REJECTED)
        self.assertIn('not covered', x.headline)
        self.assertIsNone(preview_explanation(x)['score'])

    def test_identity_conflict_not_high_score(self):
        x = self.explain(fact_candidate(identities=(ReleaseIdentity(reference('wrong')),)))
        self.assertEqual(x.state, State.REJECTED)
        self.assertIn('identity conflicts', x.headline)
        self.assertIsNone(x.score)
        self.assertTrue(all(v.points == 0 for v in x.entries))

    def test_multiple_rejections_preserve_order(self):
        e = evaluate_release(target(), fact_candidate('6', extension='.exe'))
        x = explain_release(e)
        self.assertEqual(tuple(v.rule for v in x.entries if v.kind == EntryKind.REJECTION), e.rejections)
        self.assertGreater(len(e.rejections), 1)

    def test_opaque_raw_labels_lossless(self):
        for label in ('1', '01', '1.0', '1.5', '1A', '1.01', '[nn]', 'Annual', 'Special'):
            with self.subTest(label=label):
                x = self.explain(fact_candidate(label), target(label))
                self.assertIn('Exact raw', next(v.message for v in x.entries if v.rule == Rule.ISSUE_RAW))
                self.assertEqual(json.loads(x.target_json)['issues'][0]['label'], label)
                detail = next(v for v in x.entries if v.rule == Rule.ISSUE_RAW)
                self.assertTrue(any(label in ev.values for ev in detail.evidence))

    def test_numeric_equivalence_not_raw_equality(self):
        x = self.explain(fact_candidate('01'), target('1'))
        entry = next(v for v in x.entries if v.rule == Rule.ISSUE_NUMERIC)
        self.assertIn('numeric-equivalent', entry.message)
        self.assertTrue(any('01' in ev.values for ev in entry.evidence))
        self.assertEqual(json.loads(x.target_json)['issues'][0]['label'], '1')

    def test_suffix_decimal_not_fabricated_equivalence(self):
        x = self.explain(fact_candidate('1.01'), target('1A'))
        self.assertEqual(x.state, State.REJECTED)
        self.assertFalse(any(v.rule == Rule.ISSUE_NUMERIC for v in x.entries))

    def test_numeric_ambiguity_review(self):
        t = target(catalog=(WantedIssue(5, '1'), WantedIssue(6, '01')))
        x = self.explain(fact_candidate('1.0'), t)
        self.assertEqual(x.state, State.REVIEW)
        self.assertIn('ambiguous', x.headline)

    def test_undetermined_not_mismatch(self):
        x = self.explain(structured(series='Batman'))
        self.assertEqual(x.state, State.UNDETERMINED)
        self.assertIn('could not be determined', x.headline)
        self.assertIsNone(x.score)

    def test_unknown_language_neutral_and_hidden_concise(self):
        dto = preview_explanation(self.explain())
        index = next(i for i, v in enumerate(dto['entries']) if v['rule'] == Rule.LANGUAGE_UNKNOWN.value)
        self.assertEqual(dto['entries'][index]['kind'], 'neutral')
        self.assertNotIn(index, dto['concise_entries'])

    def test_set_not_compressed_range(self):
        x = self.explain(candidate('Batman #1,3,5 (2016)'))
        self.assertIn('containing issue set', x.headline)
        values = next(v.evidence for v in x.entries if v.rule == Rule.ISSUE_SET)
        self.assertTrue(any(('1', '3', '5') == ev.values[1:4] for ev in values))

    def test_exact_finite_coverage(self):
        t = target(catalog=(WantedIssue(5, '1'), WantedIssue(6, '3')), ids=(5, 6))
        self.assertIn('exact finite coverage', self.explain(candidate('Batman #1,3 (2016)'), t).headline)

    def test_pack_review_allowed_and_forbidden(self):
        c = structured(series='Batman', pack=PackKind.SERIES, coverage=ReleaseCoverage(CoverageKind.PACK))
        x = self.explain(c)
        self.assertIn('membership is not verified', x.headline)
        allowed = self.explain(c, p=ScoringPolicy(packs=PackPolicy.ALLOW))
        self.assertIn('allowed complete-pack claim', allowed.headline)
        self.assertTrue(any(v.points == -40 for v in allowed.entries))
        self.assertEqual(self.explain(c, p=ScoringPolicy(packs=PackPolicy.FORBID)).state, State.REJECTED)

    def test_generic_pack_not_complete_claim(self):
        c = structured(series='Batman', pack=PackKind.MULTI_ISSUE, coverage=ReleaseCoverage(CoverageKind.PACK))
        x = self.explain(c)
        self.assertNotIn('complete', x.headline)
        self.assertEqual(json.loads(x.candidate_json)['observations'][0]['pack'], 'multi_issue')

    def test_special_axes_and_vai(self):
        for special, marker in ((SpecialVersion.TPB, 'TPB'), (SpecialVersion.HARD_COVER, 'HC'),
                                (SpecialVersion.OMNIBUS, 'Omnibus'), (SpecialVersion.ONE_SHOT, 'One-Shot')):
            x = self.explain(candidate(f'Batman {marker} (2016)'), target('1', kind=TargetKind.COLLECTION, special=special))
            self.assertIn('special-publication', x.coverage)
            axis = 'Physical format' if marker in ('TPB', 'HC') else 'Publication kind'
            self.assertTrue(any(axis in v.message for v in x.entries))
        x = self.explain(candidate('Batman Volume 5 (2016)'), target(special=SpecialVersion.VOLUME_AS_ISSUE))
        self.assertTrue(any('Volume-as-Issue' in v.message for v in x.entries))

    def test_graphic_novel_no_new_mapping(self):
        x = self.explain(fact_candidate(publication_kind='Graphic Novel'))
        self.assertEqual(x.state, State.REVIEW)
        self.assertTrue(any('Graphic Novel' in ev.values for v in x.entries for ev in v.evidence))

    def test_year_roles_and_conflict_values(self):
        c = fact_candidate(year_kind=ReleaseYearKind.SERIES)
        c = replace(c, observations=(*c.observations, replace(c.observations[0], year=2017, locator='other')))
        x = self.explain(c)
        self.assertEqual(x.state, State.REVIEW)
        ev = next(v.evidence for v in x.entries if v.rule == Rule.EVIDENCE_CONFLICT)
        self.assertEqual({n for v in ev for n in v.values}, {'2016', '2017'})
        self.assertTrue(all('Series-start year' in v.label for v in ev))

    def test_no_double_count_explanation(self):
        c = fact_candidate()
        c = replace(c, observations=tuple(replace(c.observations[0], origin=o) for o in ObservationOrigin))
        x = self.explain(c)
        self.assertEqual(sum(v.points for v in x.entries if v.axis == 'series'), 100)
        self.assertEqual(len(next(v.evidence for v in x.entries if v.rule == Rule.SERIES_EXACT)), 3)

    def test_language_and_archive_preferences(self):
        x = self.explain(fact_candidate(language='en', extension='.cbz'), target(language='en'),
                         ScoringPolicy(archive_order=('.cbr', '.cbz')))
        preference = next(v for v in x.entries if v.rule == Rule.ARCHIVE_PREFERENCE)
        self.assertEqual(preference.points, 9)
        self.assertNotIn('quality', preference.message)
        self.assertTrue(any(v.rule == Rule.LANGUAGE_MATCH and v.points == 10 for v in x.entries))
        self.assertEqual(self.explain(fact_candidate(language='fr'), target(language='en')).state, State.REJECTED)
        self.assertEqual(self.explain(fact_candidate(extension='.exe')).state, State.REJECTED)

    def test_unchanged_component_order_and_points(self):
        for e in evaluations():
            x = explain_release(e)
            self.assertEqual([(v.rule, v.points, v.outcome, v.gate) for v in x.entries],
                             [(v.rule, v.points, v.outcome, v.gate) for v in e.components])

    def test_presenter_uses_arbitrary_receipted_points_not_weights(self):
        e = receipt((ScoreComponent('series', Rule.SERIES_EXACT, Outcome.MATCH, 321),))
        self.assertEqual(explain_release(e).score, 321)
        self.assertEqual(explain_release(e).entries[0].points, 321)

    def test_zero_score_is_not_missing(self):
        self.assertEqual(explain_release(receipt(())).score, 0)

    def test_total_mismatch_is_not_repaired(self):
        with self.assertRaises(InvalidEvaluationReceipt):
            explain_release(replace(receipt(), score=999))

    def test_noncompatible_score_invariant(self):
        with self.assertRaises(InvalidEvaluationReceipt):
            explain_release(replace(receipt(), state=State.REVIEW))

    def test_source_points_invariant(self):
        with self.assertRaises(InvalidEvaluationReceipt):
            explain_release(receipt((ScoreComponent('source', Rule.SOURCE_PREFERENCE, Outcome.NEUTRAL, 5),)))

    def test_unknown_policy_rule_outcome_fail_safe(self):
        for e in (replace(receipt(), policy_id='future'),
                  receipt((ScoreComponent('x', 'future', Outcome.NEUTRAL),)),
                  receipt((ScoreComponent('x', Rule.DIAGNOSTIC, 'future'),))):
            with self.assertRaises(UnsupportedExplanation):
                explain_release(e)

    def test_identity_binds_target_candidate_receipt_and_policy(self):
        e = receipt()
        variants = (replace(e, target=target('6')), replace(e, candidate=replace(e.candidate, raw_title='Other')),
                    replace(e, policy_fingerprint='a' * 64), replace(e, source_priority=1),
                    replace(e, components=tuple(reversed(e.components))))
        self.assertEqual(len({evaluation_identity(v) for v in (e, *variants)}), 6)

    def test_immutable_explanation_and_detached_dto(self):
        x = explain_release(receipt())
        with self.assertRaises(FrozenInstanceError):
            x.score = 900
        dto = preview_explanation(x)
        dto['candidate']['raw_title'] = 'changed'
        self.assertNotEqual(preview_explanation(x)['candidate']['raw_title'], 'changed')

    def test_evidence_order_is_canonical_component_order_preserved(self):
        e = evaluate_release(target(), candidate())
        reordered = replace(e, candidate=replace(e.candidate, observations=tuple(reversed(e.candidate.observations))),
                            components=tuple(replace(c, evidence=tuple(reversed(c.evidence))) for c in e.components))
        self.assertEqual(explain_release(e), explain_release(reordered))

    def test_secret_locators_never_transported(self):
        e = receipt()
        o = replace(e.candidate.observations[0], locator='https://example.test/?apikey=SECRET', policy='Cookie=SECRET')
        c = replace(e.candidate, observations=(o,), result_id='PRIVATE_RESULT')
        ref = f'{o.origin.value}:{o.locator}:{o.policy}:series'
        e = replace(e, candidate=c, components=(ScoreComponent('series', Rule.SERIES_EXACT, Outcome.MATCH, 280, (ref, 'Bearer SECRET')),))
        dto = preview_explanation(explain_release(e))
        rendered = json.dumps(dto)
        for secret in ('SECRET', 'PRIVATE_RESULT', c.acquisition.key, 'resolver_key', 'apikey=', 'Cookie='):
            self.assertNotIn(secret, rendered)
        self.assertEqual(json.loads(rendered), dto)

    def test_html_shaped_facts_remain_data(self):
        e = receipt()
        c = replace(e.candidate, raw_title='<script>alert(1)</script>', source=replace(e.candidate.source, name='<img src=x onerror=alert(1)>'))
        dto = preview_explanation(explain_release(replace(e, candidate=c)))
        self.assertEqual(dto['candidate']['raw_title'], c.raw_title)
        self.assertEqual(dto['candidate']['source']['name'], c.source.name)

    def test_bulk_counts_and_input_order(self):
        values = evaluations()
        explained = explain_releases(values)
        self.assertEqual(tuple(v.evaluation_id for v in explained), tuple(evaluation_identity(e) for e in values))
        counts = explanation_counts(explained)
        self.assertEqual(sum(counts.values()), len(values))
        self.assertEqual(set(counts), {s.value for s in State})

    def test_bulk_precomputed_no_io_no_rescoring(self):
        e = receipt()
        forbidden = ('socket.socket', 'sqlite3.connect', 'builtins.open', 'os.stat',
                     'backend.implementations.release_scoring.evaluate_release',
                     'backend.implementations.release_scoring.evaluate_releases',
                     'backend.implementations.release_scoring.build_wanted_target',
                     'backend.features.library_identification.local_matching_snapshot',
                     'backend.implementations.release_candidates.parse_release_title')
        with ExitStack() as stack:
            for path in forbidden:
                stack.enter_context(patch(path, side_effect=AssertionError('Explanation IO/decision call forbidden')))
            for count in (1, 100, 1000, 10000):
                started = perf_counter()
                result = explain_releases(e for _ in range(count))
                self.assertEqual(len(result), count)
                self.assertTrue(all(v.score == e.score for v in result))
                if count == 10000:
                    print(f'10,000 explanations: {perf_counter() - started:.3f}s; precomputed, no IO/rescoring')


class RankingExplanationTests(TestCase):
    def assert_tier(self, left, right, tier):
        result = explain_ranking(left, right)
        self.assertEqual(result.tier, tier)
        self.assertEqual(result.order, compare_evaluations(left, right).order)
        ordered = rank_evaluations((right, left))
        self.assertEqual(ordered[0], left if result.order < 0 else right)
        self.assertEqual(explain_ranking(right, left).order, -result.order)
        return result

    def test_state_precedes_all_preferences(self):
        good = evaluate_release(target(), candidate())
        bad = evaluate_release(target(), candidate('Batman #6 (2016)'))
        result = self.assert_tier(good, bad, RankingTier.STATE)
        self.assertNotIn('source priority', result.message)

    def test_coverage_precedes_score(self):
        left = receipt()
        right = replace(receipt((ScoreComponent('coverage', Rule.ISSUE_RANGE, Outcome.MATCH, 999),)), band=Band.CONTAINING)
        self.assert_tier(left, right, RankingTier.COVERAGE)

    def test_score_precedes_source(self):
        left = receipt()
        right = replace(receipt((ScoreComponent('series', Rule.SERIES_EXACT, Outcome.MATCH, 100),)), source_priority=999999)
        self.assert_tier(left, right, RankingTier.SCORE)

    def test_source_priority_tie_only(self):
        left, right = replace(receipt(), source_priority=10), receipt()
        result = self.assert_tier(left, right, RankingTier.SOURCE_PRIORITY)
        self.assertIn('tie-break only', result.message)
        self.assertEqual(left.score, right.score)
        self.assertEqual(preview_ranking(result)['tier'], 'source_priority')

    def test_preferred_wrong_source_stays_rejected(self):
        c = candidate('Batman #6 (2016)')
        p = ScoringPolicy(source_priorities=(SourcePriority(c.source.kind, c.source.key, 999999),))
        e = evaluate_release(target(), c, p)
        x = explain_release(e)
        self.assertEqual(x.state, State.REJECTED)
        self.assertFalse(any(v.kind == EntryKind.TIE_BREAK for v in x.entries))

    def test_semantic_key_not_quality(self):
        left = receipt()
        right = replace(left, candidate=replace(left.candidate, raw_title='Other display text'))
        result = self.assert_tier(left, right, RankingTier.SEMANTIC)
        self.assertIn('not evidence of greater quality', result.message)

    def test_stable_identity_tiers(self):
        from backend.base.release_candidate import SourceKind
        left = receipt()
        for changed, tier in ((replace(left.candidate.source, kind=SourceKind.NEWZNAB), RankingTier.SOURCE_KIND),
                              (replace(left.candidate.source, key='other'), RankingTier.SOURCE_KEY)):
            self.assert_tier(left, replace(left, candidate=replace(left.candidate, source=changed)), tier)
        self.assert_tier(left, replace(left, candidate=replace(left.candidate, result_id='other')), RankingTier.CANDIDATE_ID)

    def test_equivalent_no_winner(self):
        result = explain_ranking(receipt(), receipt())
        self.assertEqual(result.order, 0)
        self.assertIsNone(result.tier)
        self.assertNotIn('winner', preview_ranking(result))

    def test_scope_mismatch_refused(self):
        for other in (replace(receipt(), target=target('6')), replace(receipt(), policy_fingerprint='b' * 64)):
            with self.assertRaises(ValueError):
                explain_ranking(receipt(), other)

    def test_shuffled_ranking_explanations(self):
        values = list(evaluations())
        expected = tuple(evaluation_identity(v) for v in rank_evaluations(values))
        for seed in range(5):
            random.Random(seed).shuffle(values)
            ordered = rank_evaluations(values)
            self.assertEqual(tuple(evaluation_identity(v) for v in ordered), expected)
            for left, right in zip(ordered, ordered[1:]):
                self.assertLessEqual(explain_ranking(left, right).order, 0)

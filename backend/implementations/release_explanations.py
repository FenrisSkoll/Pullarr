"""Project recorded scoring outcomes. No parsing, matching, scoring or I/O."""

from dataclasses import replace
from hashlib import sha256
from json import dumps, loads
from types import MappingProxyType
from typing import Iterable, Tuple

from backend.base.release_candidate import (ObservationOrigin,
                                            ReleaseYearKind, preview_release)
from backend.base.release_evaluation import (SCORING_POLICY,
                                             Compatibility as State,
                                             CoverageBand as Band, RankingTier,
                                             ReleaseEvaluation, Rule,
                                             RuleOutcome as Outcome)
from backend.base.release_explanation import (EntryKind, EvidenceSummary,
                                              ExplanationEntry,
                                              InvalidEvaluationReceipt,
                                              RankingExplanation,
                                              ReleaseExplanation,
                                              UnsupportedExplanation)
from backend.implementations.release_scoring import compare_evaluations

# No weights here. Keys are the scorer's semantic identifiers, not English text.
RULE_MESSAGES = MappingProxyType({
    Rule.QUALITY: 'Quality Profile admission and strict improvement; release claims remain provisional until file verification',
    Rule.SERIES_ID: 'Qualified publication identity matched',
    Rule.IDENTITY_CONFLICT: 'Qualified publication or issue identity conflicts with the target',
    Rule.IDENTITY_UNKNOWN: 'Cross-provider identity relationship is not proven',
    Rule.SERIES_EXACT: 'Exact canonical series title matched',
    Rule.SERIES_NORMALIZED: 'Normalized series title matched',
    Rule.SERIES_ALIAS: 'Explicit series alias matched',
    Rule.SERIES_WRONG: 'Series title disagrees with the target',
    Rule.SERIES_UNKNOWN: 'Series identity could not be determined',
    Rule.EVIDENCE_CONFLICT: 'Release observations disagree; no interpretation was chosen',
    Rule.YEAR_MATCH: 'Year evidence matched',
    Rule.YEAR_WRONG: 'Known year evidence is incompatible with the target',
    Rule.YEAR_UNKNOWN: 'Year match is not established; optional evidence adds no points',
    Rule.YEAR_UNTYPED: 'Release-year meaning cannot be resolved safely',
    Rule.VOLUME_MATCH: 'Canonical volume number matched',
    Rule.VOLUME_WRONG: 'Canonical volume number is incompatible',
    Rule.VOLUME_UNKNOWN: 'Canonical meaning of volume evidence is unavailable',
    Rule.ISSUE_ID: 'Qualified issue identity matched',
    Rule.ISSUE_RAW: 'Exact raw issue-label coverage matched',
    Rule.ISSUE_NUMERIC: 'Safe numeric-equivalent issue coverage matched; raw spelling may differ',
    Rule.ISSUE_SET: 'Explicit issue set includes the wanted coverage',
    Rule.ISSUE_RANGE: 'Release range contains the wanted coverage',
    Rule.ISSUE_WRONG: 'Wanted issue coverage is not covered by this release',
    Rule.ISSUE_UNKNOWN: 'Issue coverage could not be determined',
    Rule.NUMBER_UNAVAILABLE: 'Required numeric or range interpretation is unavailable',
    Rule.NUMBER_AMBIGUOUS: 'Issue-number interpretation is ambiguous',
    Rule.SPECIAL_MATCH: 'Publication format evidence matched',
    Rule.SPECIAL_WRONG: 'Publication form is incompatible with the target',
    Rule.SPECIAL_UNKNOWN: 'Required publication-format meaning is unavailable',
    Rule.SOLE_SPECIAL: 'Sole special-publication compatibility recorded',
    Rule.VAI: 'Volume-as-Issue target uses release volume evidence as issue coverage',
    Rule.PACK: 'Complete-pack membership is claimed and permitted by the scoring policy',
    Rule.PACK_FORBIDDEN: 'Current scoring policy forbids this surplus or pack coverage',
    Rule.PACK_PENALTY: 'Compatible coverage includes additional material and is less preferred',
    Rule.LANGUAGE_MATCH: 'Known language matched',
    Rule.LANGUAGE_WRONG: 'Known language is incompatible with the target',
    Rule.LANGUAGE_UNKNOWN: 'Language comparison is unknown; no mismatch is implied',
    Rule.ARCHIVE_MATCH: 'Archive format is supported; support is not a quality preference',
    Rule.ARCHIVE_WRONG: 'Archive format is unsupported',
    Rule.ARCHIVE_UNKNOWN: 'Archive format is unknown',
    Rule.ARCHIVE_PREFERENCE: 'Explicit archive-format preference',
    Rule.GROUP_PREFERENCE: 'Explicit release-group preference',
    Rule.SOURCE_PREFERENCE: 'Source priority is available only for a ranking tie-break; no score points',
    Rule.SIZE_ZERO: 'Source reports an empty, zero-byte release',
    Rule.SIZE_NEUTRAL: 'Size has no quality-score contribution',
    Rule.ACQUISITION_UNAVAILABLE: 'Acquisition capability is unavailable or unsupported',
    Rule.ACQUISITION_AVAILABLE: 'Acquisition mechanism is described; no download is authorized',
    Rule.OWNED: 'All wanted issues are already owned under the current policy',
    Rule.OWNERSHIP_UNKNOWN: 'Ownership evidence is incomplete or does not exclude this target',
    Rule.DIAGNOSTIC: 'Source-adaptation diagnostic retained by the scorer',
})
STATE_LABELS = MappingProxyType({State.COMPATIBLE: 'Compatible', State.REVIEW: 'Review required',
                               State.UNDETERMINED: 'Insufficient evidence', State.REJECTED: 'Rejected'})
BAND_LABELS = MappingProxyType({Band.EXACT: 'exact coverage', Band.CONTAINING: 'containing coverage',
                              Band.CLAIMED_PACK: 'allowed complete-pack claim', Band.UNKNOWN: 'coverage unknown'})
_ORIGINS = {ObservationOrigin.STRUCTURED: 'Structured source metadata',
            ObservationOrigin.TITLE: 'Release-title parser', ObservationOrigin.LEGACY: 'Legacy DDL observation'}
_FIELDS = ('series', 'year', 'volume', 'coverage', 'physical_format', 'publication_kind',
           'special_version', 'extension', 'language', 'release_group', 'identities')
_AXES = {'physical_format': 'Physical format', 'publication_kind': 'Publication kind'}
_YEAR_LABELS = {ReleaseYearKind.SERIES: 'Series-start year', ReleaseYearKind.ISSUE: 'Issue-publication year',
                ReleaseYearKind.UNSPECIFIED: 'Untyped release year'}
_RANK_LABELS = {RankingTier.STATE: 'compatibility state', RankingTier.COVERAGE: 'coverage band',
                RankingTier.QUALITY: 'Quality Profile group',
                RankingTier.SCORE: 'policy score', RankingTier.SOURCE_PRIORITY: 'configured source priority',
                RankingTier.SOURCE_KIND: 'stable source namespace', RankingTier.SOURCE_KEY: 'stable source identity',
                RankingTier.CANDIDATE_ID: 'stable candidate identity', RankingTier.SEMANTIC: 'stable semantic presentation key'}


def _json(value) -> str:
    return dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _text(value) -> str:
    text = str(value)
    return text if len(text) <= 512 else text[:512] + '… [display shortened]'


def _validate(e: ReleaseEvaluation) -> None:
    if (e.policy_id != SCORING_POLICY or e.state not in STATE_LABELS or e.band not in BAND_LABELS
            or any(c.rule not in RULE_MESSAGES or c.outcome not in tuple(Outcome)
                   or c.gate not in (None, *State) for c in e.components)):
        raise UnsupportedExplanation('Explanation unavailable for this scoring receipt version')
    if any(type(c.points) is not int for c in e.components):
        raise InvalidEvaluationReceipt('Invalid component points')
    if e.state == State.COMPATIBLE:
        if type(e.score) is not int or sum(c.points for c in e.components) != e.score:
            raise InvalidEvaluationReceipt('Score receipt total is inconsistent')
    elif e.score is not None or any(c.points for c in e.components):
        raise InvalidEvaluationReceipt('Non-compatible receipt contains a score')
    if any(c.rule == Rule.SOURCE_PREFERENCE and c.points for c in e.components):
        raise InvalidEvaluationReceipt('Source tie-break cannot contribute score points')


def evaluation_identity(e: ReleaseEvaluation) -> str:
    """Bind exact inputs and ordered receipts, never publish their private values."""
    candidate = replace(e.candidate,
        observations=tuple(sorted(set(e.candidate.observations), key=repr)),
        diagnostics=tuple(sorted(set(e.candidate.diagnostics), key=repr)))
    canonical = replace(e, candidate=candidate,
        components=tuple(replace(c, evidence=tuple(sorted(set(c.evidence)))) for c in e.components))
    return _digest(repr(canonical))


def _evidence_index(e):
    """Dereference recorded locators only; no metadata comparisons or parsing."""
    index = {}
    for o in e.candidate.observations:
        for field in _FIELDS:
            ref = f'{o.origin.value}:{o.locator}:{o.policy or "unversioned"}:{field}'
            value = getattr(o, field)
            label = _ORIGINS[o.origin]
            if field == 'coverage':
                values = (o.coverage.kind.value, *o.coverage.labels, 'Pack observation: ' + o.pack.value)
            elif field == 'year':
                label += ' — ' + _YEAR_LABELS[o.year_kind]
                values = () if value is None else (str(value),)
            elif field == 'volume':
                label += ' — ' + o.volume_kind.value
                values = () if value is None else (value,)
            elif field == 'identities':
                values = tuple(f'{i.reference.provider}:{i.reference.kind.value}:{i.reference.provider_id}'
                               for i in o.identities)
            else:
                values = () if value is None else (value,)
                if not values and field in _AXES and o.special_version:
                    values = ('Compatibility format observation: ' + o.special_version,)
            summary = EvidenceSummary(_digest(ref), o.origin.value, field, label,
                                      tuple(_text(v) for v in values[:32]))
            index.setdefault(ref, set()).add(summary)
    return index


def _evidence(refs, index):
    result = set()
    for ref in refs:
        if ref in index:
            result.update(index[ref])
        else:
            # Never echo unknown locators: they may contain URLs or opaque data.
            result.add(EvidenceSummary(_digest(ref), 'receipt', 'unspecified',
                                      'Scoring receipt reference; detail unavailable'))
    return tuple(sorted(result, key=lambda v: (v.origin, v.field, v.reference, v.values)))


def _kind(component):
    if component.gate is not None:
        return {State.REJECTED: EntryKind.REJECTION, State.REVIEW: EntryKind.REVIEW,
                State.UNDETERMINED: EntryKind.UNDETERMINED}.get(component.gate, EntryKind.NEUTRAL)
    if component.rule == Rule.SOURCE_PREFERENCE:
        return EntryKind.TIE_BREAK
    if component.points > 0:
        return EntryKind.POSITIVE
    if component.points < 0:
        return EntryKind.PENALTY
    return EntryKind.NEUTRAL


def _message(c):
    message = RULE_MESSAGES[c.rule]
    if c.rule in (Rule.SPECIAL_MATCH, Rule.SPECIAL_WRONG, Rule.SPECIAL_UNKNOWN) and c.axis in _AXES:
        message = _AXES[c.axis] + ': ' + message
    if c.rule == Rule.SPECIAL_UNKNOWN and c.outcome == Outcome.NEUTRAL:
        message = 'No required comparison for this ' + _AXES.get(c.axis, 'publication format').lower()
    if c.rule == Rule.ISSUE_UNKNOWN and c.outcome == Outcome.UNAVAILABLE and c.gate == State.REVIEW:
        message = 'Pack coverage is claimed, but membership is not verified'
    return message


def _coverage(e):
    rules = {c.rule for c in e.components if c.axis == 'coverage'}
    if e.band == Band.EXACT:
        if Rule.SOLE_SPECIAL in rules:
            return 'exact special-publication coverage'
        return 'exact issue coverage' if len(e.target.issue_ids) == 1 else 'exact finite coverage'
    if e.band == Band.CONTAINING:
        if Rule.ISSUE_RANGE in rules:
            return 'containing issue range'
        if Rule.ISSUE_SET in rules:
            return 'containing issue set'
    return BAND_LABELS[e.band]


def explain_release(e: ReleaseEvaluation) -> ReleaseExplanation:
    _validate(e)
    index = _evidence_index(e)
    entries = tuple(ExplanationEntry(c.rule.value, c.rule, c.axis, c.outcome, _kind(c),
                    c.points, c.gate, _message(c), _evidence(c.evidence, index)) for c in e.components)
    coverage = _coverage(e)
    reason = next((v.message for v in entries if v.gate == e.state), None)
    headline = STATE_LABELS[e.state] + ' — ' + (coverage if e.state == State.COMPATIBLE else reason or coverage)
    # A narrower version of 5A's explicit safe preview, not asdict(candidate).
    preview = preview_release(e.candidate)
    candidate = {key: preview[key] for key in ('candidate_id', 'raw_title', 'mechanism',
                 'locator_kind', 'size_bytes', 'published_at')}
    if 'torrent_facts' in preview:
        candidate['torrent_facts'] = preview['torrent_facts']
    candidate['source'] = {key: preview['source'][key] for key in ('kind', 'name', 'via')}
    candidate['observations'] = sorted((
        {key: observation[key] for key in (*_FIELDS, 'origin', 'pack', 'year_kind', 'volume_kind')}
        for observation in preview['observations']), key=_json)
    wanted = set(e.target.issue_ids)
    target = {'volume_id': e.target.publication.id, 'kind': e.target.kind.value,
              'series': e.target.publication.title, 'series_year': e.target.publication.year,
              'volume_number': e.target.publication.volume_number,
              'authority': e.target.publication.authority.provider,
              'special_version': e.target.publication.special_version.value,
              'issues': [{'id': i.id, 'label': i.raw_number, 'year': i.year}
                         for i in e.target.catalog if i.id in wanted]}
    return ReleaseExplanation(evaluation_identity(e), e.state, e.band, headline, coverage,
        e.score, entries, e.source_priority, e.policy_id, e.policy_fingerprint,
        _json(candidate), _json(target))


def explain_releases(evaluations: Iterable[ReleaseEvaluation]) -> Tuple[ReleaseExplanation, ...]:
    """Preserve caller order; explanation does not rank or select results."""
    return tuple(explain_release(e) for e in evaluations)


def explanation_counts(values: Iterable[ReleaseExplanation]) -> dict:
    counts = {state.value: 0 for state in State}
    for value in values:
        counts[value.state.value] += 1
    return counts


def preview_explanation(value: ReleaseExplanation) -> dict:
    entries = [{'key': e.key, 'rule': e.rule.value, 'axis': e.axis, 'outcome': e.outcome.value,
                'kind': e.kind.value, 'points': e.points, 'gate': e.gate.value if e.gate else None,
                'message': e.message, 'evidence': [{'reference': v.reference, 'origin': v.origin,
                    'field': v.field, 'label': v.label, 'values': list(v.values)} for v in e.evidence]}
               for e in value.entries]
    return {'evaluation_id': value.evaluation_id, 'state': value.state.value,
            'coverage_band': value.band.value, 'coverage': value.coverage, 'headline': value.headline,
            'score': value.score, 'entries': entries,
            'concise_entries': [i for i, e in enumerate(value.entries)
                                if e.kind not in (EntryKind.NEUTRAL, EntryKind.TIE_BREAK)],
            'rejections': [i for i, e in enumerate(value.entries) if e.kind == EntryKind.REJECTION],
            'review': [i for i, e in enumerate(value.entries) if e.kind == EntryKind.REVIEW],
            'undetermined': [i for i, e in enumerate(value.entries) if e.kind == EntryKind.UNDETERMINED],
            'source_priority': value.source_priority,
            'scoring_policy': value.scoring_policy, 'scoring_fingerprint': value.scoring_fingerprint,
            'explanation_policy': value.policy, 'candidate': loads(value.candidate_json),
            'target': loads(value.target_json)}


def explain_ranking(left: ReleaseEvaluation, right: ReleaseEvaluation) -> RankingExplanation:
    _validate(left)
    _validate(right)
    receipt = compare_evaluations(left, right)
    if receipt.tier is None:
        key, message = 'ranking.equivalent', 'Equivalent under the current ranking policy; no superior result is implied'
    else:
        key = 'ranking.' + receipt.tier.value
        direction = 'before' if receipt.order < 0 else 'after'
        message = 'Left result ranks ' + direction + ' right because of ' + _RANK_LABELS[receipt.tier]
        if receipt.tier in (RankingTier.SOURCE_KIND, RankingTier.SOURCE_KEY, RankingTier.CANDIDATE_ID, RankingTier.SEMANTIC):
            message += '; stable presentation order, not evidence of greater quality'
        if receipt.tier == RankingTier.SOURCE_PRIORITY:
            message += '; tie-break only, no score points'
    return RankingExplanation(evaluation_identity(left), evaluation_identity(right),
                              receipt.order, receipt.tier, key, message)


def preview_ranking(value: RankingExplanation) -> dict:
    return {'left_id': value.left_id, 'right_id': value.right_id, 'order': value.order,
            'tier': value.tier.value if value.tier else None, 'key': value.key,
            'message': value.message, 'explanation_policy': value.policy}

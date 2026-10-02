"""Compatibility first, preferences second. This module has no acquisition calls."""

from dataclasses import replace
from json import dumps
from typing import Iterable, Mapping, Optional, Tuple

from backend.base.definitions import FileConstants, SpecialVersion
from backend.base.import_candidate import ResourceKind
from backend.base.issue_facts import NumberCatalog, NumericRange, SemanticState
from backend.base.release_candidate import (AcquisitionMechanism, CoverageKind,
                                            LocatorKind, ObservationOrigin,
                                            PackKind, ReleaseCandidate,
                                            ReleaseCoverage,
                                            ReleaseDiagnosticCode,
                                            ReleaseVolumeKind, ReleaseYearKind,
                                            preview_release)
from backend.base.release_evaluation import (Compatibility as State,
                                             CoverageBand as Band,
                                             PackPolicy, RankingComparison,
                                             RankingTier, ReleaseEvaluation,
                                             Rule, RuleOutcome as Outcome,
                                             ScoreComponent, ScoringPolicy,
                                             TargetKind, WantedIssue,
                                             WantedTarget)
from backend.implementations.identification import (MatchingSnapshot,
                                                    numeric_label, title_key)

# One award per semantic axis; rejection is never represented by negative points.
_POINTS = {
    Rule.SERIES_ID: 120, Rule.SERIES_EXACT: 100,
    Rule.SERIES_NORMALIZED: 90, Rule.SERIES_ALIAS: 80,
    Rule.YEAR_MATCH: 40, Rule.VOLUME_MATCH: 10,
    Rule.ISSUE_ID: 160, Rule.ISSUE_RAW: 140, Rule.ISSUE_NUMERIC: 130,
    Rule.ISSUE_SET: 80, Rule.ISSUE_RANGE: 70, Rule.PACK: 30,
    Rule.SOLE_SPECIAL: 130, Rule.SPECIAL_MATCH: 20,
    Rule.LANGUAGE_MATCH: 10,
}
_ORIGIN_ORDER = {ObservationOrigin.STRUCTURED: 0, ObservationOrigin.TITLE: 1,
                 ObservationOrigin.LEGACY: 2}
_PHYSICAL = {'tpb': 'tpb', 'trade paperback': 'tpb', 'trade-paperback': 'tpb',
             'hc': 'hard-cover', 'hardcover': 'hard-cover', 'hard-cover': 'hard-cover'}
_PUBLICATION = {'omnibus': 'omnibus', 'one-shot': 'one-shot', 'oneshot': 'one-shot',
                'one shot': 'one-shot'}
_SPECIAL_PHYSICAL = {SpecialVersion.TPB: 'tpb', SpecialVersion.HARD_COVER: 'hard-cover'}
_SPECIAL_KIND = {SpecialVersion.OMNIBUS: 'omnibus', SpecialVersion.ONE_SHOT: 'one-shot'}


def build_wanted_target(snapshot: MatchingSnapshot, volume_id: int,
                        issue_ids: Iterable[int], *, kind: TargetKind = TargetKind.ISSUES,
                        issue_years: Optional[Mapping[int, int]] = None,
                        owned_issue_ids: Optional[Iterable[int]] = None,
                        language: Optional[str] = None,
                        physical_format: Optional[str] = None,
                        publication_kind: Optional[str] = None) -> WantedTarget:
    """Build from an already acquired snapshot; never fetch issues or ownership.

Callers may use the existing four-SELECT matching snapshot loader once per run.
Missing ownership is unknown, not evidence that every issue is unowned.
"""
    volume = snapshot.volumes[volume_id]
    owned = set(owned_issue_ids) if owned_issue_ids is not None else None
    years = issue_years or {}
    catalog = tuple(WantedIssue(i.id, i.raw_number, i.references, years.get(i.id),
                               i.id in owned if owned is not None else None, i.number_facts)
                    for i in snapshot.children[volume_id])
    return WantedTarget(volume, catalog, tuple(issue_ids), kind, language,
                        physical_format, publication_kind)


def policy_from_format_preference(formats: Iterable[str], **kwargs) -> ScoringPolicy:
    """Explicit opt-in translation of existing conversion preference, no Settings().

Unrepresentable conversion-only choices are not invented release formats.
"""
    order = tuple(dict.fromkeys('.' + f.lower().lstrip('.') for f in formats))
    return ScoringPolicy(archive_order=tuple(f for f in order
                         if f in FileConstants.CONTAINER_EXTENSIONS), **kwargs)


class _TargetContext:
    """Per-batch indexes, never persistent/global state or a second target model."""

    def __init__(self, target: WantedTarget):
        self.target = target
        self.issues = {i.id: i for i in target.catalog}
        self.wanted = set(target.issue_ids)
        self.numbers = NumberCatalog.build((i.id, i.raw_number, i.number_facts) for i in target.catalog)
        self.raw = {k: set(v) for k, v in self.numbers.raw.items()}
        self.numeric = {k: set(v) for k, v in self.numbers.numeric.items()}
        self.refs = {}
        for issue in target.catalog:
            for ref in issue.references:
                self.refs.setdefault(ref, set()).add(issue.id)
        self.volume_refs = set((target.publication.authority,) + target.publication.references)
        self.issue_providers = {r.provider for r in self.refs}

    def label(self, value):
        match = self.numbers.match(value, allow_unnumbered=True)
        if match.evidence == 'raw':
            return set(match.issue_ids), Rule.ISSUE_RAW
        return set(match.issue_ids), (Rule.NUMBER_UNAVAILABLE if numeric_label(value) is None else Rule.ISSUE_NUMERIC)


class _Evaluation:
    def __init__(self, context, candidate, policy):
        self.context, self.target = context, context.target
        self.candidate, self.policy = candidate, policy
        self.components = []
        self.band = Band.UNKNOWN
        self.observations = tuple(sorted(set(candidate.observations),
            key=lambda o: (_ORIGIN_ORDER[o.origin], o.locator, repr(o))))
        self.exact_series_id = False
        self.identity_issues = None

    def add(self, axis, rule, outcome=Outcome.MATCH, evidence=(), gate=None, points=None):
        self.components.append(ScoreComponent(axis, rule, outcome,
            _POINTS.get(rule, 0) if points is None else points,
            tuple(sorted(set(evidence))), gate))

    def evidence(self, observation, field):
        return f'{observation.origin.value}:{observation.locator}:{observation.policy or "unversioned"}:{field}'

    def facts(self, field, normalizer=lambda v: v, observations=None):
        observations = self.observations if observations is None else observations
        values = {}
        for o in observations:
            value = getattr(o, field)
            if value is None or value == '':
                continue
            values.setdefault(normalizer(value), []).append(self.evidence(o, field))
        evidence = tuple(sorted({e for refs in values.values() for e in refs}))
        if len(values) > 1:
            self.add(field, Rule.EVIDENCE_CONFLICT, Outcome.CONFLICT, evidence, State.REVIEW)
            return None, evidence
        return next(iter(values), None), evidence

    def identities(self):
        refs = self.context.volume_refs
        providers = {r.provider for r in refs}
        issue_ids, had_issues = set(), False
        series_evidence = []
        for o in self.observations:
            for claim in sorted(set(o.identities), key=repr):
                evidence = (self.evidence(o, 'identities'),)
                volume = claim.reference if claim.reference.kind == ResourceKind.VOLUME else claim.parent
                if volume is not None:
                    if volume in refs:
                        self.exact_series_id = True
                        series_evidence.extend(evidence)
                    elif volume.provider in providers:
                        self.add('identity', Rule.IDENTITY_CONFLICT, Outcome.MISMATCH, evidence, State.REJECTED)
                    else:
                        self.add('identity', Rule.IDENTITY_UNKNOWN, Outcome.UNAVAILABLE, evidence, State.REVIEW)
                if claim.reference.kind == ResourceKind.ISSUE:
                    had_issues = True
                    found = self.context.refs.get(claim.reference, set())
                    if len(found) > 1:
                        self.add('identity', Rule.NUMBER_AMBIGUOUS, Outcome.CONFLICT, evidence, State.REVIEW)
                    elif found:
                        issue_ids.update(found)
                        self.exact_series_id = True
                        series_evidence.extend(evidence)
                    elif claim.reference.provider in self.context.issue_providers:
                        self.add('identity', Rule.IDENTITY_CONFLICT, Outcome.MISMATCH, evidence, State.REJECTED)
                    else:
                        self.add('identity', Rule.IDENTITY_UNKNOWN, Outcome.UNAVAILABLE, evidence, State.REVIEW)
        if had_issues:
            self.identity_issues = issue_ids
            if issue_ids and not self.context.wanted <= issue_ids:
                self.add('identity', Rule.IDENTITY_CONFLICT, Outcome.MISMATCH, gate=State.REJECTED)
        if self.exact_series_id:
            self.add('series', Rule.SERIES_ID, evidence=series_evidence)

    def series(self):
        value, evidence = self.facts('series', title_key)
        p = self.target.publication
        if value is None:
            if not self.exact_series_id:
                self.add('series', Rule.SERIES_UNKNOWN, Outcome.UNKNOWN, evidence, State.UNDETERMINED)
            return
        if value == title_key(p.title):
            rule = Rule.SERIES_EXACT if any(o.series == p.title for o in self.observations) else Rule.SERIES_NORMALIZED
        elif value in {title_key(a) for a in p.aliases}:
            rule = Rule.SERIES_ALIAS
        else:
            self.add('series', Rule.SERIES_WRONG, Outcome.MISMATCH, evidence,
                     State.REVIEW if self.exact_series_id else State.REJECTED)
            return
        if not self.exact_series_id:
            self.add('series', rule, evidence=evidence)

    def year(self):
        p = self.target.publication
        issue_years = {self.context.issues[i].year for i in self.target.issue_ids}
        known_years = issue_years - {None}
        matched, all_evidence = False, []
        for kind in ReleaseYearKind:
            group = tuple(o for o in self.observations if o.year_kind == kind)
            value, evidence = self.facts('year', observations=group)
            all_evidence.extend(evidence)
            if value is None:
                continue
            expected = ({p.year} if kind == ReleaseYearKind.SERIES else known_years
                        if kind == ReleaseYearKind.ISSUE else known_years | {p.year}) - {None}
            if value in expected:
                matched = True
            elif expected:
                definite = (kind == ReleaseYearKind.SERIES or
                            kind == ReleaseYearKind.ISSUE and None not in issue_years or
                            kind == ReleaseYearKind.UNSPECIFIED and p.year is not None and None not in issue_years)
                self.add('year', Rule.YEAR_WRONG if definite else Rule.YEAR_UNTYPED,
                         Outcome.MISMATCH if definite else Outcome.UNAVAILABLE, evidence,
                         State.REJECTED if definite else State.REVIEW)
        self.add('year', Rule.YEAR_MATCH if matched else Rule.YEAR_UNKNOWN,
                 Outcome.MATCH if matched else Outcome.UNKNOWN, all_evidence)

    def volume(self):
        if self.target.publication.special_version == SpecialVersion.VOLUME_AS_ISSUE:
            return
        value, evidence = self.facts('volume', lambda v: numeric_label(v) if numeric_label(v) is not None else v)
        expected = self.target.publication.volume_number
        canonical = any(o.volume is not None and o.volume_kind == ReleaseVolumeKind.CANONICAL for o in self.observations)
        if value is None or expected is None:
            self.add('volume', Rule.VOLUME_UNKNOWN, Outcome.UNKNOWN, evidence)
        elif value == expected and canonical:
            self.add('volume', Rule.VOLUME_MATCH, evidence=evidence)
        elif value != expected:
            self.add('volume', Rule.VOLUME_WRONG if canonical else Rule.VOLUME_UNKNOWN,
                     Outcome.MISMATCH if canonical else Outcome.UNAVAILABLE, evidence,
                     State.REJECTED if canonical else State.REVIEW)
        else:
            self.add('volume', Rule.VOLUME_UNKNOWN, Outcome.UNAVAILABLE, evidence)

    def formats(self):
        p = self.target.publication
        expected_physical = self.target.physical_format or _SPECIAL_PHYSICAL.get(p.special_version)
        expected_kind = self.target.publication_kind or _SPECIAL_KIND.get(p.special_version)
        normal = p.special_version == SpecialVersion.NORMAL and self.target.kind != TargetKind.COLLECTION
        seen = False
        for axis, expected, mapping in (('physical_format', expected_physical, _PHYSICAL),
                                        ('publication_kind', expected_kind, _PUBLICATION)):
            def normalize(value):
                return mapping.get(title_key(value), title_key(value))
            # SpecialVersion is a compatibility observation, not a third format axis.
            observations = []
            for o in self.observations:
                fallback = o.special_version if o.special_version in mapping.values() else None
                observations.append(replace(o, **{axis: getattr(o, axis) or fallback}))
            value, evidence = self.facts(axis, normalize, observations)
            if value is None:
                if expected is not None:
                    seen = True
                    self.add(axis, Rule.SPECIAL_UNKNOWN, Outcome.UNKNOWN, evidence, State.REVIEW)
                continue
            seen = True
            if value not in mapping.values():
                self.add(axis, Rule.SPECIAL_UNKNOWN, Outcome.UNAVAILABLE, evidence, State.REVIEW)
            elif expected is not None:
                if value == normalize(expected):
                    self.add(axis, Rule.SPECIAL_MATCH, evidence=evidence)
                else:
                    self.add(axis, Rule.SPECIAL_WRONG, Outcome.MISMATCH, evidence, State.REJECTED)
            elif normal:
                self.add(axis, Rule.SPECIAL_WRONG, Outcome.MISMATCH, evidence, State.REJECTED)
            else:
                self.add(axis, Rule.SPECIAL_UNKNOWN, Outcome.NEUTRAL, evidence)
        # Retain Kapowarr's sole HC/One-Shot/Omnibus numbered-release convention.
        self.sole_numbered = (
            p.special_version in (SpecialVersion.HARD_COVER, SpecialVersion.ONE_SHOT, SpecialVersion.OMNIBUS)
            and len(self.target.catalog) == 1 and numeric_label(self.target.catalog[0].raw_number) == 1
            and any(o.coverage.kind == CoverageKind.SINGLE and numeric_label(o.coverage.labels[0]) == 1
                    for o in self.observations)
            and not any(o.physical_format or o.publication_kind or o.special_version for o in self.observations)
        )
        if self.sole_numbered:
            self.components = [c for c in self.components if c.rule != Rule.SPECIAL_UNKNOWN]
            self.add('special', Rule.SOLE_SPECIAL, points=20)
        if self.target.kind == TargetKind.COLLECTION and not seen:
            self.add('special', Rule.SPECIAL_UNKNOWN, Outcome.UNAVAILABLE, gate=State.REVIEW)

    def coverage_value(self, coverage, evidence):
        ctx = self.context
        if coverage.kind in (CoverageKind.SINGLE, CoverageKind.SET):
            ids, numeric = set(), False
            for label in coverage.labels:
                found, rule = ctx.label(label)
                if len(found) > 1:
                    return None, Rule.NUMBER_AMBIGUOUS
                if found & ids:
                    return None, Rule.NUMBER_AMBIGUOUS
                ids.update(found)
                numeric |= rule == Rule.ISSUE_NUMERIC
            if len(set(coverage.labels)) != len(coverage.labels):
                return None, Rule.NUMBER_AMBIGUOUS
            if ctx.wanted <= ids:
                if len(coverage.labels) == len(ctx.wanted):
                    return ids, Rule.ISSUE_NUMERIC if numeric else Rule.ISSUE_RAW
                return ids, Rule.ISSUE_SET
            return ids, Rule.ISSUE_WRONG
        if coverage.kind == CoverageKind.RANGE:
            first, last = (numeric_label(v) for v in coverage.labels)
            if first is None or last is None:
                return None, Rule.NUMBER_UNAVAILABLE
            if first > last:
                return None, Rule.NUMBER_AMBIGUOUS
            if any(numeric_label(ctx.issues[i].raw_number) is None for i in ctx.wanted):
                return None, Rule.NUMBER_UNAVAILABLE
            membership = ctx.numbers.range_members(self.target.publication.id,
                NumericRange(self.target.publication.id, first, last))
            if membership.state == SemanticState.AMBIGUOUS:
                return None, Rule.NUMBER_AMBIGUOUS
            ids = set(membership.issue_ids)
            return ids, Rule.ISSUE_RANGE if ctx.wanted <= ids else Rule.ISSUE_WRONG
        return None, Rule.ISSUE_UNKNOWN

    def coverage(self):
        ctx = self.context
        coverages = {}
        packs = set()
        for o in self.observations:
            coverage = o.coverage
            if o.pack not in (PackKind.UNKNOWN, PackKind.SINGLE):
                packs.add(o.pack)
            if coverage.kind != CoverageKind.UNKNOWN:
                key = (coverage.kind, tuple(sorted(coverage.labels)) if coverage.kind == CoverageKind.SET else coverage.labels)
                coverages.setdefault(key, []).append(self.evidence(o, 'coverage'))
        if self.target.publication.special_version == SpecialVersion.VOLUME_AS_ISSUE:
            value, evidence = self.facts('volume')
            if value is not None:
                labels = tuple(part.strip() for part in value.split('-'))
                if len(labels) in (1, 2) and all(numeric_label(v) is not None for v in labels):
                    kind = CoverageKind.SINGLE if len(labels) == 1 else CoverageKind.RANGE
                    coverages.setdefault((kind, labels), []).extend(evidence)
                    self.add('coverage_context', Rule.VAI, evidence=evidence)
                else:
                    self.add('coverage', Rule.NUMBER_UNAVAILABLE, Outcome.UNAVAILABLE, evidence, State.REVIEW)
        explicit = [(ReleaseCoverage(kind, labels), evidence) for (kind, labels), evidence in coverages.items()
                    if kind in (CoverageKind.SINGLE, CoverageKind.SET, CoverageKind.RANGE)]
        results = [(self.coverage_value(c, e), c, e) for c, e in explicit]
        if len({(frozenset(ids) if ids is not None else None, rule == Rule.ISSUE_WRONG)
                for (ids, rule), _, _ in results}) > 1:
            self.add('coverage', Rule.EVIDENCE_CONFLICT, Outcome.CONFLICT,
                     tuple(e for _, _, refs in results for e in refs), State.REVIEW)
            return
        if self.identity_issues and ctx.wanted <= self.identity_issues:
            if any(ids is None or not ctx.wanted <= ids for (ids, _), _, _ in results):
                self.add('coverage', Rule.EVIDENCE_CONFLICT, Outcome.CONFLICT, gate=State.REVIEW)
            else:
                # A qualified target ID proves membership, not that a release
                # containing additional issues is an exact single-issue release.
                finite_exact = all(rule in (Rule.ISSUE_RAW, Rule.ISSUE_NUMERIC)
                                   for (_, rule), _, _ in results)
                pack_claim = any(kind == CoverageKind.PACK for kind, _ in coverages)
                self.band = Band.EXACT if self.identity_issues == ctx.wanted and finite_exact and not pack_claim else Band.CONTAINING
                self.add('coverage', Rule.ISSUE_ID, evidence=tuple(self.evidence(o, 'identities')
                         for o in self.observations if o.identities))
            return
        collection = self.target.kind == TargetKind.COLLECTION
        special = self.target.publication.special_version in (*_SPECIAL_PHYSICAL, *_SPECIAL_KIND)
        if (collection or special) and not self.sole_numbered:
            if results:
                self.add('coverage', Rule.SPECIAL_WRONG, Outcome.MISMATCH, gate=State.REJECTED)
            elif any(kind == CoverageKind.COLLECTION for kind, _ in coverages) and len(self.target.catalog) == 1:
                self.band = Band.EXACT
                self.add('coverage', Rule.SOLE_SPECIAL)
            else:
                self.add('coverage', Rule.ISSUE_UNKNOWN, Outcome.UNKNOWN, gate=State.REVIEW)
            return
        if results:
            # Same coverage from several tiers is one award, not summed evidence.
            (ids, rule), coverage, evidence = min(results, key=lambda r: (
                r[0][1].value, r[1].kind.value, repr(r[1].labels)))
            if ids is None:
                self.add('coverage', rule, Outcome.UNAVAILABLE, evidence, State.REVIEW)
            elif rule == Rule.ISSUE_WRONG:
                self.add('coverage', rule, Outcome.MISMATCH, evidence, State.REJECTED)
            else:
                self.band = Band.EXACT if rule in (Rule.ISSUE_RAW, Rule.ISSUE_NUMERIC) else Band.CONTAINING
                self.add('coverage', rule, evidence=tuple(e for _, _, refs in results for e in refs))
            return
        if any(kind == CoverageKind.PACK for kind, _ in coverages):
            if self.policy.packs == PackPolicy.FORBID:
                self.add('coverage', Rule.PACK_FORBIDDEN, Outcome.MISMATCH, gate=State.REJECTED)
            elif packs & {PackKind.SERIES, PackKind.VOLUME} and self.policy.packs == PackPolicy.ALLOW:
                self.band = Band.CLAIMED_PACK
                self.add('coverage', Rule.PACK)
            else:
                self.add('coverage', Rule.ISSUE_UNKNOWN, Outcome.UNAVAILABLE, gate=State.REVIEW)
        else:
            self.add('coverage', Rule.ISSUE_UNKNOWN, Outcome.UNKNOWN, gate=State.UNDETERMINED)

    def remaining_gates(self):
        c = self.candidate
        language, evidence = self.facts('language', title_key)
        if language is None or self.target.language is None:
            self.add('language', Rule.LANGUAGE_UNKNOWN, Outcome.UNKNOWN, evidence)
        elif language == title_key(self.target.language):
            self.add('language', Rule.LANGUAGE_MATCH, evidence=evidence)
        else:
            self.add('language', Rule.LANGUAGE_WRONG, Outcome.MISMATCH, evidence, State.REJECTED)
        self.extension, evidence = self.facts('extension', str.lower)
        if self.extension is None:
            self.add('archive', Rule.ARCHIVE_UNKNOWN, Outcome.UNKNOWN, evidence)
        elif self.extension not in FileConstants.CONTAINER_EXTENSIONS:
            self.add('archive', Rule.ARCHIVE_WRONG, Outcome.MISMATCH, evidence, State.REJECTED)
        else:
            self.add('archive', Rule.ARCHIVE_MATCH, evidence=evidence)
        if c.acquisition.kind == LocatorKind.UNAVAILABLE or c.acquisition.mechanism == AcquisitionMechanism.OTHER:
            self.add('acquisition', Rule.ACQUISITION_UNAVAILABLE, Outcome.UNAVAILABLE, gate=State.REJECTED)
        else:
            self.add('acquisition', Rule.ACQUISITION_AVAILABLE)
        self.add('size', Rule.SIZE_ZERO if c.size_bytes == 0 else Rule.SIZE_NEUTRAL,
                 Outcome.MISMATCH if c.size_bytes == 0 else Outcome.NEUTRAL,
                 gate=State.REJECTED if c.size_bytes == 0 else None)
        owned = tuple(self.context.issues[i].owned for i in self.target.issue_ids)
        if all(owned) and self.policy.reject_owned:
            self.add('ownership', Rule.OWNED, Outcome.MISMATCH, gate=State.REJECTED)
        else:
            self.add('ownership', Rule.OWNERSHIP_UNKNOWN, Outcome.UNKNOWN if None in owned else Outcome.NEUTRAL)
        if self.band == Band.CONTAINING and self.policy.packs == PackPolicy.FORBID:
            self.add('pack', Rule.PACK_FORBIDDEN, Outcome.MISMATCH, gate=State.REJECTED)
        for d in sorted(set(c.diagnostics), key=lambda d: (d.code.value, d.field)):
            gate = State.REVIEW if d.code in (ReleaseDiagnosticCode.CONFLICT,
                    ReleaseDiagnosticCode.AMBIGUOUS_COVERAGE) else None
            self.add('diagnostic', Rule.DIAGNOSTIC, Outcome.CONFLICT if gate else Outcome.UNAVAILABLE,
                     (f'diagnostic:{d.code.value}:{d.field}',), gate)

    def run(self):
        self.identities()
        self.series()
        self.year()
        self.volume()
        self.formats()
        self.coverage()
        self.remaining_gates()
        # Group conflicts are still facts; do not silently choose a favored group.
        group, evidence = self.facts('release_group', title_key)
        quality_group, quality_receipt = 0, ''
        if self.policy.quality_context:
            import json

            from backend.base.quality import (ClaimedQuality, QualityError,
                                              classify, compare)
            context = json.loads(self.policy.quality_context)
            try:
                claims = classify(self.candidate.raw_title)
            except QualityError:
                claims = ClaimedQuality(conflict=True, origin='bounded_unavailable')
            quality = compare(context['profile'], claims,
                current=ClaimedQuality(**context['current']) if context.get('current') else None)
            blocked = context.get('conflict') or quality['result'] in ('not_allowed', 'equal', 'downgrade')
            quality_group = quality['group'] or 0
            quality_receipt = json.dumps(quality, sort_keys=True)
            self.add('quality', Rule.QUALITY, Outcome.MISMATCH if blocked else Outcome.MATCH,
                     gate=State.REJECTED if blocked else None)
        gates = {c.gate for c in self.components}
        state = next((s for s in (State.REJECTED, State.REVIEW, State.UNDETERMINED) if s in gates), State.COMPATIBLE)
        priority = 0
        if state == State.COMPATIBLE:
            if self.band in (Band.CONTAINING, Band.CLAIMED_PACK) and self.target.kind == TargetKind.ISSUES:
                self.add('pack', Rule.PACK_PENALTY, Outcome.NEUTRAL,
                         points=-40 if self.band == Band.CLAIMED_PACK else -20)
            if self.extension in self.policy.archive_order:
                self.add('archive_preference', Rule.ARCHIVE_PREFERENCE,
                         points=10 - self.policy.archive_order.index(self.extension))
            if group in {title_key(v) for v in self.policy.preferred_groups}:
                self.add('group', Rule.GROUP_PREFERENCE, evidence=evidence, points=5)
            priority = next((p.priority for p in self.policy.source_priorities
                             if (p.kind, p.key) == (self.candidate.source.kind, self.candidate.source.key)), 0)
            self.add('source', Rule.SOURCE_PREFERENCE, Outcome.NEUTRAL,
                     evidence=(f'{self.candidate.source.kind.value}:{self.candidate.source.key}',))
            score = sum(c.points for c in self.components)
        else:
            self.components = [replace(c, points=0) for c in self.components]
            score = None
        return ReleaseEvaluation(self.target, self.candidate, state, self.band, score,
            tuple(self.components), priority, self.policy.policy_id, self.policy.fingerprint,
            quality_group, quality_receipt)


def evaluate_release(target: WantedTarget, candidate: ReleaseCandidate,
                     policy: ScoringPolicy = ScoringPolicy()) -> ReleaseEvaluation:
    return _Evaluation(_TargetContext(target), candidate, policy).run()


def evaluate_releases(target: WantedTarget, candidates: Iterable[ReleaseCandidate],
                      policy: ScoringPolicy = ScoringPolicy()) -> Tuple[ReleaseEvaluation, ...]:
    context = _TargetContext(target)
    return tuple(_Evaluation(context, c, policy).run() for c in candidates)


def ranking_key(e: ReleaseEvaluation) -> tuple:
    """The unchanged v1 ordering tuple, shared by sorting and its receipt."""
    state = (State.COMPATIBLE, State.REVIEW, State.UNDETERMINED, State.REJECTED).index(e.state)
    band = (Band.EXACT, Band.CONTAINING, Band.CLAIMED_PACK, Band.UNKNOWN).index(e.band)
    canonical_candidate = replace(e.candidate,
        observations=tuple(sorted(set(e.candidate.observations), key=repr)),
        diagnostics=tuple(sorted(set(e.candidate.diagnostics), key=repr)))
    semantic = dumps(preview_release(canonical_candidate), sort_keys=True, ensure_ascii=False)
    return (state, band if e.state == State.COMPATIBLE else 0,
            -e.quality_group if e.state == State.COMPATIBLE else 0, -(e.score or 0), -e.source_priority,
            e.candidate.source.kind.value, e.candidate.source.key,
            e.candidate.candidate_id or '', semantic)


def _same_ranking_scope(values) -> None:
    if values and any(e.target != values[0].target or e.policy_fingerprint != values[0].policy_fingerprint for e in values):
        raise ValueError('Rank only evaluations for the same target and policy')


def compare_evaluations(left: ReleaseEvaluation, right: ReleaseEvaluation) -> RankingComparison:
    """Describe the first differing tier of the existing comparator, no selection."""
    _same_ranking_scope((left, right))
    for tier, a, b in zip(RankingTier, ranking_key(left), ranking_key(right)):
        if a != b:
            return RankingComparison(-1 if a < b else 1, tier)
    return RankingComparison(0, None)


def rank_evaluations(evaluations: Iterable[ReleaseEvaluation]) -> Tuple[ReleaseEvaluation, ...]:
    """Presentation ranking only. Retain rejected results; never select or grab."""
    values = tuple(evaluations)
    _same_ranking_scope(values)
    return tuple(sorted(values, key=ranking_key))


def preview_evaluation(evaluation: ReleaseEvaluation) -> dict:
    """Machine-readable Phase 5C input, not prose, HTML or grab permission."""
    return {
        'candidate': preview_release(evaluation.candidate),
        'target': {'volume_id': evaluation.target.publication.id,
                   'issue_ids': list(evaluation.target.issue_ids),
                   'kind': evaluation.target.kind.value},
        'state': evaluation.state.value, 'band': evaluation.band.value,
        'score': evaluation.score, 'source_priority': evaluation.source_priority,
        'rejections': [r.value for r in evaluation.rejections],
        'components': [{'axis': c.axis, 'rule': c.rule.value, 'outcome': c.outcome.value,
                        'points': c.points, 'evidence': list(c.evidence),
                        'gate': c.gate.value if c.gate else None} for c in evaluation.components],
        'policy': evaluation.policy_id, 'fingerprint': evaluation.policy_fingerprint,
    }

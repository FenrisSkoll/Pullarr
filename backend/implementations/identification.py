"""Pure, conservative local identification. Acquisition and application live elsewhere."""

from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
from math import isfinite
from re import findall, search
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Optional, Tuple
from unicodedata import normalize

from backend.base.definitions import SpecialVersion
from backend.base.identification import (IdentificationResult, LocalMatchIssue,
                                         LocalMatchVolume, MatchBand,
                                         MatchContribution, MatchReason,
                                         MatchState, PublicationMatch)
from backend.base.import_candidate import (ClaimRole, DiagnosticKind,
                                           ImportCandidate, InspectionState,
                                           ProviderReference, ResourceKind)
from backend.base.issue_facts import (NumberCatalog, NumericRange,
                                      SemanticState, numeric_label)

AUTO_THRESHOLD = 140
AUTO_MARGIN = 30


def title_key(value: str) -> str:
    """No token deletion, article removal or fuzzy equivalence."""
    return ' '.join(normalize('NFC', value).casefold().split())


@dataclass(frozen=True)
class MatchingSnapshot:
    volumes: Mapping[int, LocalMatchVolume]
    issues: Mapping[int, LocalMatchIssue]
    children: Mapping[int, Tuple[LocalMatchIssue, ...]]
    titles: Mapping[str, Tuple[int, ...]]
    identities: Mapping[ProviderReference, Tuple[int, ...]]
    issue_identities: Mapping[ProviderReference, Tuple[int, ...]]
    raw_numbers: Mapping[Tuple[int, str], Tuple[LocalMatchIssue, ...]]
    numeric_numbers: Mapping[Tuple[int, float], Tuple[LocalMatchIssue, ...]]
    snapshot_id: str
    number_catalogs: Mapping[int, NumberCatalog]

    @classmethod
    def build(cls, volumes: Iterable[LocalMatchVolume],
              issues: Iterable[LocalMatchIssue]) -> 'MatchingSnapshot':
        def ref_key(r: ProviderReference) -> Tuple[str, str, str]:
            return r.provider, r.kind.value, r.provider_id
        ordered_v = tuple(sorted((replace(v, references=tuple(sorted(set(v.references), key=ref_key)),
                                          aliases=tuple(sorted(set(v.aliases)))) for v in volumes), key=lambda v: v.id))
        ordered_i = tuple(sorted((replace(i, references=tuple(sorted(set(i.references), key=ref_key)))
                                  for i in issues), key=lambda i: i.id))
        vs = {v.id: v for v in ordered_v}
        ins = {i.id: i for i in ordered_i}
        if len(vs) != len(ordered_v) or len(ins) != len(ordered_i):
            raise ValueError('Duplicate local snapshot identity')
        children: Dict[int, list[LocalMatchIssue]] = {v: [] for v in vs}
        titles: Dict[str, set[int]] = {}
        refs: Dict[ProviderReference, set[int]] = {}
        issue_refs: Dict[ProviderReference, set[int]] = {}
        raw_numbers: Dict[Tuple[int, str], list[LocalMatchIssue]] = {}
        numeric_numbers: Dict[Tuple[int, float], list[LocalMatchIssue]] = {}
        for v in ordered_v:
            if v.id <= 0 or v.authority.kind != ResourceKind.VOLUME:
                raise ValueError('Invalid selected volume identity')
            for title in (v.title,) + v.aliases:
                if title_key(title):
                    titles.setdefault(title_key(title), set()).add(v.id)
            for ref in (v.authority,) + v.references:
                if ref.kind != ResourceKind.VOLUME:
                    raise ValueError('Invalid volume reference kind')
                refs.setdefault(ref, set()).add(v.id)
        for i in ordered_i:
            if (i.id <= 0 or i.volume_id not in vs
                    or i.calculated_number is not None and not isfinite(i.calculated_number)
                    or i.calculated_number is None and i.number_facts is None):
                raise ValueError('Invalid issue parent or numeric storage')
            children[i.volume_id].append(i)
            raw_numbers.setdefault((i.volume_id, i.raw_number), []).append(i)
            if i.calculated_number is not None:
                numeric_numbers.setdefault((i.volume_id, i.calculated_number), []).append(i)
            for ref in i.references:
                if ref.kind != ResourceKind.ISSUE:
                    raise ValueError('Invalid issue reference kind')
                refs.setdefault(ref, set()).add(i.volume_id)
                issue_refs.setdefault(ref, set()).add(i.id)
        # Sorted set projections ensure presentation cannot decide ambiguity.
        return cls(MappingProxyType(vs), MappingProxyType(ins),
                   MappingProxyType({k: tuple(v) for k, v in children.items()}),
                   MappingProxyType({k: tuple(sorted(v)) for k, v in titles.items()}),
                   MappingProxyType({k: tuple(sorted(v)) for k, v in refs.items()}),
                   MappingProxyType({k: tuple(sorted(v)) for k, v in issue_refs.items()}),
                   MappingProxyType({k: tuple(v) for k, v in raw_numbers.items()}),
                   MappingProxyType({k: tuple(v) for k, v in numeric_numbers.items()}),
                   sha256(repr((ordered_v, ordered_i)).encode()).hexdigest(),
                   MappingProxyType({vid: NumberCatalog.build((i.id, i.raw_number, i.number_facts)
                       for i in rows) for vid, rows in children.items()}))


def _claims(candidate: ImportCandidate) -> Tuple[ProviderReference, ...]:
    return tuple(sorted({r for c in candidate.claims if c.role == ClaimRole.EMBEDDED
                         for r in (c.reference, c.parent) if r is not None},
                        key=lambda r: (r.provider, r.kind.value, r.provider_id)))


def _coverage(candidate: ImportCandidate, volume: LocalMatchVolume,
              snapshot: MatchingSnapshot) -> Tuple[Tuple[int, ...], MatchReason]:
    issues = snapshot.children[volume.id]
    refs = [r for r in _claims(candidate) if r.kind == ResourceKind.ISSUE]
    if refs:
        ids: set[int] = set()
        for ref in refs:
            matches = snapshot.issue_identities.get(ref, ())
            if len(matches) != 1:
                return (), MatchReason.ISSUE_AMBIGUOUS if matches else MatchReason.UNKNOWN_IDENTITY
            if snapshot.issues[matches[0]].volume_id != volume.id:
                return (), MatchReason.INVALID_LOCAL
            ids.update(matches)
        return tuple(sorted(ids)), MatchReason.ISSUE_ID
    document = candidate.comicinfo.document
    if document and any(d.field in ('Number',) for d in document.diagnostics):
        return (), MatchReason.NUMBER_UNAVAILABLE
    raw = document.number if document else None
    if raw is not None:
        # An unnumbered marker is not a publication identifier.
        if raw.strip().casefold() in ('', 'nn', '[nn]', 'unnumbered'):
            return (), MatchReason.NUMBER_UNAVAILABLE
        semantic = snapshot.number_catalogs[volume.id].match(raw)
        if semantic.evidence == 'raw' or any(i.calculated_number is None for i in issues):
            if semantic.state == SemanticState.AMBIGUOUS:
                return (), MatchReason.ISSUE_AMBIGUOUS
            if semantic.state == SemanticState.UNSUPPORTED:
                return (), MatchReason.NUMBER_UNAVAILABLE
            if semantic.issue_ids:
                return semantic.issue_ids, (MatchReason.ISSUE_RAW if semantic.evidence == 'raw' else MatchReason.ISSUE_NUMERIC)
            return (), MatchReason.ISSUE_MISSING
        number = numeric_label(raw)
        if number is None:
            return (), MatchReason.NUMBER_UNAVAILABLE
        projection = float(number)
        if not isfinite(projection) or Decimal.from_float(projection) != number:
            return (), MatchReason.NUMBER_UNAVAILABLE
        matches = snapshot.numeric_numbers.get((volume.id, projection), ())
        if len(matches) > 1:
            return (), MatchReason.ISSUE_AMBIGUOUS
        if matches and numeric_label(matches[0].raw_number) == number:
            return (matches[0].id,), MatchReason.ISSUE_NUMERIC
        return (), MatchReason.ISSUE_MISSING
    filename = candidate.filename
    number_or_range = filename.legacy_issue_number if filename else None
    if filename and volume.special_version == SpecialVersion.VOLUME_AS_ISSUE:
        number_or_range = filename.volume_number
    if number_or_range is not None:
        endpoints = number_or_range if isinstance(number_or_range, tuple) else (number_or_range, number_or_range)
        low, high = endpoints
        if not isfinite(low) or not isfinite(high) or low > high:
            return (), MatchReason.NUMBER_UNAVAILABLE
        if filename and (search(r'\b[0-9]+[A-Za-z]+\b', filename.raw_stem)
                         or '[nn]' in filename.raw_stem.casefold()):
            return (), MatchReason.NUMBER_UNAVAILABLE
        matches = (snapshot.numeric_numbers.get((volume.id, low), ()) if low == high else
                   tuple(i for i in issues if i.calculated_number is not None and low <= i.calculated_number <= high))
        if len({i.calculated_number for i in matches}) != len(matches):
            return (), MatchReason.ISSUE_AMBIGUOUS
        # A parser float alone is not proof of decimal syntax. Require literal
        # decimal tokens in the original stem (not alphabet/marker projections).
        tokens = {Decimal(t) for t in findall(
            r'(?<![\w.])[0-9]+(?:\.[0-9]+)?(?![\w.])', filename.raw_stem if filename else '')}
        if not {Decimal.from_float(float(low)), Decimal.from_float(float(high))}.issubset(tokens):
            return (), MatchReason.NUMBER_UNAVAILABLE
        if any(i.calculated_number is None for i in issues):
            catalog = snapshot.number_catalogs[volume.id]
            a, b = Decimal.from_float(float(low)), Decimal.from_float(float(high))
            members = catalog.range_members(volume.id, NumericRange(volume.id, a, b))
            if members.state == SemanticState.AMBIGUOUS:
                return (), MatchReason.ISSUE_AMBIGUOUS
            if a not in catalog.numeric or b not in catalog.numeric:
                return (), MatchReason.ISSUE_MISSING
            return members.issue_ids, (
                MatchReason.ISSUE_RANGE if low != high else MatchReason.ISSUE_NUMERIC)
        # Legacy suffix projections must not acquire range membership.
        for issue in matches:
            numeric = numeric_label(issue.raw_number)
            if numeric is None or issue.calculated_number is None or Decimal.from_float(issue.calculated_number) != numeric:
                return (), MatchReason.NUMBER_UNAVAILABLE
        if not matches or not {low, high}.issubset({i.calculated_number for i in matches}):
            return (), MatchReason.ISSUE_MISSING
        return tuple(sorted(i.id for i in matches)), (
            MatchReason.ISSUE_RANGE if low != high else MatchReason.ISSUE_NUMERIC)
    if (filename and filename.special_version == volume.special_version.value
            and volume.special_version in (SpecialVersion.TPB, SpecialVersion.HARD_COVER,
                                           SpecialVersion.OMNIBUS, SpecialVersion.ONE_SHOT)
            and len(issues) == 1):
        return (issues[0].id,), MatchReason.SPECIAL
    return (), MatchReason.ISSUE_MISSING


def identify(candidate: ImportCandidate, snapshot: MatchingSnapshot) -> IdentificationResult:
    """Evaluate only supplied observations and snapshot; no IO or mutation."""
    def result(state: MatchState, selected: Optional[PublicationMatch] = None,
               alternatives: Tuple[PublicationMatch, ...] = (),
               reasons: Tuple[MatchReason, ...] = ()) -> IdentificationResult:
        return IdentificationResult(candidate, state, selected, alternatives, reasons, snapshot.snapshot_id)

    if candidate.file.stat_state == InspectionState.FAILED or any(
        d.kind == DiagnosticKind.FATAL for d in candidate.diagnostics
    ):
        return result(MatchState.BLOCKED, reasons=(MatchReason.DIAGNOSTIC,))
    refs = _claims(candidate)
    associations = candidate.existing.associations if candidate.existing else ()
    if associations:
        volumes = {a.volume_id for a in associations}
        if len(volumes) != 1:
            return result(MatchState.CONFLICTED, reasons=(MatchReason.INVALID_LOCAL,))
        volume = snapshot.volumes.get(next(iter(volumes)))
        if volume is None or any(a.selected_volume != volume.authority for a in associations):
            return result(MatchState.BLOCKED, reasons=(MatchReason.INVALID_LOCAL,))
        ids = tuple(sorted({a.issue_id for a in associations if a.issue_id is not None}))
        if any(i not in snapshot.issues or snapshot.issues[i].volume_id != volume.id for i in ids):
            return result(MatchState.BLOCKED, reasons=(MatchReason.INVALID_LOCAL,))
        if any(a.selected_issue is not None and a.selected_issue not in snapshot.issues[a.issue_id].references
               for a in associations if a.issue_id is not None):
            return result(MatchState.BLOCKED, reasons=(MatchReason.INVALID_LOCAL,))
        reviews = []
        for ref in refs:
            owners = snapshot.identities.get(ref, ())
            if volume.id not in owners:
                same_namespace = ref.kind == ResourceKind.VOLUME and any(
                    r.provider == ref.provider for r in (volume.authority,) + volume.references)
                reviews.append(MatchReason.IDENTITY_CONFLICT if owners or same_namespace else MatchReason.UNKNOWN_IDENTITY)
            if ref.kind == ResourceKind.ISSUE and ids and not set(snapshot.issue_identities.get(ref, ())).issubset(ids):
                reviews.append(MatchReason.IDENTITY_CONFLICT)
        if candidate.conflicts:
            reviews.append(MatchReason.EVIDENCE_CONFLICT)
        selected = PublicationMatch(volume.id, volume.authority, ids, (
            MatchContribution(MatchReason.FORCED if any(a.forced for a in associations)
                              else MatchReason.EXISTING, 'existing.associations'),),
            review_reasons=tuple(dict.fromkeys(reviews)), band=MatchBand.EXACT)
        return result(MatchState.REVIEW if reviews else MatchState.AUTOMATIC,
                      selected, (selected,), selected.review_reasons)

    doc = candidate.comicinfo.document
    filename = candidate.filename
    title = doc.series if doc and doc.series else filename.series if filename else None
    source = 'comicinfo' if doc and doc.series else 'filename'
    year = doc.date.year if doc and doc.date.year else filename.year if filename else None
    year_source = 'comicinfo' if doc and doc.date.year else 'filename'
    ids_set = set(candidate.folder.local_volume_ids)
    if title:
        ids_set.update(snapshot.titles.get(title_key(title), ()))
    for ref in refs:
        ids_set.update(snapshot.identities.get(ref, ()))
    # Hypotheses generate alternatives, never confirmed associations.
    for hypothesis in candidate.alternatives:
        ids_set.update(snapshot.identities.get(hypothesis.volume, ()))
        if hypothesis.local_volume_id is not None:
            ids_set.add(hypothesis.local_volume_id)
    if any(i not in snapshot.volumes for i in ids_set):
        return result(MatchState.BLOCKED, reasons=(MatchReason.INVALID_LOCAL,))
    options = []
    for vid in sorted(ids_set):
        volume = snapshot.volumes[vid]
        contributions = []
        rejects = []
        reviews = []
        exact_identity = False
        for ref in refs:
            owners = snapshot.identities.get(ref, ())
            if vid in owners:
                points = 0 if exact_identity else 200
                exact_identity = True
                contributions.append(MatchContribution(MatchReason.IDENTITY, 'comicinfo.identity', points))
            elif owners:
                rejects.append(MatchReason.IDENTITY_CONFLICT)
            elif ref.kind == ResourceKind.VOLUME and any(
                r.provider == ref.provider for r in (volume.authority,) + volume.references
            ):
                rejects.append(MatchReason.IDENTITY_CONFLICT)
            else:
                reviews.append(MatchReason.UNKNOWN_IDENTITY)
        if title and title_key(title) in {title_key(volume.title), *(title_key(a) for a in volume.aliases)}:
            contributions.append(MatchContribution(
                MatchReason.TITLE if title_key(title) == title_key(volume.title) else MatchReason.ALIAS,
                source + '.series', 80))
        elif title:
            contributions.append(MatchContribution(MatchReason.TITLE_CONFLICT, source + '.series', -20))
            reviews.append(MatchReason.TITLE_CONFLICT)
        if year is not None and volume.year is not None:
            if year == volume.year:
                contributions.append(MatchContribution(MatchReason.YEAR, year_source + '.year', 40))
            else:
                contributions.append(MatchContribution(MatchReason.YEAR_CONFLICT, year_source + '.year', -40))
                reviews.append(MatchReason.YEAR_CONFLICT)
        else:
            contributions.append(MatchContribution(MatchReason.YEAR_UNKNOWN, year_source + '.year'))
        if doc and any(d.field in ('Series', 'Year', 'Month', 'Day', 'Volume') for d in doc.diagnostics):
            reviews.append(MatchReason.EVIDENCE_CONFLICT)
        if doc and doc.publisher and volume.publisher:
            same = title_key(doc.publisher) == title_key(volume.publisher)
            contributions.append(MatchContribution(MatchReason.PUBLISHER if same else MatchReason.PUBLISHER_CONFLICT,
                                                   'comicinfo.publisher', 10 if same else -10))
        if filename and filename.volume_number == volume.volume_number and volume.volume_number is not None:
            contributions.append(MatchContribution(MatchReason.VOLUME, 'filename.volume', 10))
        if vid in candidate.folder.local_volume_ids:
            contributions.append(MatchContribution(MatchReason.FOLDER, 'folder.ownership', 50))
        # Format axes are not interchangeable. A textual disagreement requires
        # review; it is not a new classification rule or provider-format mapping.
        if filename and filename.special_version is not None and volume.special_version != SpecialVersion.VOLUME_AS_ISSUE and filename.special_version != volume.special_version.value:
            reviews.append(MatchReason.SPECIAL_CONFLICT)
        if doc and doc.text('Format'):
            # Retain raw Format in candidate; no newly approved mapping.
            reviews.append(MatchReason.SPECIAL_CONFLICT)
        coverage, coverage_reason = _coverage(candidate, volume, snapshot)
        if coverage_reason == MatchReason.ISSUE_ID and doc and doc.number is not None:
            labels = [snapshot.issues[i].raw_number for i in coverage]
            numeric = numeric_label(doc.number)
            if doc.number not in labels and not (numeric is not None and any(numeric_label(label) == numeric for label in labels)):
                reviews.append(MatchReason.EVIDENCE_CONFLICT)
        contributions.append(MatchContribution(coverage_reason, 'issue.coverage', 20 if coverage else 0))
        if not coverage:
            reviews.append(coverage_reason)
        if coverage_reason == MatchReason.INVALID_LOCAL:
            rejects.append(coverage_reason)
        if candidate.conflicts:
            reviews.append(MatchReason.EVIDENCE_CONFLICT)
        options.append(PublicationMatch(vid, volume.authority, coverage, tuple(contributions),
                                       tuple(dict.fromkeys(rejects)), tuple(dict.fromkeys(reviews)),
                                       MatchBand.EXACT if exact_identity else MatchBand.HIGH
                                       if sum(c.points for c in contributions) >= AUTO_THRESHOLD else MatchBand.LOW))
    ranked = tuple(sorted(options, key=lambda m: (-m.score, m.local_volume_id or 0)))
    allowed = tuple(m for m in ranked if not m.rejections)
    if not allowed:
        return result(MatchState.CONFLICTED if ranked or candidate.conflicts else MatchState.UNRESOLVED,
                      alternatives=ranked, reasons=(MatchReason.IDENTITY_CONFLICT if ranked else
                                                   MatchReason.EVIDENCE_CONFLICT if candidate.conflicts else MatchReason.NO_CANDIDATE,))
    best = allowed[0]
    reasons = list(best.review_reasons)
    if len(allowed) > 1 and best.score - allowed[1].score < AUTO_MARGIN:
        reasons.append(MatchReason.MULTIPLE)
    if best.band == MatchBand.LOW:
        reasons.append(MatchReason.WEAK)
    if reasons:
        return result(MatchState.REVIEW, alternatives=ranked, reasons=tuple(dict.fromkeys(reasons)))
    return result(MatchState.AUTOMATIC, best, ranked)

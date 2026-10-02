"""Exact, provider-neutral correspondence intent; never permission to mutate.

This module has no database, network, classifier or filesystem dependency.
Labels, titles, dates and ordering deliberately are not correspondence inputs.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

POLICY = 'kapowarr-provider-switch/v1'
MAX_ISSUES = 10000


@dataclass(frozen=True, order=True)
class ProviderReference:
    provider: str
    provider_id: str

    def __post_init__(self):
        for value in (self.provider, self.provider_id):
            if (not isinstance(value, str) or not value or len(value) > 128
                    or any(ord(character) < 32 for character in value)):
                raise ValueError('Expected bounded qualified provider identity')


@dataclass(frozen=True)
class StoredIssueIdentity:
    local_id: int
    selected: ProviderReference
    # Exact established identities and their original assertion provenance.
    references: Tuple[Tuple[ProviderReference, str], ...]


@dataclass(frozen=True)
class TargetIssueIdentity:
    reference: ProviderReference
    parent: ProviderReference
    # References reported by this admitted target resource, not title matches.
    assertions: Tuple[Tuple[ProviderReference, str], ...] = ()


class CorrespondenceKind(Enum):
    EXISTING = 'existing_target_identity'
    TARGET_REPORTED = 'target_reported_source_reference'
    MUTUAL = 'mutual_reference'
    OPERATOR = 'operator_confirmed'
    UNRESOLVED = 'unresolved'
    CONFLICT = 'conflict'


@dataclass(frozen=True)
class IssueCorrespondence:
    local_id: int
    source: ProviderReference
    target: Optional[ProviderReference]
    kind: CorrespondenceKind
    evidence: Tuple[str, ...] = ()
    candidates: Tuple[ProviderReference, ...] = ()
    blockers: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrespondencePlan:
    source: ProviderReference
    target: ProviderReference
    issues: Tuple[IssueCorrespondence, ...]
    target_only: Tuple[ProviderReference, ...]
    policy: str = POLICY

    @property
    def ready(self) -> bool:
        """Correspondence completeness only; not application authorization."""
        return all(row.target is not None and not row.blockers for row in self.issues)


def correspondence(
    source: ProviderReference,
    target: ProviderReference,
    local_issues: Tuple[StoredIssueIdentity, ...],
    target_issues: Tuple[TargetIssueIdentity, ...],
    overrides: Optional[Mapping[int, str]] = None,
) -> CorrespondencePlan:
    """Propose exact mappings, retaining conflicts instead of picking a winner.

    Snapshot admission, global identity ownership, dependency checks, metadata
    review and explicit confirmation are separate application preconditions.
    Manual mappings may resolve competing *assertions*, never replace an already
    established target identity. Every unused target is an explicit new issue.
    """
    if source.provider == target.provider:
        raise ValueError('Same-provider identity repair is not provider switching')
    if max(len(local_issues), len(target_issues)) > MAX_ISSUES:
        raise ValueError('Issue correspondence exceeds bounded review scope')
    local = {row.local_id: row for row in local_issues}
    remote = {row.reference.provider_id: row for row in target_issues}
    if (len(local) != len(local_issues) or len(remote) != len(target_issues)
            or any(type(key) is not int or key <= 0 for key in local)):
        raise ValueError('Duplicate or invalid issue identity')
    if any(row.selected.provider != source.provider for row in local_issues):
        raise ValueError('Local issue selected namespace differs from volume')
    if len({row.selected for row in local_issues}) != len(local_issues):
        raise ValueError('Duplicate selected issue identity')
    if any(row.parent != target or row.reference.provider != target.provider for row in target_issues):
        raise ValueError('Target issue does not belong to selected target volume')
    manual = dict(overrides or {})
    if any(type(key) is not int or key not in local or value not in remote for key, value in manual.items()):
        raise ValueError('Manual mapping must select existing local and admitted target IDs')
    if len(set(manual.values())) != len(manual):
        raise ValueError('Manual mappings must be one-to-one')

    # One indexed pass; never compare each source against every target.
    reported = {}
    for row in target_issues:
        for reference, provenance in row.assertions:
            if not provenance:
                raise ValueError('Assertion provenance is required')
            if reference.provider == source.provider:
                reported.setdefault(reference, {})[row.reference] = provenance
    proposals = []
    for key, row in sorted(local.items()):
        identities = {}
        for reference, provenance in row.references:
            if reference.provider in identities or not provenance:
                raise ValueError('Incoherent established issue references')
            identities[reference.provider] = (reference, provenance)
        if identities.get(source.provider, (None,))[0] != row.selected:
            raise ValueError('Selected issue identity missing from established references')
        existing = identities.get(target.provider)
        assertions = reported.get(row.selected, {})
        candidates = tuple(sorted(set(assertions) | ({existing[0]} if existing else set())))
        selected: Optional[ProviderReference] = None
        kind = CorrespondenceKind.UNRESOLVED
        evidence: Tuple[str, ...] = ()
        blockers: Tuple[str, ...] = ()
        if existing:
            reference, provenance = existing
            if reference.provider_id not in remote:
                blockers = ('established_target_identity_absent',)
            elif key in manual and manual[key] != reference.provider_id:
                blockers = ('established_target_identity_conflict',)
            elif any(candidate != reference for candidate in assertions):
                blockers = ('target_assertion_conflicts_with_established_identity',)
            else:
                selected = reference
                kind = CorrespondenceKind.MUTUAL if reference in assertions else CorrespondenceKind.EXISTING
                evidence = (provenance,) + ((assertions[reference],) if reference in assertions else ())
        elif key in manual:
            selected = remote[manual[key]].reference
            kind = CorrespondenceKind.OPERATOR
            evidence = ('operator_confirmed',)
        elif len(assertions) == 1:
            selected = next(iter(assertions))
            kind = CorrespondenceKind.TARGET_REPORTED
            evidence = (assertions[selected],)
        elif assertions:
            blockers = ('ambiguous_target_assertions',)
        else:
            blockers = ('exact_correspondence_required',)
        proposals.append(IssueCorrespondence(key, row.selected, selected,
            CorrespondenceKind.CONFLICT if blockers and candidates else kind,
            evidence, candidates, blockers))

    owners = {}
    for row in proposals:
        if row.target is not None:
            owners.setdefault(row.target, []).append(row.local_id)
    output = []
    for row in proposals:
        if row.target is not None and len(owners[row.target]) != 1:
            row = IssueCorrespondence(row.local_id, row.source, None,
                CorrespondenceKind.CONFLICT, row.evidence, row.candidates or (row.target,),
                ('target_mapped_to_multiple_local_issues',))
        output.append(row)
    used = {row.target for row in output if row.target is not None}
    return CorrespondencePlan(source, target, tuple(output), tuple(
        row.reference for row in target_issues if row.reference not in used))

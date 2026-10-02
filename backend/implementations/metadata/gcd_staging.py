"""GCD staging contracts and historical legacy-admission characterization.

Pure immutable evidence is shared by offline fixtures and the REST adapter.
This module itself performs no transport/persistence. Its legacy DTO admission
is not the richer production snapshot gate and grants no write permission.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from math import isfinite
from typing import Optional, Tuple


class Capability(str, Enum):
    SEARCH_SAFE = 'search_safe'
    DETAIL_SAFE = 'detail_safe'
    NEUTRAL_METADATA_SAFE = 'neutral_metadata_safe'
    DESTRUCTIVE_REFRESH_SAFE = 'destructive_refresh_safe'


@dataclass(frozen=True)
class Admission:
    blockers: Tuple[str, ...]
    capabilities: Tuple[Capability, ...]

    @property
    def neutral_admissible(self) -> bool:
        return Capability.NEUTRAL_METADATA_SAFE in self.capabilities

    @property
    def destructive_complete(self) -> bool:
        return Capability.DESTRUCTIVE_REFRESH_SAFE in self.capabilities


@dataclass(frozen=True)
class GcdIssueNumber:
    raw: Optional[str]

    @property
    def calculated(self) -> Optional[float]:
        # No generic filename parser, suffix encoding, exponent or sentinel.
        if self.raw is None or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', self.raw):
            return None
        exact = Decimal(self.raw)
        value = float(exact)
        return value if isfinite(value) and Decimal.from_float(value) == exact else None

    @property
    def reason(self) -> str:
        return ('exact_decimal' if self.calculated is not None
                else 'unsupported_issue_number')


@dataclass(frozen=True)
class GcdPartialDate:
    raw: Optional[str]

    @property
    def components(self) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        if self.raw is None or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', self.raw):
            return None, None, None
        year, month, day = (int(part) for part in self.raw.split('-'))
        try:
            if not month and day:
                return None, None, None
            date(year, month or 1, day or 1)  # Validation only, never output.
        except ValueError:
            return None, None, None
        return year, month or None, day or None

    @property
    def precision(self) -> str:
        if self.raw in (None, ''):
            return 'absent'
        year, month, day = self.components
        return ('unsupported' if year is None else 'year' if month is None
                else 'month' if day is None else 'day')

    @property
    def complete_date(self) -> Optional[str]:
        return self.raw if self.precision == 'day' else None

    @property
    def reason(self) -> Optional[str]:
        if self.precision in ('day', 'absent'):
            return None
        return ('unsupported_date' if self.precision == 'unsupported'
                else 'partial_date_not_representable')


@dataclass(frozen=True)
class GcdVariantRelation:
    issue_id: str
    base_issue_id: str
    provenance: str


@dataclass(frozen=True)
class GcdIssueSnapshot:
    provider_id: str
    parent_series_id: str
    number: GcdIssueNumber
    title: Optional[str]
    key_date: GcdPartialDate
    on_sale_date: GcdPartialDate
    variant: Optional[GcdVariantRelation] = None
    # Lossless immutable transport payload, including bibliography not mapped
    # here. Not parsed, inherited, logged, persisted or used as identity.
    bibliography_json: str = '{}'


def _identity(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'[1-9][0-9]*', value))


def admit_issue(issue: GcdIssueSnapshot, date_source: str) -> Admission:
    if date_source not in ('key_date', 'on_sale_date'):
        raise ValueError('An explicit GCD date source is required')
    blockers = []
    if not _identity(issue.provider_id) or not _identity(issue.parent_series_id):
        blockers.append('malformed_identity')
    if issue.number.calculated is None:
        blockers.append('unsupported_issue_number')
    selected = issue.key_date if date_source == 'key_date' else issue.on_sale_date
    if selected.reason:
        blockers.append(selected.reason)
    if issue.variant is not None:
        relation = issue.variant
        if (relation.issue_id != issue.provider_id
                or not _identity(relation.base_issue_id)
                or relation.base_issue_id == issue.provider_id
                or not relation.provenance):
            blockers.append('malformed_variant_relation')
        blockers.append('variant_policy_unresolved')
    capabilities = []
    if 'malformed_identity' not in blockers and 'malformed_variant_relation' not in blockers:
        capabilities.append(Capability.DETAIL_SAFE)
    if not blockers:
        capabilities.append(Capability.NEUTRAL_METADATA_SAFE)
    return Admission(tuple(blockers), tuple(capabilities))


@dataclass(frozen=True)
class Acquisition:
    expected_issue_ids: Tuple[str, ...]
    enumeration_exhausted: bool = False
    failures: Tuple[str, ...] = ()
    scope: str = 'all_active_including_variants/v1'
    # A future reader must establish this from a coherent catalog transaction,
    # not set it merely because HTTP requests all succeeded.
    coherent_catalog_snapshot: Optional[str] = None


@dataclass(frozen=True)
class GcdSeriesSnapshot:
    provider_id: str
    title: str
    issues: Tuple[GcdIssueSnapshot, ...]
    acquisition: Acquisition
    bibliography_json: str = '{}'


def admit_series(snapshot: GcdSeriesSnapshot, requested_id: str,
                 date_source: str) -> Admission:
    """Assess a staged issue set, not a production VolumeMetadata conversion.

REST has no proven coherence token; even a fully acquired REST set cannot earn
    DESTRUCTIVE_REFRESH_SAFE here. No caller currently consumes this assessment.
    """
    if date_source not in ('key_date', 'on_sale_date'):
        raise ValueError('An explicit GCD date source is required')
    blockers = []
    capabilities = []
    if _identity(snapshot.provider_id) and snapshot.title.strip():
        capabilities.extend((Capability.SEARCH_SAFE, Capability.DETAIL_SAFE))
    else:
        blockers.append('malformed_series')
    if not _identity(requested_id) or snapshot.provider_id != requested_id:
        blockers.append('wrong_series')
    acquisition = snapshot.acquisition
    if not acquisition.enumeration_exhausted or acquisition.failures:
        blockers.append('incomplete_snapshot')
    if acquisition.scope != 'all_active_including_variants/v1':
        blockers.append('variant_scope_mismatch')
    expected = acquisition.expected_issue_ids
    actual = tuple(issue.provider_id for issue in snapshot.issues)
    if any(not _identity(value) for value in expected):
        blockers.append('malformed_expected_identity')
    if len(set(expected)) != len(expected) or len(set(actual)) != len(actual):
        blockers.append('duplicate_identity')
    if set(expected) != set(actual):
        blockers.append('issue_set_mismatch')
    for issue in snapshot.issues:
        if issue.parent_series_id != snapshot.provider_id:
            blockers.append('wrong_parent')
        blockers.extend(admit_issue(issue, date_source).blockers)
    # Current matching dictionaries cannot distinguish equal numeric keys.
    numbers = [issue.number.calculated for issue in snapshot.issues
               if issue.number.calculated is not None]
    if len(set(numbers)) != len(numbers):
        blockers.append('calculated_number_collision')
    if not blockers:
        capabilities.append(Capability.NEUTRAL_METADATA_SAFE)
        if acquisition.coherent_catalog_snapshot:
            capabilities.append(Capability.DESTRUCTIVE_REFRESH_SAFE)
    if not acquisition.coherent_catalog_snapshot:
        blockers.append('snapshot_coherence_unproven')
    return Admission(tuple(dict.fromkeys(blockers)), tuple(capabilities))

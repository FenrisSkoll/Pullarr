"""Transient, immutable identification results; never organization commands."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from backend.base.definitions import SpecialVersion
from backend.base.import_candidate import ImportCandidate, ProviderReference
from backend.base.issue_facts import IssueNumberFacts

POLICY_ID = 'kapowarr-identification/v1'


class PublicationAuthority(Enum):
    HYPOTHESIS = 'local-target-hypothesis/v1'
    IMPORT_SELECTION = 'library-import-selection/v1'
    MANAGED_VOLUME = 'managed-volume-scan/v1'


class MatchState(Enum):
    AUTOMATIC = 'automatic_match'
    REVIEW = 'review_required'
    UNRESOLVED = 'unresolved'
    CONFLICTED = 'conflicted'
    BLOCKED = 'blocked'


class MatchBand(Enum):
    EXACT = 'exact_identity'
    HIGH = 'corroborated'
    LOW = 'insufficient'


class MatchReason(Enum):
    EXISTING = 'existing_local_identity'
    FORCED = 'explicit_local_association'
    IDENTITY = 'exact_provider_identity'
    UNKNOWN_IDENTITY = 'unproven_provider_relationship'
    IDENTITY_CONFLICT = 'provider_identity_conflict'
    INVALID_LOCAL = 'invalid_local_identity_or_parent'
    TITLE = 'exact_normalized_title'
    TITLE_CONFLICT = 'title_disagreement'
    ALIAS = 'exact_normalized_alias'
    YEAR = 'exact_year'
    YEAR_CONFLICT = 'year_disagreement'
    YEAR_UNKNOWN = 'year_unavailable'
    PUBLISHER = 'publisher_corroboration'
    PUBLISHER_CONFLICT = 'publisher_disagreement'
    VOLUME = 'filename_volume_corroboration'
    FOLDER = 'validated_folder_ownership'
    ISSUE_RAW = 'exact_raw_issue_label'
    ISSUE_NUMERIC = 'safe_legacy_numeric_equality'
    ISSUE_RANGE = 'safe_inclusive_numeric_range'
    ISSUE_ID = 'exact_issue_identity'
    ISSUE_AMBIGUOUS = 'multiple_issue_matches'
    ISSUE_MISSING = 'issue_coverage_unresolved'
    NUMBER_UNAVAILABLE = 'unsupported_number_semantics'
    SPECIAL = 'sole_special_publication'
    SPECIAL_CONFLICT = 'special_version_disagreement'
    EVIDENCE_CONFLICT = 'candidate_evidence_conflict'
    DIAGNOSTIC = 'blocking_candidate_diagnostic'
    MULTIPLE = 'multiple_series_matches_or_close_margin'
    WEAK = 'insufficient_corroboration'
    NO_CANDIDATE = 'no_local_candidate'
    PROVIDER_UNAVAILABLE = 'provider_acquisition_unavailable'
    PROVIDER_RESULT = 'external_publication_requires_explicit_add'


@dataclass(frozen=True)
class MatchContribution:
    reason: MatchReason
    source: str
    points: int = 0


@dataclass(frozen=True)
class LocalMatchIssue:
    id: int
    volume_id: int
    raw_number: str
    calculated_number: Optional[float]
    references: Tuple[ProviderReference, ...] = ()
    # Keep historical repr-based job fingerprints stable for legacy projections.
    number_facts: Optional[IssueNumberFacts] = field(default=None, repr=False)
    title: Optional[str] = field(default=None, repr=False)


@dataclass(frozen=True)
class LocalMatchVolume:
    id: int
    authority: ProviderReference
    title: str
    year: Optional[int]
    volume_number: Optional[int]
    publisher: Optional[str]
    special_version: SpecialVersion = SpecialVersion.NORMAL
    references: Tuple[ProviderReference, ...] = ()
    aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicationMatch:
    local_volume_id: Optional[int]
    provider_identity: ProviderReference
    local_issue_ids: Tuple[int, ...]
    contributions: Tuple[MatchContribution, ...]
    rejections: Tuple[MatchReason, ...] = ()
    review_reasons: Tuple[MatchReason, ...] = ()
    band: MatchBand = MatchBand.LOW
    title: Optional[str] = None
    year: Optional[int] = None

    @property
    def score(self) -> int:
        return sum(c.points for c in self.contributions)


@dataclass(frozen=True)
class IdentificationResult:
    candidate: ImportCandidate
    state: MatchState
    selected: Optional[PublicationMatch]
    alternatives: Tuple[PublicationMatch, ...]
    reasons: Tuple[MatchReason, ...]
    snapshot_id: str
    policy_id: str = POLICY_ID
    acquisition_failure: Optional[str] = None


@dataclass(frozen=True)
class BulkIdentification:
    results: Tuple[IdentificationResult, ...]

    @property
    def counts(self) -> Tuple[Tuple[str, int], ...]:
        return tuple((state.value, sum(r.state == state for r in self.results))
                     for state in MatchState)

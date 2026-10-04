"""Immutable transient release evaluation contracts. No search or grab authority."""

from dataclasses import dataclass, field, fields, replace
from enum import Enum
from hashlib import sha256
from typing import Optional, Tuple

from backend.base.definitions import FileConstants, SpecialVersion
from backend.base.identification import LocalMatchVolume
from backend.base.import_candidate import ProviderReference, ResourceKind
from backend.base.issue_facts import IssueNumberFacts
from backend.base.release_candidate import ReleaseCandidate, SourceKind

SCORING_POLICY = 'kapowarr-release-scoring/v1'


def _is_type(value: object, expected: type) -> bool:
    """Validate runtime callers as well as statically typed internal callers."""
    return isinstance(value, expected)


class TargetKind(Enum):
    ISSUES = 'issues'
    WHOLE_VOLUME = 'whole_volume'
    COLLECTION = 'collection'


class Compatibility(Enum):
    COMPATIBLE = 'compatible'
    REVIEW = 'review_required'
    UNDETERMINED = 'undetermined'
    REJECTED = 'rejected'


class CoverageBand(Enum):
    EXACT = 'exact'
    CONTAINING = 'containing'
    CLAIMED_PACK = 'claimed_pack'
    UNKNOWN = 'unknown'


class RankingTier(Enum):
    STATE = 'compatibility'
    COVERAGE = 'coverage'
    QUALITY = 'quality_group'
    SCORE = 'score'
    SOURCE_PRIORITY = 'source_priority'
    SOURCE_KIND = 'source_kind'
    SOURCE_KEY = 'source_key'
    CANDIDATE_ID = 'candidate_identity'
    SEMANTIC = 'semantic_presentation'


@dataclass(frozen=True)
class RankingComparison:
    """Existing comparator receipt: -1 before, 0 equivalent, +1 after."""

    order: int
    tier: Optional[RankingTier]


class PackPolicy(Enum):
    REVIEW = 'review_unverified_membership'
    ALLOW = 'allow_claimed_complete_pack'
    FORBID = 'forbid_surplus_coverage'


class RuleOutcome(Enum):
    MATCH = 'match'
    MISMATCH = 'mismatch'
    UNKNOWN = 'unknown'
    UNAVAILABLE = 'unavailable'
    CONFLICT = 'conflict'
    NEUTRAL = 'neutral'


class Rule(Enum):
    QUALITY = 'quality_profile'
    SERIES_ID = 'series_identity_exact'
    IDENTITY_CONFLICT = 'qualified_identity_conflict'
    IDENTITY_UNKNOWN = 'identity_relationship_unproven'
    SERIES_EXACT = 'series_exact'
    SERIES_NORMALIZED = 'series_normalized'
    SERIES_ALIAS = 'series_alias'
    SERIES_WRONG = 'series_incompatible'
    SERIES_UNKNOWN = 'series_unknown'
    EVIDENCE_CONFLICT = 'conflicting_observations'
    YEAR_MATCH = 'year_match'
    YEAR_WRONG = 'year_incompatible'
    YEAR_UNKNOWN = 'year_unknown'
    YEAR_UNTYPED = 'year_semantics_unavailable'
    VOLUME_MATCH = 'volume_match'
    VOLUME_WRONG = 'volume_incompatible'
    VOLUME_UNKNOWN = 'volume_semantics_unavailable'
    ISSUE_ID = 'issue_identity_exact'
    ISSUE_RAW = 'issue_raw_exact'
    ISSUE_NUMERIC = 'issue_numeric_equal'
    ISSUE_SET = 'issue_set_contains'
    ISSUE_RANGE = 'issue_range_contains'
    ISSUE_WRONG = 'issue_not_covered'
    ISSUE_UNKNOWN = 'issue_coverage_unknown'
    NUMBER_UNAVAILABLE = 'number_semantics_unavailable'
    NUMBER_AMBIGUOUS = 'issue_identity_ambiguous'
    SPECIAL_MATCH = 'special_publication_match'
    SPECIAL_WRONG = 'special_publication_incompatible'
    SPECIAL_UNKNOWN = 'special_publication_unavailable'
    SOLE_SPECIAL = 'sole_special_numbered_release'
    VAI = 'volume_as_issue_coverage'
    PACK = 'complete_pack_claim'
    PACK_FORBIDDEN = 'surplus_coverage_forbidden'
    PACK_PENALTY = 'surplus_coverage_penalty'
    LANGUAGE_MATCH = 'language_match'
    LANGUAGE_WRONG = 'language_incompatible'
    LANGUAGE_UNKNOWN = 'language_unknown'
    ARCHIVE_MATCH = 'archive_supported'
    ARCHIVE_WRONG = 'archive_unsupported'
    ARCHIVE_UNKNOWN = 'archive_unknown'
    ARCHIVE_PREFERENCE = 'archive_preference'
    GROUP_PREFERENCE = 'release_group_preference'
    SOURCE_PREFERENCE = 'source_priority_tiebreak'
    SIZE_ZERO = 'empty_release'
    SIZE_NEUTRAL = 'size_neutral'
    ACQUISITION_UNAVAILABLE = 'acquisition_unavailable'
    ACQUISITION_AVAILABLE = 'acquisition_described'
    OWNED = 'target_already_owned'
    OWNERSHIP_UNKNOWN = 'ownership_unknown'
    DIAGNOSTIC = 'candidate_diagnostic'


@dataclass(frozen=True)
class WantedIssue:
    id: int
    raw_number: str
    references: Tuple[ProviderReference, ...] = ()
    year: Optional[int] = None
    owned: Optional[bool] = None
    number_facts: Optional[IssueNumberFacts] = field(default=None, repr=False)
    title: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id <= 0 or not _is_type(self.raw_number, str):
            raise ValueError('Stable local issue identity and raw label required')
        if not _is_type(self.references, tuple) or any(
            not _is_type(r, ProviderReference) or r.kind != ResourceKind.ISSUE
            for r in self.references
        ):
            raise ValueError('Qualified immutable issue references required')
        if self.year is not None and (type(self.year) is not int or not 1 <= self.year <= 9999):
            raise ValueError('Invalid issue year')
        if self.owned is not None and type(self.owned) is not bool:
            raise ValueError('Ownership must be known boolean or unavailable')
        object.__setattr__(self, 'references', tuple(sorted(set(self.references),
                           key=lambda r: (r.provider, r.provider_id))))


@dataclass(frozen=True)
class WantedTarget:
    publication: LocalMatchVolume
    catalog: Tuple[WantedIssue, ...]
    issue_ids: Tuple[int, ...]
    kind: TargetKind = TargetKind.ISSUES
    language: Optional[str] = None
    physical_format: Optional[str] = None
    publication_kind: Optional[str] = None

    def __post_init__(self) -> None:
        p = self.publication
        if not _is_type(p, LocalMatchVolume) or p.id <= 0 or not p.title.strip():
            raise ValueError('Canonical local publication required')
        if p.authority.kind != ResourceKind.VOLUME or not isinstance(p.special_version, SpecialVersion):
            raise ValueError('Selected volume authority/classification required')
        if not _is_type(p.references, tuple) or not _is_type(p.aliases, tuple):
            raise ValueError('Publication inputs must be immutable')
        if any(r.kind != ResourceKind.VOLUME for r in p.references):
            raise ValueError('Invalid volume reference')
        if any(not _is_type(a, str) or not a.strip() for a in p.aliases):
            raise ValueError('Aliases must be explicit nonempty strings')
        object.__setattr__(self, 'publication', replace(p,
            references=tuple(sorted(set(p.references), key=lambda r: (r.provider, r.provider_id))),
            aliases=tuple(sorted(set(p.aliases)))))
        if not _is_type(self.catalog, tuple) or not _is_type(self.issue_ids, tuple):
            raise ValueError('Immutable target coverage required')
        if not _is_type(self.kind, TargetKind) or any(not _is_type(i, WantedIssue) for i in self.catalog):
            raise ValueError('Typed target inputs required')
        ids = {i.id for i in self.catalog}
        if len(ids) != len(self.catalog) or not set(self.issue_ids) <= ids:
            raise ValueError('Duplicate catalog or unknown target issue')
        if not self.issue_ids or len(set(self.issue_ids)) != len(self.issue_ids):
            raise ValueError('Explicit nonempty unique target membership required')
        if any(type(i) is not int or i <= 0 for i in self.issue_ids):
            raise ValueError('Local issue IDs must be positive integers')
        if self.kind == TargetKind.WHOLE_VOLUME and set(self.issue_ids) != ids:
            raise ValueError('Whole-volume target must include the supplied catalog')
        if self.kind == TargetKind.COLLECTION and len(self.issue_ids) != 1:
            raise ValueError('Collection is a publication, not invented contained issues')
        for value in (self.language, self.physical_format, self.publication_kind):
            if value is not None and (not _is_type(value, str) or not value.strip()):
                raise ValueError('Optional expectations must be nonempty text')
        object.__setattr__(self, 'catalog', tuple(sorted(self.catalog, key=lambda i: i.id)))
        object.__setattr__(self, 'issue_ids', tuple(sorted(self.issue_ids)))


@dataclass(frozen=True)
class SourcePriority:
    kind: SourceKind
    key: str
    priority: int

    def __post_init__(self) -> None:
        if (not _is_type(self.kind, SourceKind) or not _is_type(self.key, str)
                or not self.key.strip() or type(self.priority) is not int):
            raise ValueError('Typed source preference required')


@dataclass(frozen=True)
class ScoringPolicy:
    """Fixed v1 weights; modest explicit preferences, no mutable global settings."""

    packs: PackPolicy = PackPolicy.REVIEW
    archive_order: Tuple[str, ...] = ()
    preferred_groups: Tuple[str, ...] = ()
    source_priorities: Tuple[SourcePriority, ...] = ()
    reject_owned: bool = True
    policy_id: str = SCORING_POLICY
    quality_context: str = ''

    def __post_init__(self) -> None:
        if self.policy_id != SCORING_POLICY or not isinstance(self.packs, PackPolicy):
            raise ValueError('Unsupported scoring policy')
        if type(self.reject_owned) is not bool:
            raise ValueError('Ownership policy must be explicit')
        for value in (self.archive_order, self.preferred_groups, self.source_priorities):
            if not isinstance(value, tuple):
                raise ValueError('Immutable preferences required')
        if any(not _is_type(v, str) or v not in FileConstants.CONTAINER_EXTENSIONS for v in self.archive_order):
            raise ValueError('Use canonical archive extensions')
        if len(set(self.archive_order)) != len(self.archive_order) or len(self.archive_order) > 10:
            raise ValueError('Duplicate or oversized archive preference')
        if any(not _is_type(v, str) or not v.strip() for v in self.preferred_groups):
            raise ValueError('Invalid release group preference')
        if any(not isinstance(v, SourcePriority) for v in self.source_priorities):
            raise ValueError('Invalid source preferences')
        keys = {(v.kind, v.key) for v in self.source_priorities}
        if len(keys) != len(self.source_priorities):
            raise ValueError('Duplicate source preference')
        object.__setattr__(self, 'source_priorities', tuple(sorted(self.source_priorities,
                           key=lambda v: (v.kind.value, v.key))))
        object.__setattr__(self, 'preferred_groups', tuple(sorted(set(self.preferred_groups))))

    @property
    def fingerprint(self) -> str:
        return sha256(repr(tuple((f.name, getattr(self, f.name)) for f in fields(self)
            if f.name != 'quality_context' or self.quality_context)).encode()).hexdigest()


@dataclass(frozen=True)
class ScoreComponent:
    axis: str
    rule: Rule
    outcome: RuleOutcome
    points: int = 0
    evidence: Tuple[str, ...] = ()
    gate: Optional[Compatibility] = None


@dataclass(frozen=True)
class ReleaseEvaluation:
    target: WantedTarget
    candidate: ReleaseCandidate
    state: Compatibility
    band: CoverageBand
    score: Optional[int]
    components: Tuple[ScoreComponent, ...]
    source_priority: int
    policy_id: str
    policy_fingerprint: str
    quality_group: int = 0
    quality_receipt: str = ''

    @property
    def rejections(self) -> Tuple[Rule, ...]:
        return tuple(dict.fromkeys(c.rule for c in self.components if c.gate == Compatibility.REJECTED))

"""Transient organizer evidence. No IO, persistence, matching or planning.

Legacy numeric parser observations are compatibility projections, not canonical
issue numbers. Raw bibliography never passes through that parser here.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from os.path import basename, splitext
from typing import Optional, Tuple, Union

from backend.base.comicinfo import ComicInfoDiagnostic, ComicInfoDocument
from backend.base.definitions import FilenameData


class EvidenceSource(Enum):
    FILESYSTEM = 'filesystem'
    LEGACY_PARSER = 'legacy_parser'
    FOLDER = 'folder'
    DATABASE = 'database'
    MANUAL = 'manual'
    COMICINFO = 'comicinfo'
    PROVIDER_RESULT = 'provider_result'


@dataclass(frozen=True)
class Provenance:
    source: EvidenceSource
    locator: str
    policy: Optional[str] = None
    observed_at: Optional[datetime] = None


class InspectionState(Enum):
    NOT_INSPECTED = 'not_inspected'
    PRESENT = 'present'
    ABSENT = 'absent'
    FAILED = 'failed'


class ResourceKind(Enum):
    VOLUME = 'volume'
    ISSUE = 'issue'


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class ProviderReference:
    provider: str
    kind: ResourceKind
    provider_id: str

    def __post_init__(self) -> None:
        if not _nonempty_string(self.provider):
            raise ValueError('Provider namespace required')
        if not _nonempty_string(self.provider_id):
            raise ValueError('Opaque string provider ID required')


class ClaimRole(Enum):
    CROSS_REFERENCE = 'cross_reference'
    EMBEDDED = 'embedded'
    SEARCH_RESULT = 'search_result'


@dataclass(frozen=True)
class ProviderIdentityClaim:
    reference: ProviderReference
    role: ClaimRole
    provenance: Provenance
    parent: Optional[ProviderReference] = None

    def __post_init__(self) -> None:
        if self.parent is not None and (
            self.reference.kind != ResourceKind.ISSUE
            or self.parent.kind != ResourceKind.VOLUME
            or self.reference.provider != self.parent.provider
        ):
            raise ValueError('Issue parent must be a volume in the same namespace')


class DiscoveryKind(Enum):
    LIBRARY_SCAN = 'library_scan'
    MANUAL_PATH = 'manual_path'


@dataclass(frozen=True)
class DiscoveryScope:
    run_id: str
    root: Optional[str]
    manual_only: bool = True
    kind: DiscoveryKind = DiscoveryKind.MANUAL_PATH


@dataclass(frozen=True)
class FileObservation:
    path: str
    observed_at: datetime
    size: Optional[int]
    mtime_ns: Optional[int]
    stat_state: InspectionState

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError('Observed path required')
        if self.stat_state == InspectionState.PRESENT:
            if self.size is None or self.mtime_ns is None:
                raise ValueError('Successful stat requires size and mtime')
        elif self.size is not None or self.mtime_ns is not None:
            raise ValueError('Unavailable stat must not invent freshness facts')

    @property
    def raw_name(self) -> str:
        return basename(self.path)

    @property
    def raw_stem(self) -> str:
        return splitext(self.raw_name)[0]

    @property
    def extension(self) -> str:
        return splitext(self.raw_name)[1]


@dataclass(frozen=True)
class FolderObservation:
    parent: str
    relative_path: Optional[str]
    at_root: Optional[bool]
    # Ownership must be supplied from validated local state, not a folder name.
    local_volume_ids: Tuple[int, ...] = ()
    provenance: Optional[Provenance] = None

    def __post_init__(self) -> None:
        if self.local_volume_ids and (
            self.provenance is None or self.provenance.source not in (
                EvidenceSource.DATABASE, EvidenceSource.MANUAL)
        ):
            raise ValueError('Folder ownership requires explicit local provenance')


@dataclass(frozen=True)
class FilenameObservation:
    raw_name: str
    raw_stem: str
    extension: str
    series: str
    year: Optional[int]
    volume_number: Union[int, Tuple[int, int], None]
    special_version: Optional[str]
    legacy_issue_number: Union[float, Tuple[float, float], None]
    annual: bool
    provenance: Provenance

    @property
    def number_semantics(self) -> 'NumberSemantics':
        return NumberSemantics.LEGACY_PROJECTION

    def to_legacy(self) -> FilenameData:
        """Fresh transitional DTO; mutating it cannot alter this observation."""
        return FilenameData(
            series=self.series, year=self.year, volume_number=self.volume_number,
            special_version=self.special_version,
            issue_number=self.legacy_issue_number, annual=self.annual)


@dataclass(frozen=True)
class LocalAssociation:
    volume_id: int
    issue_id: Optional[int]
    forced: bool
    selected_volume: ProviderReference
    provenance: Provenance
    volume_title: Optional[str] = None
    issue_number: Optional[str] = None
    selected_issue: Optional[ProviderReference] = None

    def __post_init__(self) -> None:
        if self.volume_id <= 0 or (self.issue_id is not None and self.issue_id <= 0):
            raise ValueError('Local IDs must be positive')
        if self.selected_volume.kind != ResourceKind.VOLUME:
            raise ValueError('Selected volume identity required')
        if self.selected_issue is not None and (
            self.issue_id is None or self.selected_issue.kind != ResourceKind.ISSUE
            or self.selected_issue.provider != self.selected_volume.provider
        ):
            raise ValueError('Selected issue must share local volume authority')


@dataclass(frozen=True)
class ExistingFileIdentity:
    file_id: int
    associations: Tuple[LocalAssociation, ...]
    references: Tuple[ProviderIdentityClaim, ...] = ()

    def __post_init__(self) -> None:
        if self.file_id <= 0:
            raise ValueError('Local file ID must be positive')


class NumberSemantics(Enum):
    UNINTERPRETED = 'uninterpreted_numeric_and_range_operations_unavailable'
    LEGACY_PROJECTION = 'legacy_projection_only'


@dataclass(frozen=True)
class BibliographicObservation:
    """Literal values, NOT parsed numbers or full-date projections.

    Unknown/opaque/partial values are representable without claiming numeric
    matching or exact-day capability. Those operations are deliberately absent.
    """

    raw_number: Optional[str]
    raw_date: Optional[str]
    provenance: Provenance

    @property
    def number_semantics(self) -> NumberSemantics:
        return NumberSemantics.UNINTERPRETED


@dataclass(frozen=True)
class ComicInfoObservation:
    state: InspectionState = InspectionState.NOT_INSPECTED
    # Ordered name/value observations permit duplicates; bytes preserve XML
    # structure/unknown fields. No XML interpretation/writer in this phase.
    fields: Tuple[Tuple[str, Optional[str]], ...] = ()
    raw_bytes: Optional[bytes] = None
    provenance: Optional[Provenance] = None
    document: Optional[ComicInfoDocument] = None
    diagnostics: Tuple[ComicInfoDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.state != InspectionState.PRESENT and (self.fields or self.document is not None):
            raise ValueError('Uninspected/absent/failed metadata cannot have content')
        if self.state in (InspectionState.NOT_INSPECTED, InspectionState.ABSENT) and self.raw_bytes is not None:
            raise ValueError('Uninspected/absent metadata cannot have source bytes')
        if self.state != InspectionState.NOT_INSPECTED and self.provenance is None:
            raise ValueError('Inspection requires provenance')


class CoverageKind(Enum):
    UNKNOWN = 'unknown'
    SINGLE = 'single'
    SET = 'set'
    LEGACY_RANGE = 'legacy_range'
    COLLECTED = 'collected'


@dataclass(frozen=True)
class CoverageHypothesis:
    kind: CoverageKind
    provenance: Provenance
    local_issue_ids: Tuple[int, ...] = ()
    raw_labels: Tuple[str, ...] = ()
    legacy_endpoints: Optional[Tuple[float, float]] = None

    def __post_init__(self) -> None:
        if (self.legacy_endpoints is not None) != (self.kind == CoverageKind.LEGACY_RANGE):
            raise ValueError('Only a legacy range carries legacy endpoints')


@dataclass(frozen=True)
class MatchHypothesis:
    volume: ProviderReference
    provenance: Provenance
    coverage: CoverageHypothesis
    local_volume_id: Optional[int] = None
    reasons: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.volume.kind != ResourceKind.VOLUME:
            raise ValueError('Series hypothesis requires a volume reference')


class DiagnosticKind(Enum):
    WARNING = 'warning'
    CONFLICT = 'conflict'
    UNAVAILABLE = 'unavailable'
    FATAL = 'fatal'


class DiagnosticCode(Enum):
    COMICINFO_FAILED = 'comicinfo_failed'
    COMICINFO_FIELD = 'comicinfo_field'
    STAT_FAILED = 'stat_failed'
    PATH_MISSING = 'path_missing'
    PATH_UNREADABLE = 'path_unreadable'
    PATH_NOT_FILE = 'path_not_file'
    BIBLIOGRAPHIC_DISAGREEMENT = 'bibliographic_disagreement'
    IDENTITY_DISAGREEMENT = 'identity_disagreement'
    UNVERIFIED_IDENTITY_RELATION = 'unverified_identity_relation'
    MULTIPLE_LOCAL_VOLUMES = 'multiple_local_volumes'
    NUMERIC_SEMANTICS_UNAVAILABLE = 'numeric_semantics_unavailable'
    RANGE_SEMANTICS_UNAVAILABLE = 'range_semantics_unavailable'
    PARSE_FAILED = 'parse_failed'


@dataclass(frozen=True)
class CandidateDiagnostic:
    kind: DiagnosticKind
    code: DiagnosticCode
    provenance: Provenance


class IdentificationState(Enum):
    UNIDENTIFIED = 'unidentified'
    IDENTIFIED = 'identified'
    AMBIGUOUS = 'ambiguous'
    CONFLICTED = 'conflicted'


class ReviewState(Enum):
    CONTINUE = 'continue_evidence_collection'
    REQUIRED = 'review_required'
    BLOCKED = 'blocked'


def _require_immutable(value: object) -> None:
    """Reject mutable containers even when an untyped caller ignores annotations."""
    if isinstance(value, (list, dict, set, bytearray)):
        raise TypeError('Candidate evidence requires immutable containers')
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _require_immutable(getattr(value, field.name))
    elif isinstance(value, tuple):
        for member in value:
            _require_immutable(member)


@dataclass(frozen=True)
class ImportCandidate:
    candidate_id: str
    scope: DiscoveryScope
    file: FileObservation
    folder: FolderObservation
    filename: Optional[FilenameObservation]
    existing: Optional[ExistingFileIdentity] = None
    claims: Tuple[ProviderIdentityClaim, ...] = ()
    bibliography: Tuple[BibliographicObservation, ...] = ()
    comicinfo: ComicInfoObservation = ComicInfoObservation()
    archive_state: InspectionState = InspectionState.NOT_INSPECTED
    coverage: Tuple[CoverageHypothesis, ...] = ()
    alternatives: Tuple[MatchHypothesis, ...] = ()
    diagnostics: Tuple[CandidateDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.scope.run_id:
            raise ValueError('Run and candidate identity required')
        _require_immutable(self)

    @property
    def conflicts(self) -> Tuple[CandidateDiagnostic, ...]:
        """Conservative claim coherence, not identity matching or resolution."""
        result = [d for d in self.diagnostics if d.kind == DiagnosticKind.CONFLICT]
        associations = self.existing.associations if self.existing else ()
        known = {a.selected_volume for a in associations}
        if len({a.volume_id for a in associations}) > 1:
            result.append(CandidateDiagnostic(
                DiagnosticKind.CONFLICT, DiagnosticCode.MULTIPLE_LOCAL_VOLUMES,
                Provenance(EvidenceSource.DATABASE, 'issues_files/volume_files')))
        # Only DB-loaded references can corroborate an embedded cross-provider
        # claim. An unverified claim labelling itself a reference is not proof.
        if self.existing:
            known.update(c.reference for c in self.existing.references)
        embedded = [c for c in self.claims if c.role == ClaimRole.EMBEDDED]
        embedded += [ProviderIdentityClaim(c.parent, ClaimRole.EMBEDDED, c.provenance)
                     for c in tuple(embedded) if c.parent is not None]
        # Several issue IDs can legitimately describe coverage of one file.
        # Only contradictory volume claims are automatically diagnosed here;
        # issue correspondence requires the later identity/coverage resolver.
        embedded = [c for c in embedded if c.reference.kind == ResourceKind.VOLUME]
        for claim in embedded:
            peers = {c.reference for c in embedded
                     if c.reference.kind == claim.reference.kind}
            peers.update(r for r in known if r.kind == claim.reference.kind)
            if claim.reference in known:
                continue
            differing = {r for r in peers if r != claim.reference}
            if differing:
                code = (DiagnosticCode.IDENTITY_DISAGREEMENT
                        if any(r.provider == claim.reference.provider for r in differing)
                        else DiagnosticCode.UNVERIFIED_IDENTITY_RELATION)
                result.append(CandidateDiagnostic(
                    DiagnosticKind.CONFLICT, code, claim.provenance))
        return tuple(dict.fromkeys(result))

    @property
    def identification(self) -> IdentificationState:
        if self.conflicts:
            return IdentificationState.CONFLICTED
        if self.existing and self.existing.associations:
            return IdentificationState.IDENTIFIED
        if len(self.alternatives) > 1:
            return IdentificationState.AMBIGUOUS
        # A single hypothesis is still not a confirmed association.
        return IdentificationState.UNIDENTIFIED

    @property
    def review(self) -> ReviewState:
        if (self.file.stat_state == InspectionState.FAILED
                or any(d.kind == DiagnosticKind.FATAL for d in self.diagnostics)):
            return ReviewState.BLOCKED
        if self.identification in (
            IdentificationState.CONFLICTED, IdentificationState.AMBIGUOUS
        ):
            return ReviewState.REQUIRED
        return ReviewState.CONTINUE

"""Immutable acquisition observations, never matching or download decisions.

Resolver references identify source-owned records; they are not URLs or bearer
credentials. A future grab service must resolve them outside this contract.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from json import dumps
from re import fullmatch
from typing import Optional, Tuple

from backend.base.import_candidate import ProviderReference, ResourceKind

RELEASE_POLICY = 'kapowarr-release-candidate/v1'


def acquisition_identity(candidate) -> str:
    """Preserve legacy keys; exact torrent identity is source-independent."""
    facts = dict(getattr(candidate, 'torrent_facts', ()))
    value = (['torrent', candidate.candidate_id] if
        getattr(getattr(candidate, 'acquisition', None), 'mechanism', None) == AcquisitionMechanism.TORRENT
        and (facts.get('infohash_v1') or facts.get('infohash_v2')) else
        [candidate.source.kind.value, candidate.source.key[:200], candidate.candidate_id])
    return sha256(dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


class SourceKind(Enum):
    DIRECT_DOWNLOAD = 'direct_download'
    NEWZNAB = 'newznab'
    TORZNAB = 'torznab'
    EXTERNAL = 'external'


class AcquisitionMechanism(Enum):
    DIRECT_DOWNLOAD = 'direct_download'
    NZB = 'nzb'
    TORRENT = 'torrent'
    OTHER = 'other'


class LocatorKind(Enum):
    SOURCE_PAGE = 'source_page'
    SOURCE_RECORD = 'source_record'
    UNAVAILABLE = 'unavailable'


class ObservationOrigin(Enum):
    STRUCTURED = 'structured_source'
    TITLE = 'release_title'
    LEGACY = 'legacy_parser'


class ReleaseYearKind(Enum):
    UNSPECIFIED = 'unspecified'
    SERIES = 'series_start'
    ISSUE = 'issue_publication'


class ReleaseVolumeKind(Enum):
    UNSPECIFIED = 'unspecified'
    CANONICAL = 'canonical_volume_number'


@dataclass(frozen=True)
class ReleaseIdentity:
    """Source claim, not selected metadata authority or a matching decision."""

    reference: ProviderReference
    parent: Optional[ProviderReference] = None

    def __post_init__(self) -> None:
        _require_type(self.reference, ProviderReference)
        if self.parent is not None:
            _require_type(self.parent, ProviderReference)
        for ref in (self.reference, self.parent):
            if ref is not None and (
                ':/' in ref.provider_id or ref.provider_id.lower().startswith('magnet:')
            ):
                raise ValueError('Metadata identity must not be a credential-bearing URL')
        if self.parent is not None and (
            self.reference.kind != ResourceKind.ISSUE
            or self.parent.kind != ResourceKind.VOLUME
            or self.parent.provider != self.reference.provider
        ):
            raise ValueError('Issue parent must be a same-provider volume')


class CoverageKind(Enum):
    UNKNOWN = 'unknown'
    SINGLE = 'single'
    RANGE = 'range'
    SET = 'set'
    PACK = 'pack'
    COLLECTION = 'collection'


class PackKind(Enum):
    UNKNOWN = 'unknown'
    SINGLE = 'single_issue'
    MULTI_ISSUE = 'multi_issue'
    SERIES = 'series_pack'
    VOLUME = 'volume_pack'
    COLLECTION = 'collection'


class ReleaseDiagnosticCode(Enum):
    INVALID_RESULT = 'invalid_result'
    INVALID_SIZE = 'invalid_size'
    INVALID_TIME = 'invalid_published_time'
    LOCATOR_UNAVAILABLE = 'locator_unavailable'
    COVERAGE_UNAVAILABLE = 'coverage_unavailable'
    AMBIGUOUS_COVERAGE = 'ambiguous_coverage'
    CONFLICT = 'conflicting_observations'
    UNKNOWN_EXTENSION = 'unknown_extension'


@dataclass(frozen=True)
class ReleaseDiagnostic:
    code: ReleaseDiagnosticCode
    field: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, ReleaseDiagnosticCode):
            raise ValueError('Typed diagnostic required')
        if not isinstance(self.field, str):
            raise ValueError('Diagnostic field must be text, not an exception')


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_type(value: object, expected: type) -> None:
    """Validate untyped adapter callers as well as type-checked domain callers."""
    if not isinstance(value, expected):
        raise ValueError('Invalid release value type')


@dataclass(frozen=True)
class ReleaseSource:
    kind: SourceKind
    key: str
    name: str
    via: Optional[str] = None

    def __post_init__(self) -> None:
        _require_type(self.kind, SourceKind)
        if not _text(self.key) or not _text(self.name):
            raise ValueError(
                'Source namespace, stable key and display name required')
        if self.via is not None and not _text(self.via):
            raise ValueError('Aggregator attribution must be text')


@dataclass(frozen=True)
class AcquisitionReference:
    mechanism: AcquisitionMechanism
    kind: LocatorKind
    key: Optional[str] = None

    def __post_init__(self) -> None:
        _require_type(self.mechanism, AcquisitionMechanism)
        _require_type(self.kind, LocatorKind)
        if self.kind == LocatorKind.UNAVAILABLE:
            if self.key is not None:
                raise ValueError(
                    'Unavailable acquisition cannot have a resolver key')
        elif not isinstance(self.key, str) or len(self.key) != 64 or any(
            c not in '0123456789abcdef' for c in self.key
        ):
            raise ValueError(
                'Resolver keys must be SHA-256 references, never URLs')


@dataclass(frozen=True)
class ReleaseCoverage:
    kind: CoverageKind = CoverageKind.UNKNOWN
    labels: Tuple[str, ...] = ()
    raw: Optional[str] = None

    def __post_init__(self) -> None:
        _require_type(self.kind, CoverageKind)
        _require_type(self.labels, tuple)
        if any(not _text(label) for label in self.labels):
            raise ValueError('Issue labels must be nonempty raw strings')
        if self.raw is not None and not isinstance(self.raw, str):
            raise ValueError('Raw coverage must remain text')
        required = {CoverageKind.SINGLE: 1, CoverageKind.RANGE: 2}
        if self.kind in required and len(self.labels) != required[self.kind]:
            raise ValueError('Coverage cardinality mismatch')
        if self.kind == CoverageKind.SET and len(self.labels) < 2:
            raise ValueError('An issue set needs at least two labels')
        if self.kind not in (*required, CoverageKind.SET) and self.labels:
            raise ValueError(
                'Unknown/pack/collection cannot invent issue membership')


@dataclass(frozen=True)
class ReleaseObservation:
    """One coherent evidence tier; competing tiers never overwrite each other.

Format/kind values are literal source evidence, not SpecialVersion decisions.
Volume is raw text: a source's volume need not mean Kapowarr volume_number.
"""

    origin: ObservationOrigin
    locator: str
    series: Optional[str] = None
    year: Optional[int] = None
    volume: Optional[str] = None
    coverage: ReleaseCoverage = ReleaseCoverage()
    pack: PackKind = PackKind.UNKNOWN
    physical_format: Optional[str] = None
    publication_kind: Optional[str] = None
    special_version: Optional[str] = None
    extension: Optional[str] = None
    language: Optional[str] = None
    release_group: Optional[str] = None
    uploader: Optional[str] = None
    tags: Tuple[str, ...] = ()
    policy: Optional[str] = None
    identities: Tuple[ReleaseIdentity, ...] = ()
    year_kind: ReleaseYearKind = ReleaseYearKind.UNSPECIFIED
    volume_kind: ReleaseVolumeKind = ReleaseVolumeKind.UNSPECIFIED

    def __post_init__(self) -> None:
        _require_type(self.origin, ObservationOrigin)
        _require_type(self.year_kind, ReleaseYearKind)
        _require_type(self.volume_kind, ReleaseVolumeKind)
        _require_type(self.identities, tuple)
        if any(not isinstance(i, ReleaseIdentity) for i in self.identities):
            raise ValueError('Immutable qualified identity claims required')
        if self.identities and self.origin != ObservationOrigin.STRUCTURED:
            raise ValueError('Qualified claims require structured source provenance')
        if not _text(self.locator):
            raise ValueError('Evidence origin and field locator required')
        if self.year is not None and (
            type(self.year) is not int or not 1 <= self.year <= 9999):
            raise ValueError(
                'Year must be a genuine calendar year or unavailable')
        _require_type(self.coverage, ReleaseCoverage)
        _require_type(self.pack, PackKind)
        for name in (
            'series',
            'volume',
            'physical_format',
            'publication_kind',
            'special_version',
            'extension',
            'language',
            'release_group',
                'uploader'):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError('Observation text must remain text')
        _require_type(self.tags, tuple)
        if any(not isinstance(v, str) for v in self.tags):
            raise ValueError('Tags must be immutable strings')
        if len(self.tags) > 32 or any(len(v) > 1024 for v in self.tags):
            raise ValueError('Source tags exceed bounded evidence envelope')
        if self.policy is not None and not _text(self.policy):
            raise ValueError('Policy identifier must be text')


@dataclass(frozen=True)
class ReleaseCandidate:
    source: ReleaseSource
    raw_title: str
    acquisition: AcquisitionReference
    result_id: Optional[str] = None
    observations: Tuple[ReleaseObservation, ...] = ()
    size_bytes: Optional[int] = None
    published_at: Optional[datetime] = None
    diagnostics: Tuple[ReleaseDiagnostic, ...] = ()
    adapter: str = RELEASE_POLICY
    torrent_facts: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _text(self.raw_title):
            raise ValueError('Exact nonempty raw title required')
        if (not isinstance(self.torrent_facts, tuple) or len(self.torrent_facts) > 16  # type: ignore[redundant-expr]
                or any(not isinstance(pair, tuple) or len(pair) != 2  # type: ignore[redundant-expr]
                       or any(not isinstance(v, str) or len(v) > 128 for v in pair) for pair in self.torrent_facts)):  # type: ignore[redundant-expr]
            raise ValueError('Bounded immutable torrent facts required')
        facts = dict(self.torrent_facts)
        if len(facts) != len(self.torrent_facts) or any(key in facts and
                not fullmatch('[0-9a-f]{' + str(size) + '}', facts[key])
                for key, size in (('infohash_v1', 40), ('infohash_v2', 64))):
            raise ValueError('Exact unique torrent facts required')
        if len(self.raw_title) > 16384 or not _text(self.adapter):
            raise ValueError('Bounded title and adapter identifier required')
        if self.result_id is not None and not _text(self.result_id):
            raise ValueError('Result ID must be opaque nonempty text')
        if self.result_id and (
            ':/' in self.result_id or self.result_id.lower().startswith('magnet:')):
            raise ValueError('URL-shaped IDs require an adapter-owned digest')
        _require_type(self.source, ReleaseSource)
        _require_type(self.acquisition, AcquisitionReference)
        _require_type(self.observations, tuple)
        _require_type(self.diagnostics, tuple)
        if any(not isinstance(o, ReleaseObservation)
               for o in self.observations):
            raise ValueError('Immutable observations required')
        if any(not isinstance(d, ReleaseDiagnostic) for d in self.diagnostics):
            raise ValueError('Immutable diagnostics required')
        if self.size_bytes is not None and (
            type(self.size_bytes) is not int or self.size_bytes < 0):
            raise ValueError('Size is a nonnegative byte count or unavailable')
        if self.published_at is not None:
            _require_type(self.published_at, datetime)
            if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
                raise ValueError(
                    'Source publication timestamp requires timezone')
            object.__setattr__(
                self,
                'published_at',
                self.published_at.astimezone(
                    timezone.utc))

    @property
    def candidate_id(self) -> Optional[str]:
        """Source identity, NOT bibliographic identity or a quality/dedup decision.

No ID/locator means unavailable identity, never a title-only uniqueness claim.
"""
        if self.acquisition.mechanism == AcquisitionMechanism.TORRENT:
            facts = dict(self.torrent_facts)
            exact = facts.get('infohash_v1') or facts.get('infohash_v2')
            if exact:
                return sha256(('torrent:' + exact).encode()).hexdigest()
        if self.result_id is not None:
            reference = ('result', self.result_id)
        elif self.acquisition.key is not None:
            reference = ('locator', self.acquisition.key)
        else:
            return None
        return sha256(
            dumps(
                (self.source.kind.value, self.source.key, reference),
                ensure_ascii=False).encode('utf-8')).hexdigest()


def preview_release(candidate: ReleaseCandidate) -> dict:
    """Explicit public projection: no locator key, raw result ID, URLs or blobs.

Strings are data, not HTML. Future UIs must render title/source via textContent.
"""
    def project(value):
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        if is_dataclass(value):
            return {f.name: project(getattr(value, f.name))
                    for f in fields(value)}
        if isinstance(value, tuple):
            return [project(v) for v in value]
        return value

    return {
        'candidate_id': candidate.candidate_id,
        'raw_title': candidate.raw_title,
        'source': project(candidate.source),
        'mechanism': candidate.acquisition.mechanism.value,
        'locator_kind': candidate.acquisition.kind.value,
        'size_bytes': candidate.size_bytes,
        'published_at': project(candidate.published_at),
        'observations': project(candidate.observations),
        'diagnostics': project(candidate.diagnostics),
        'adapter': candidate.adapter,
        **({'torrent_facts': dict(candidate.torrent_facts)} if candidate.torrent_facts else {}),
        'policy': RELEASE_POLICY,
    }

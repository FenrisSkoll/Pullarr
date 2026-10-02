"""Pure source adaptation. No search, settings, matching, persistence or grab.

Legacy DDL results remain the execution input. The resolver key can correlate a
candidate to that caller-owned result; this module does not retain its URL.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from hashlib import sha256
from json import dumps
from re import IGNORECASE, compile
from typing import Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from backend.base.definitions import FileConstants, SearchResultData
from backend.base.file_extraction import (special_version_regex,
                                          volume_regex, year_regex)
from backend.base.release_candidate import (RELEASE_POLICY,
                                            AcquisitionMechanism,
                                            AcquisitionReference, CoverageKind,
                                            LocatorKind, ObservationOrigin,
                                            PackKind, ReleaseCandidate,
                                            ReleaseCoverage, ReleaseDiagnostic,
                                            ReleaseDiagnosticCode as Code,
                                            ReleaseObservation, ReleaseSource,
                                            SourceKind)

PARSER_POLICY = 'kapowarr-release-title/v1'
DDL_ADAPTER = 'kapowarr-getcomics-release/v1'
# Only explicit labels or a final issue token before a year/end are captured.
# This is a lossless title wrapper, not legacy float reversal or a second
# matcher.
_LABEL = r'(?:\d+(?:\.\d+)?[A-Za-z]*|\[nn\]|Annual|Special|½)'
_COVERAGE = rf'{_LABEL}(?:\s*(?:-|–|,)\s*{_LABEL})*'
_EXPLICIT = compile(
    rf'(?:#\s*|\bissues?\s+)({_COVERAGE})(?=$|\s|\()', IGNORECASE)
_TRAILING = compile(rf'\s({_COVERAGE})\s*(?=\(\d{{4}}\)|$)', IGNORECASE)
_DECIMAL = compile(r'\d+(?:\.\d+)?')
_SEPARATOR = compile(r'\s*[-–,]\s*')
_EXTENSION = compile(r'(\.[A-Za-z][A-Za-z0-9]*)$')
_KNOWN_EXTENSIONS = tuple(sorted(
    {value.lower() for value in FileConstants.CONTENT_EXTENSIONS},
    key=lambda value: (-len(value), value)
))
_PACK = compile(
    r'\b(?:(complete)\s+(series|volume)|((?:series|volume))\s+pack|pack)\b',
    IGNORECASE)


def parse_release_title(
    title: str) -> Tuple[ReleaseObservation, Tuple[ReleaseDiagnostic, ...]]:
    """Conservative raw captures; no implicit TPB, language or group guesses."""
    if len(title) > 16384:
        raise ValueError('Release title exceeds bounded parser input')
    diagnostics = []
    known_extension = next((title[-len(value):] for value in _KNOWN_EXTENSIONS
                            if title.lower().endswith(value)), None)
    extension_match = _EXTENSION.search(title) if not known_extension else None
    extension = known_extension or (extension_match.group(1) if extension_match else None)
    if extension and extension.lower() not in FileConstants.CONTAINER_EXTENSIONS:
        diagnostics.append(
            ReleaseDiagnostic(
                Code.UNKNOWN_EXTENSION,
                'extension'))
    stem = title[:-len(known_extension)] if known_extension else title
    years = tuple(year_regex.finditer(stem))
    volume = volume_regex.search(stem)
    specials = tuple(special_version_regex.finditer(stem))
    special = specials[0] if specials else None
    matches = tuple(_EXPLICIT.finditer(stem))
    if not matches:
        matches = tuple(
            m for m in _TRAILING.finditer(stem)
            if not volume or m.start(1) >= volume.end() or m.end(1)
            <= volume.start())
    coverage = ReleaseCoverage()
    pack = PackKind.UNKNOWN
    if len(matches) == 1:
        raw = matches[0].group(1)
        labels = tuple(_SEPARATOR.split(raw))
        if ',' in raw and ('-' in raw or '–' in raw):
            diagnostics.append(
                ReleaseDiagnostic(
                    Code.AMBIGUOUS_COVERAGE,
                    'coverage'))
            coverage = ReleaseCoverage(raw=raw)
        elif ',' in raw:
            coverage = ReleaseCoverage(CoverageKind.SET, labels, raw)
            pack = PackKind.MULTI_ISSUE
        elif '-' in raw or '–' in raw:
            if len(labels) == 2 and all(
                _DECIMAL.fullmatch(v) for v in labels) and Decimal(
                labels[0]) <= Decimal(
                labels[1]):
                coverage = ReleaseCoverage(CoverageKind.RANGE, labels, raw)
                pack = PackKind.MULTI_ISSUE
            else:
                coverage = ReleaseCoverage(raw=raw)
                diagnostics.append(
                    ReleaseDiagnostic(
                        Code.AMBIGUOUS_COVERAGE,
                        'coverage'))
        else:
            coverage = ReleaseCoverage(CoverageKind.SINGLE, labels, raw)
            pack = PackKind.SINGLE
        numeric_labels = tuple(Decimal(v)
                               for v in labels if _DECIMAL.fullmatch(v))
        if len(set(labels)) < len(labels) or len(
            set(numeric_labels)) < len(numeric_labels):
            diagnostics.append(
                ReleaseDiagnostic(
                    Code.AMBIGUOUS_COVERAGE,
                    'coverage'))
    elif matches:
        diagnostics.append(
            ReleaseDiagnostic(
                Code.AMBIGUOUS_COVERAGE,
                'coverage'))

    pack_match = _PACK.search(stem)
    if pack_match:
        text = pack_match.group().lower()
        pack = PackKind.SERIES if 'series' in text else PackKind.VOLUME if 'volume' in text else PackKind.MULTI_ISSUE
        if coverage.kind == CoverageKind.UNKNOWN:
            coverage = ReleaseCoverage(
                CoverageKind.PACK, raw=pack_match.group())

    physical = kind = special_value = None
    physical_values, kind_values, special_values = [], [], []
    for match in specials:
        value = next(k for k, v in match.groupdict().items() if v is not None)
        value = value.replace('_', '-')
        special_values.append(value)
        if value in ('tpb', 'hard-cover'):
            physical_values.append(match.group())
        else:
            kind_values.append(match.group())
    if special:
        physical = ' / '.join(physical_values) or None
        kind = ' / '.join(kind_values) or None
        if len(set(special_values)) == 1:
            special_value = special_values[0]
        else:
            diagnostics.append(ReleaseDiagnostic(Code.CONFLICT, 'special_version'))
        if coverage.kind == CoverageKind.UNKNOWN and not pack_match:
            coverage = ReleaseCoverage(
                CoverageKind.COLLECTION, raw=special.group())
            pack = PackKind.COLLECTION

    positions = [m.start() for m in (*years, *matches)]
    positions += [m.start()
                  for m in (volume, special, pack_match) if m is not None]
    series = stem[:min(positions)].strip() if positions else None
    year_values = tuple(int(next(v for v in m.groups() if v)) for m in years)
    if len(set(year_values)) > 1:
        diagnostics.append(ReleaseDiagnostic(Code.CONFLICT, 'year'))
    if coverage.kind == CoverageKind.UNKNOWN:
        diagnostics.append(
            ReleaseDiagnostic(
                Code.COVERAGE_UNAVAILABLE,
                'coverage'))
    return ReleaseObservation(
        ObservationOrigin.TITLE, 'display_title', series=series or None,
        year=year_values[0]
        if len(set(year_values)) == 1 and year_values[0] else None,
        volume=volume.group(1) if volume else None, coverage=coverage,
        pack=pack, physical_format=physical, publication_kind=kind,
        special_version=special_value, extension=extension,
        policy=PARSER_POLICY), tuple(diagnostics)


def resolver_key(source: ReleaseSource, locator: str) -> str:
    """Opaque correlation, not a stored authenticated URL or grab authorization."""
    return sha256(dumps((source.kind.value, source.key, locator),
                        ensure_ascii=False).encode('utf-8')).hexdigest()


def normalize_release(
    source: ReleaseSource,
    raw_title: str,
    acquisition: AcquisitionReference,
    *,
    result_id: Optional[str] = None,
    structured: Tuple[ReleaseObservation, ...] = (),
    size: object = None,
    published: object = None,
    source_timezone: Optional[tzinfo] = None,
    adapter: str = RELEASE_POLICY
) -> ReleaseCandidate:
    """Normalize already acquired facts; invalid optional fields stay unavailable.

Structured observations must be supplied by an allowlisting adapter. Never pass
raw HTTP dictionaries/headers or credential-bearing GUID URLs through this seam.
"""
    if not isinstance(raw_title, str):
        raise ValueError('Source result title must be text')
    if not raw_title.strip():
        raise ValueError('Source result requires a nonempty raw title')
    if len(raw_title) > 16384:
        raise ValueError('Release title exceeds bounded parser input')
    # GUID fields can be authenticated URLs. Keep opaque IDs verbatim, but
    # retain only a correlation digest for URL-shaped IDs.
    if isinstance(
        result_id, str) and (
        ':/' in result_id or result_id.lower().startswith('magnet:')):
        result_id = 'url-sha256:' + resolver_key(source, result_id)
    parsed, notes = parse_release_title(raw_title)
    diagnostics = list(notes)
    size_bytes = size if type(size) is int and size >= 0 else None
    if size is not None and size_bytes is None:
        diagnostics.append(ReleaseDiagnostic(Code.INVALID_SIZE, 'size'))
    published_at = None
    if published is not None:
        try:
            value = datetime.fromisoformat(published.replace(
                'Z', '+00:00')) if isinstance(published, str) else published
            if not isinstance(value, datetime):
                raise ValueError('Invalid timestamp')
            if value.utcoffset() is None:
                if source_timezone is None:
                    raise ValueError('Unknown source timezone')
                value = value.replace(tzinfo=source_timezone)
            if value.utcoffset() is None:
                raise ValueError('Unknown source timezone offset')
            published_at = value.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            diagnostics.append(
                ReleaseDiagnostic(
                    Code.INVALID_TIME,
                    'published_at'))
    if acquisition.kind == LocatorKind.UNAVAILABLE:
        diagnostics.append(
            ReleaseDiagnostic(
                Code.LOCATOR_UNAVAILABLE,
                'acquisition'))
    for observation in structured:
        for field in ('series', 'year', 'volume', 'extension'):
            left, right = getattr(observation, field), getattr(parsed, field)
            if left is not None and right is not None and left != right:
                diagnostics.append(ReleaseDiagnostic(Code.CONFLICT, field))
    return ReleaseCandidate(
        source, raw_title, acquisition, result_id,
        (*structured, parsed), size_bytes, published_at,
        tuple(dict.fromkeys(diagnostics)), adapter
    )


def adapt_ddl_result(result: Mapping[str, object]) -> ReleaseCandidate:
    """Adapt pre-match GetComics output; ignore match/rank/target-mutated fields.

No fake GUID: source instance plus locator digest identifies the page offering.
Page resolution can eventually offer several hosts/protocols, not a known CBZ.
"""
    source_id = result.get('indexer_id')
    source_name = result.get('indexer_title')
    title = result.get('display_title')
    if type(source_id) is not int or source_id <= 0 or not isinstance(
        source_name, str) or not source_name.strip():
        raise ValueError('DDL source identity missing')
    source = ReleaseSource(
        SourceKind.DIRECT_DOWNLOAD,
        f'getcomics:{source_id}',
        source_name)
    locator = result.get('link')
    available = False
    if isinstance(locator, str) and locator:
        try:
            url = urlsplit(locator)
            available = url.scheme in ('http', 'https') and bool(url.hostname)
        except ValueError:
            pass
    acquisition = AcquisitionReference(
        AcquisitionMechanism.DIRECT_DOWNLOAD,
        LocatorKind.SOURCE_PAGE if available else LocatorKind.UNAVAILABLE,
        resolver_key(source, locator) if available else None
    )
    # Copy legacy bibliographic observations only, never reverse a float to a
    # raw issue label or inherit its implicit TPB / target-specific refinement.
    series, year = result.get('series'), result.get('year')
    legacy = ReleaseObservation(
        ObservationOrigin.LEGACY, 'SearchResultData',
        series=series if isinstance(series, str) else None,
        year=year if type(year) is int and 1 <= year <= 9999 else None,
        policy='kapowarr-legacy-filename/v1'
    )
    return normalize_release(
        source, title, acquisition, structured=(legacy,),
        size=None if result.get('size') == -1 else result.get('size'),
        adapter=DDL_ADAPTER
    )


@dataclass(frozen=True)
class AdaptationFailure:
    position: int
    diagnostic: ReleaseDiagnostic


@dataclass(frozen=True)
class ReleaseBatch:
    candidates: Tuple[ReleaseCandidate, ...]
    failures: Tuple[AdaptationFailure, ...]


def adapt_ddl_results(results: Iterable[SearchResultData]) -> ReleaseBatch:
    """Linear, input-order preserving; malformed records do not erase peers.

Only input validation failures are isolated. Programming/runtime errors escape.
Duplicates remain candidates, not silently merged or ranked.
"""
    candidates, failures = [], []
    for position, result in enumerate(results):
        try:
            if not isinstance(result, Mapping):
                raise ValueError('Expected source record')
            candidates.append(adapt_ddl_result(result))
        except ValueError:
            failures.append(
                AdaptationFailure(
                    position,
                    ReleaseDiagnostic(
                        Code.INVALID_RESULT,
                        'result')))
    return ReleaseBatch(tuple(candidates), tuple(failures))

"""Bounded Newznab acquisition; Prowlarr discovery uses the same proxy parser.

No provider, library, scoring, downloader or filesystem dependencies. Transport
does not log authenticated URLs, follow redirects, use netrc or inherit proxies.
"""

import json
import socket
import ssl
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from time import monotonic
from typing import Callable, Optional, Tuple
from urllib.parse import urlencode, urlsplit
from xml.etree.ElementTree import (ParseError, TreeBuilder,
                                   XMLParser, fromstring)

from backend.base.release_candidate import (AcquisitionMechanism,
                                            AcquisitionReference, LocatorKind,
                                            ObservationOrigin,
                                            ReleaseCandidate,
                                            ReleaseObservation)
from backend.base.release_search import (SOURCE_POLICY, ReleaseSearchRequest,
                                         SearchCapabilities, SearchError,
                                         SearchLimits, SearchPage,
                                         SourceConfig, SourceFailure)
from backend.implementations.release_candidates import (normalize_release,
                                                        resolver_key)

NEWZNAB_NS = 'http://www.newznab.com/DTD/2010/feeds/attributes/'
MAX_SEARCH_BYTES = 8 * 1024 * 1024
MAX_CAPS_BYTES = 512 * 1024


class SearchBudget:
    """One operation, shared across discovery, capabilities, pages and sources."""

    def __init__(self, limits: SearchLimits, cancelled: Callable[[], bool] = lambda: False):
        self.remaining = limits.requests
        self.cancelled = cancelled

    def consume(self):
        if self.cancelled():
            raise SourceFailure(SearchError.CANCELLED)
        if not self.remaining:
            raise SourceFailure(SearchError.LIMIT)
        self.remaining -= 1


class BoundedHTTP:
    """Finite connect/read/deadline, identity encoding only, no retries/redirects."""

    def __init__(self, budget: SearchBudget, connect_timeout=5.0, read_timeout=10.0):
        if not 0 < connect_timeout <= 10 or not 0 < read_timeout <= 30:
            raise SourceFailure(SearchError.CONFIGURATION)
        self.budget = budget
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout

    def get(self, config: SourceConfig, suffix: str, params: dict, maximum: int) -> bytes:
        self.budget.consume()
        u = urlsplit(config.url)
        headers = {'Accept-Encoding': 'identity', 'Accept': 'application/xml, application/json',
                   'User-Agent': 'Pullarr-release-search/1'}
        query = dict(params)
        if config.mode == 'prowlarr':
            headers['X-Api-Key'] = config.api_key
        else:
            query['apikey'] = config.api_key
        path = (u.path.rstrip('/') + suffix or '/') + '?' + urlencode(query)
        connection = (HTTPSConnection(u.hostname, u.port, timeout=self.connect_timeout,
                                     context=ssl.create_default_context()) if u.scheme == 'https'
                      else HTTPConnection(u.hostname, u.port, timeout=self.connect_timeout))
        from backend.implementations.configured_network import (ReadDeadline,
                                                                bounded_socket)
        connection._create_connection = bounded_socket
        guard = None
        try:
            connection.connect()
            guard = ReadDeadline(connection.sock, self.read_timeout)
            connection.sock.settimeout(self.read_timeout)
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            if guard.expired.is_set():
                raise SourceFailure(SearchError.TIMEOUT)
            if response.status in (401, 403):
                raise SourceFailure(SearchError.AUTHENTICATION)
            if response.status == 429:
                delay = response.getheader('Retry-After', '')
                raise SourceFailure(SearchError.RATE_LIMITED,
                                    min(int(delay), 86400) if delay.isdecimal() and len(delay) < 10 else None)
            if response.status != 200:
                raise SourceFailure(SearchError.UNAVAILABLE)
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise SourceFailure(SearchError.UNSUPPORTED)
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or len(length) > 10 or int(length) > maximum):
                raise SourceFailure(SearchError.LIMIT)
            parts, size = [], 0
            deadline = monotonic() + self.read_timeout
            while True:
                if self.budget.cancelled():
                    raise SourceFailure(SearchError.CANCELLED)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise SourceFailure(SearchError.TIMEOUT)
                # read1 prevents a peer's trickle stream from resetting an unlimited read loop.
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, maximum + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise SourceFailure(SearchError.LIMIT)
                parts.append(chunk)
            if length is not None and size != int(length):
                raise SourceFailure(SearchError.INVALID_RESPONSE)
            if guard.expired.is_set():
                raise SourceFailure(SearchError.TIMEOUT)
            return b''.join(parts)
        except (socket.timeout, TimeoutError):
            raise SourceFailure(SearchError.TIMEOUT) from None
        except (OSError, HTTPException, ValueError):
            raise SourceFailure(SearchError.TIMEOUT if guard and guard.expired.is_set() else SearchError.UNAVAILABLE) from None
        finally:
            if guard:
                guard.cancel()
            connection.close()


class _BoundedTree(TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = self.elements = 0
        self.text_lengths = []

    def doctype(self, name, pubid, system):
        raise SourceFailure(SearchError.INVALID_RESPONSE)

    def start(self, tag, attrs):
        self.depth += 1
        self.elements += 1
        self.text_lengths.append(0)
        if (self.depth > 16 or self.elements > 50000 or len(attrs) > 32
                or len(tag) > 512 or any(len(k) > 512 or len(v) > 16384 for k, v in attrs.items())
                or tag.startswith('{http://www.w3.org/2001/XInclude}')):
            raise SourceFailure(SearchError.LIMIT)
        return super().start(tag, attrs)

    def data(self, value):
        if self.text_lengths:
            self.text_lengths[-1] += len(value)
        if len(value) > 65536 or self.text_lengths and self.text_lengths[-1] > 65536:
            raise SourceFailure(SearchError.LIMIT)
        return super().data(value)

    def end(self, tag):
        self.depth -= 1
        self.text_lengths.pop()
        return super().end(tag)


def _xml(data: bytes, maximum: int):
    if len(data) > maximum:
        raise SourceFailure(SearchError.LIMIT)
    try:
        root = fromstring(data, parser=XMLParser(target=_BoundedTree()))
    except ParseError:
        raise SourceFailure(SearchError.INVALID_RESPONSE) from None
    if root.tag == 'error':
        code = root.get('code', '')
        mapped = (SearchError.AUTHENTICATION if code in ('100', '101', '102') else
                  SearchError.RATE_LIMITED if code == '500' else
                  SearchError.UNSUPPORTED if code == '203' else SearchError.PROTOCOL)
        raise SourceFailure(mapped)
    return root


def parse_capabilities(data: bytes) -> SearchCapabilities:
    root = _xml(data, MAX_CAPS_BYTES)
    if root.tag != 'caps':
        raise SourceFailure(SearchError.INVALID_RESPONSE)
    search = root.find('searching/search')
    limits = root.find('limits')
    value = limits.get('max', '100') if limits is not None else '100'
    if not value.isdecimal() or len(value) > 7 or int(value) < 1:
        raise SourceFailure(SearchError.INVALID_RESPONSE)
    categories = tuple(sorted({int(e.get('id')) for e in root.iter()
                        if e.tag in ('category', 'subcat') and
                        e.get('id', '').isdecimal() and len(e.get('id')) < 9}))
    return SearchCapabilities(search is not None and search.get('available') == 'yes'
                              and 'q' in search.get('supportedParams', 'q').split(','),
                              min(100, int(value)), categories)


def _text(element, name, maximum=2048):
    value = element.findtext(name)
    if value is not None and len(value) > maximum:
        raise ValueError('Bounded field required')
    return value


def _integer(value):
    return int(value) if value and len(value) <= 18 and value.isdecimal() else None


@dataclass(frozen=True)
class RawNewznabResult:
    title: str
    guid: Optional[str] = field(repr=False)
    locator: Optional[str] = field(repr=False)
    size: Optional[str] = None
    published: Optional[str] = None
    categories: Tuple[int, ...] = ()


@dataclass(frozen=True)
class PrivateNZBReference:
    """Source-owned resolver input, never portable or JSON transport data."""

    candidate: ReleaseCandidate
    guid: Optional[str] = field(repr=False)
    url: str = field(repr=False)


def parse_search(data: bytes):
    """Structural failures fail a page; bounded item failures retain valid peers."""
    root = _xml(data, MAX_SEARCH_BYTES)
    channel = root.find('channel')
    if root.tag != 'rss' or channel is None:
        raise SourceFailure(SearchError.INVALID_RESPONSE)
    items = channel.findall('item')
    if len(items) > 1000:
        raise SourceFailure(SearchError.LIMIT)
    results, failures = [], []
    for position, item in enumerate(items):
        try:
            title = _text(item, 'title', 16384)
            if not title or not title.strip():
                raise ValueError('Title required')
            guid = _text(item, 'guid')
            if guid is not None and not guid.strip():
                guid = None
            enclosure = item.find('enclosure')
            locator = enclosure.get('url') if enclosure is not None else _text(item, 'link')
            if locator and len(locator) > 4096:
                raise ValueError('Locator too long')
            if enclosure is not None and enclosure.get('type', 'application/x-nzb') != 'application/x-nzb':
                raise ValueError('Non NZB enclosure')
            attributes = item.findall('{' + NEWZNAB_NS + '}attr')
            if len(attributes) > 64 or any(len(a.get('name', '')) > 64 or
                                          len(a.get('value', '')) > 2048 for a in attributes):
                raise ValueError('Oversized attributes')
            sizes = {a.get('value') for a in attributes if a.get('name') == 'size'}
            if len(sizes) > 1:
                raise ValueError('Conflicting source sizes')
            size = next(iter(sizes), enclosure.get('length') if enclosure is not None else None)
            if size is not None and len(size) > 128:
                raise ValueError('Size too long')
            categories = tuple(sorted({int(a.get('value')) for a in attributes
                               if a.get('name') == 'category' and
                               a.get('value', '').isdecimal() and len(a.get('value')) < 9}))
            if len(categories) > 32:
                raise ValueError('Too many categories')
            results.append((position, RawNewznabResult(title, guid, locator, size,
                           _text(item, 'pubDate', 128), categories)))
        except ValueError:
            failures.append(position)
    response = channel.find('{' + NEWZNAB_NS + '}response')
    offset = _integer(response.get('offset')) if response is not None else None
    total = _integer(response.get('total')) if response is not None else None
    if response is not None and (offset is None or total is None):
        raise SourceFailure(SearchError.PAGINATION)
    return tuple(results), len(items), offset, total, tuple(failures)


class NewznabSource:
    """One configured direct source or discovered Usenet proxy, operation scoped.

Resolver entries intentionally die on close; no NZB bytes are fetched in 5D.
"""

    def __init__(self, config: SourceConfig, transport: BoundedHTTP,
                 indexer_id: Optional[int] = None, name: Optional[str] = None):
        if (config.mode == 'prowlarr' and (type(indexer_id) is not int or not 1 <= indexer_id <= 2147483647)
                or config.mode == 'newznab' and indexer_id is not None):
            raise SourceFailure(SearchError.CONFIGURATION)
        self.config, self.transport = config, transport
        self.source = config.source(indexer_id, name)
        self.priority, self.categories = config.priority, tuple(sorted(set(config.categories)))
        self.suffix = f'/{indexer_id}/api' if indexer_id is not None else ''
        self._caps = None
        self._resolvers = {}
        self._closed = False

    def capabilities(self):
        if self._caps is None:
            self._caps = parse_capabilities(self.transport.get(self.config, self.suffix,
                                                  {'t': 'caps', 'o': 'xml'}, MAX_CAPS_BYTES))
        return self._caps

    def _bound_locator(self, value):
        if not value:
            return None
        try:
            u, base = urlsplit(value), urlsplit(self.config.url)
            if (u.scheme == base.scheme and u.hostname == base.hostname and
                    (u.port or (443 if u.scheme == 'https' else 80)) ==
                    (base.port or (443 if base.scheme == 'https' else 80)) and
                    not u.username and not u.password and not u.fragment):
                return value
        except ValueError:
            pass
        return None

    def search(self, request: ReleaseSearchRequest) -> SearchPage:
        if self._closed:
            raise SourceFailure(SearchError.CONFIGURATION)
        params = {'t': 'search', 'q': request.query, 'offset': request.offset,
                  'limit': request.limit, 'extended': 1, 'o': 'xml'}
        if request.categories:
            params['cat'] = ','.join(map(str, request.categories))
        data = self.transport.get(self.config, self.suffix, params, MAX_SEARCH_BYTES)
        rows, count, offset, total, failures = parse_search(data)
        candidates = []
        failures = list(failures)
        for position, raw in rows:
            if self.config.api_key in raw.title:
                failures.append(position)
                continue
            bound = self._bound_locator(raw.locator)
            # Keep arbitrary opaque/credential-shaped GUIDs private. Digests are
            # namespaced correlations, not bibliographic identity or credentials.
            identity = raw.guid or bound
            key = resolver_key(self.source, identity) if identity else None
            acquisition = AcquisitionReference(AcquisitionMechanism.NZB,
                LocatorKind.SOURCE_RECORD if bound else LocatorKind.UNAVAILABLE,
                key if bound else None)
            published = raw.published
            if published:
                try:
                    published = parsedate_to_datetime(published)
                except (ValueError, TypeError, OverflowError):
                    pass  # The neutral adapter records invalid optional timestamps.
            observation = ReleaseObservation(ObservationOrigin.STRUCTURED, 'newznab.item',
                tags=tuple('newznab-category:' + str(c) for c in raw.categories), policy=SOURCE_POLICY)
            candidate = normalize_release(self.source, raw.title, acquisition,
                result_id='source-sha256:' + key if key else None, structured=(observation,),
                size=_integer(raw.size) if _integer(raw.size) is not None else raw.size,
                published=published, adapter=SOURCE_POLICY)
            candidates.append(candidate)
            if bound and key and len(self._resolvers) < 1000:
                self._resolvers[(candidate.candidate_id, key)] = PrivateNZBReference(candidate, raw.guid, bound)
        return SearchPage(tuple(candidates), count, offset, total, tuple(sorted(failures)))

    def selected_reference(self, candidate: ReleaseCandidate):
        """Internal future resolver seam; no HTTP, no authorization, never a DTO.

        Requires the exact in-memory candidate from this operation and source. Consumers
        must not serialize the private record. 5E still owns explicit selection and
fresh resolution failure handling. No client-supplied URL is accepted here.
"""
        found = self._resolvers.get((candidate.candidate_id, candidate.acquisition.key))
        if self._closed or candidate.source != self.source or found is None or found.candidate != candidate:
            raise SourceFailure(SearchError.UNAVAILABLE)
        return found

    def close(self):
        self._resolvers.clear()
        self._closed = True


def discover_prowlarr(config: SourceConfig, transport: BoundedHTTP, maximum=16):
    """Allowlist GET /api/v1/indexer fields; never retain returned Fields/secrets."""
    data = transport.get(config, '/api/v1/indexer', {}, MAX_CAPS_BYTES)
    try:
        rows = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise SourceFailure(SearchError.INVALID_RESPONSE) from None
    if not isinstance(rows, list) or len(rows) > 512:
        raise SourceFailure(SearchError.INVALID_RESPONSE)
    eligible, excluded, ids = [], [], set()
    for row in rows:
        if (not isinstance(row, dict) or type(row.get('id')) is not int
                or not 1 <= row['id'] <= 2147483647 or row['id'] in ids):
            raise SourceFailure(SearchError.INVALID_RESPONSE)
        ids.add(row['id'])
        if (row.get('protocol') not in ('usenet', 'torrent') or row.get('enable') is not True
                or row.get('supportsSearch') is not True):
            excluded.append(row['id'])
            continue
        if (not isinstance(row.get('name'), str) or not row['name'].strip()
                or len(row['name']) > 256 or config.api_key in row['name']):
            raise SourceFailure(SearchError.INVALID_RESPONSE)
        eligible.append((row['id'], row['name'], row['protocol']))
    eligible.sort()
    from backend.implementations.torznab import TorznabSource
    sources = tuple((TorznabSource if protocol == 'torrent' else NewznabSource)(config, transport, i, name)
                    for i, name, protocol in eligible[:maximum])
    return sources, tuple(sorted(excluded)), len(eligible) > maximum

"""Transient explicit release-search contracts. None grants download permission."""

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from json import dumps
from typing import Optional, Protocol, Tuple
from urllib.parse import urlsplit

from backend.base.release_candidate import (ReleaseCandidate,
                                            ReleaseSource, SourceKind)

QUERY_POLICY = 'kapowarr-release-query/v1'
SOURCE_POLICY = 'kapowarr-newznab-source/v1'


class SearchError(Enum):
    CONFIGURATION = 'configuration'
    AUTHENTICATION = 'authentication'
    UNAVAILABLE = 'unavailable'
    TIMEOUT = 'timeout'
    RATE_LIMITED = 'rate_limited'
    INVALID_RESPONSE = 'invalid_response'
    UNSUPPORTED = 'unsupported_capability'
    PROTOCOL = 'protocol_error'
    PAGINATION = 'pagination_error'
    LIMIT = 'limit_reached'
    ITEM = 'invalid_item'
    CANCELLED = 'cancelled'


class SourceFailure(Exception):
    """Only safe typed information; never a URL, remote body or original exception."""

    def __init__(self, code: SearchError, retry_after: Optional[int] = None):
        self.code, self.retry_after = code, retry_after
        super().__init__(code.value)


class SearchState(Enum):
    COMPLETE = 'complete'
    PARTIAL = 'partial'
    FAILED = 'failed'
    DISABLED = 'disabled'


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    url: str = field(repr=False)
    api_key: str = field(repr=False)
    mode: str = 'newznab'
    enabled: bool = True
    priority: int = 0
    categories: Tuple[int, ...] = ()

    def __post_init__(self):
        if not isinstance(self.url, str):
            raise SourceFailure(SearchError.CONFIGURATION)
        try:
            u = urlsplit(self.url)
            valid_url = (u.scheme in ('http', 'https') and u.hostname and
                         not u.username and not u.password and not u.query and
                         not u.fragment and (u.port is None or 0 < u.port < 65536))
        except (ValueError, TypeError):
            valid_url = False
        if (not valid_url or len(self.url) > 2048 or any(ord(c) < 33 for c in self.url)
                or not isinstance(self.key, str) or not self.key or len(self.key) > 64
                or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in self.key)
                or not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 256
                or not isinstance(self.api_key, str) or (self.mode != 'torznab' and not self.api_key.strip()) or len(self.api_key) > 1024
                or any(ord(c) < 32 or ord(c) > 126 for c in self.api_key)
                or self.mode not in ('newznab', 'prowlarr', 'torznab') or type(self.enabled) is not bool
                or type(self.priority) is not int or not -1000 <= self.priority <= 1000
                or not isinstance(self.categories, tuple) or len(self.categories) > 32
                or any(type(c) is not int or not 0 < c < 100000000 for c in self.categories)):
            raise SourceFailure(SearchError.CONFIGURATION)

    @property
    def namespace(self) -> str:
        # Credentials are deliberately excluded. URL edits never reuse result IDs.
        return self.key + '-' + sha256(self.url.rstrip('/').encode()).hexdigest()[:24]

    def source(self, indexer_id: Optional[int] = None, name: Optional[str] = None) -> ReleaseSource:
        key = self.namespace + (f'-indexer-{indexer_id}' if indexer_id is not None else '')
        return ReleaseSource(SourceKind.TORZNAB if self.mode == 'torznab' else SourceKind.NEWZNAB, key, name or self.name,
                             self.name if self.mode == 'prowlarr' else None)

    def preview(self) -> dict:
        return {'id': self.key, 'name': self.name, 'url': self.url,
                'mode': self.mode, 'enabled': self.enabled, 'priority': self.priority,
                'categories': list(self.categories), 'api_key_present': bool(self.api_key)}


@dataclass(frozen=True)
class SearchLimits:
    queries: int = 3
    pages: int = 3
    page_size: int = 100
    source_results: int = 500
    total_results: int = 2000
    sources: int = 16
    requests: int = 64

    def __post_init__(self):
        for value, maximum in zip((self.queries, self.pages, self.page_size,
                self.source_results, self.total_results, self.sources, self.requests),
                (3, 5, 100, 1000, 10000, 32, 128)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise SourceFailure(SearchError.CONFIGURATION)


@dataclass(frozen=True)
class SearchCapabilities:
    search: bool = True
    limit: int = 100
    categories: Tuple[int, ...] = ()


@dataclass(frozen=True)
class ReleaseSearchRequest:
    query: str
    categories: Tuple[int, ...] = ()
    offset: int = 0
    limit: int = 100

    def __post_init__(self):
        if (not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 512
                or any(ord(c) < 32 for c in self.query)
                or type(self.offset) is not int or not 0 <= self.offset <= 10000
                or type(self.limit) is not int or not 1 <= self.limit <= 100
                or not isinstance(self.categories, tuple) or len(self.categories) > 32
                or any(type(c) is not int or not 0 < c < 100000000 for c in self.categories)):
            raise SourceFailure(SearchError.CONFIGURATION)

    @property
    def query_id(self) -> str:
        return sha256(dumps((QUERY_POLICY, self.query, self.categories)).encode()).hexdigest()


@dataclass(frozen=True)
class SearchPage:
    candidates: Tuple[ReleaseCandidate, ...]
    count: int
    offset: Optional[int] = None
    total: Optional[int] = None
    invalid_items: Tuple[int, ...] = ()


class ReleaseSearchSource(Protocol):
    source: ReleaseSource
    priority: int
    categories: Tuple[int, ...]

    def capabilities(self) -> SearchCapabilities: ...
    def search(self, request: ReleaseSearchRequest) -> SearchPage: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class SearchDiagnostic:
    source: str
    code: SearchError
    query_id: Optional[str] = None
    offset: Optional[int] = None
    item: Optional[int] = None
    retry_after: Optional[int] = None


@dataclass(frozen=True)
class QueryAttempt:
    source: str
    request: ReleaseSearchRequest
    count: int
    error: Optional[SearchError] = None
    candidate_ids: Tuple[Optional[str], ...] = ()


@dataclass(frozen=True)
class SourceSearchResult:
    source: ReleaseSource
    state: SearchState
    candidates: Tuple[ReleaseCandidate, ...] = ()
    attempts: Tuple[QueryAttempt, ...] = ()
    diagnostics: Tuple[SearchDiagnostic, ...] = ()

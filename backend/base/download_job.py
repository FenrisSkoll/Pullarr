"""Explicit acquisition intent and downloader observations, never scoring state."""

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Optional
from urllib.parse import unquote, urlsplit

DOWNLOAD_POLICY = 'kapowarr-sabnzbd-download/v1'


class DownloadErrorCode(str, Enum):
    CONFIGURATION = 'configuration'
    SELECTION = 'invalid_selection'
    EXPIRED = 'resolver_expired'
    RESOLUTION = 'source_resolution_failed'
    REDIRECT = 'redirect_rejected'
    INVALID_NZB = 'invalid_nzb'
    AUTHENTICATION = 'authentication'
    UNAVAILABLE = 'unavailable'
    TIMEOUT = 'timeout'
    LIMIT = 'response_limit'
    INVALID_RESPONSE = 'invalid_response'
    CATEGORY = 'category_missing'
    REJECTED = 'submission_rejected'
    AMBIGUOUS = 'submission_ambiguous'
    MISSING = 'remote_missing'
    REMOTE_FAILED = 'remote_failed'
    DRIFT = 'downloader_configuration_changed'
    BUSY = 'busy'


class DownloadFailure(Exception):
    def __init__(self, code: DownloadErrorCode):
        self.code = code
        super().__init__(code.value)


class DownloadJobState(str, Enum):
    PENDING = 'pending'
    SUBMITTING = 'submitting'
    SUBMITTED = 'submitted'
    QUEUED = 'queued'
    DOWNLOADING = 'downloading'
    POST_PROCESSING = 'post_processing'
    COMPLETED = 'completed'
    FAILED = 'failed'
    REMOTE_UNKNOWN = 'remote_unknown'
    AMBIGUOUS = 'ambiguous'


def endpoint(value) -> str:
    """Administrator configuration only; no credential-bearing base URLs."""
    try:
        p = urlsplit(value)
        valid = (isinstance(value, str) and 0 < len(value) <= 2048
                 and p.scheme in ('http', 'https') and p.hostname and p.port != 0
                 and not (p.username or p.password or p.query or p.fragment)
                 and not any(ord(c) < 33 or c == '\\' for c in value))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise DownloadFailure(DownloadErrorCode.CONFIGURATION)
    return value.rstrip('/')


@dataclass(frozen=True)
class SABConfig:
    key: str
    name: str
    url: str = field(repr=False)
    api_key: str = field(repr=False)
    enabled: bool = True
    category: str = '*'
    priority: int = -100

    def __post_init__(self):
        endpoint(self.url)
        if (not isinstance(self.key, str) or not 1 <= len(self.key) <= 64
                or not all(c.isascii() and (c.isalnum() or c in '-_') for c in self.key)
                or type(self.enabled) is not bool or type(self.priority) is not int
                or self.priority not in (-100, -2, -1, 0, 1, 2)):
            raise DownloadFailure(DownloadErrorCode.CONFIGURATION)
        for value, maximum in ((self.name, 256), (self.category, 128), (self.api_key, 1024)):
            if (not isinstance(value, str) or not value.strip() or len(value) > maximum
                    or any(ord(c) < 32 for c in value)):
                raise DownloadFailure(DownloadErrorCode.CONFIGURATION)
        if not self.api_key.isascii():
            raise DownloadFailure(DownloadErrorCode.CONFIGURATION)
        if any(self.api_key in value for value in (self.name, self.category, unquote(self.url))):
            raise DownloadFailure(DownloadErrorCode.CONFIGURATION)

    @property
    def instance(self) -> str:
        return sha256((self.key + '\n' + endpoint(self.url)).encode()).hexdigest()

    def preview(self) -> dict:
        return dict(id=self.key, name=self.name, url=self.url, enabled=self.enabled,
                    category=self.category, priority=self.priority, api_key_present=bool(self.api_key))


@dataclass(frozen=True)
class GrabIntent:
    """Internal, secret-free receipt. Request ID distinguishes replay from repeat."""
    request_id: str
    evaluation_id: str
    candidate_id: str
    source_key: str
    resolver_key: str = field(repr=False)
    volume_id: int
    issue_ids: tuple[int, ...]
    target_digest: str
    scoring_fingerprint: str
    title: str
    source_name: str
    client_id: str
    client_instance: str
    category: str
    priority: int
    policy: str = DOWNLOAD_POLICY
    client_kind: str = 'sabnzbd'
    protocol: str = 'nzb'


@dataclass(frozen=True)
class ResolvedNZB:
    candidate_id: str
    source_key: str
    data: bytes = field(repr=False)
    digest: str
    filename: str


@dataclass(frozen=True)
class RemoteDownload:
    nzo_id: str
    state: DownloadJobState
    status: str
    category: Optional[str] = None
    progress: Optional[float] = None
    storage: Optional[str] = None
    completed: Optional[int] = None
    error: Optional[DownloadErrorCode] = None
    paths: tuple[str, ...] = ()

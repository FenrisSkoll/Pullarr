"""Configured client capabilities and seeding policy; never quality ranking."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, endpoint)

MODES = ('client_managed', 'keep', 'after_import', 'ratio', 'seedtime', 'either', 'both')


def ratio(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 10000:
            raise ValueError
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise DownloadFailure(E.CONFIGURATION) from None


@dataclass(frozen=True)
class RetentionPolicy:
    mode: str = 'client_managed'
    ratio_target: str = '1'
    seed_seconds: int = 86400
    respect_minimums: bool = True

    def __post_init__(self):
        if (self.mode not in MODES or type(self.respect_minimums) is not bool
                or type(self.seed_seconds) is not int or not 0 <= self.seed_seconds <= 315360000):
            raise DownloadFailure(E.CONFIGURATION)
        ratio(self.ratio_target)


@dataclass(frozen=True)
class ManagedClientConfig:
    key: str
    name: str
    url: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    kind: str = 'nzbget'
    enabled: bool = True
    category: str = 'pullarr'
    priority: int = 0
    retention: RetentionPolicy = RetentionPolicy()

    def __post_init__(self):
        endpoint(self.url)
        if (self.kind not in ('nzbget', 'qbittorrent') or type(self.enabled) is not bool
                or type(self.priority) is not int or not -100 <= self.priority <= 100
                or not isinstance(self.retention, RetentionPolicy)
                or not isinstance(self.key, str) or not 1 <= len(self.key) <= 64
                or any(not (c.isascii() and (c.isalnum() or c in '-_')) for c in self.key)):
            raise DownloadFailure(E.CONFIGURATION)
        for value, maximum, required in ((self.name, 256, True), (self.username, 256, True),
                (self.password, 1024, True), (self.category, 128, False)):
            if (not isinstance(value, str) or len(value) > maximum or required and not value.strip()
                    or any(ord(c) < 32 for c in value)):
                raise DownloadFailure(E.CONFIGURATION)
        if ':' in self.username or self.password in self.name or self.password in self.url:
            raise DownloadFailure(E.CONFIGURATION)

    @property
    def protocol(self):
        return 'torrent' if self.kind == 'qbittorrent' else 'nzb'

    @property
    def instance(self):
        return sha256((self.key + '\n' + self.kind + '\n' + endpoint(self.url)).encode()).hexdigest()

    @property
    def api_key(self):
        """Private credential guard used by the existing selected-intent service."""
        return self.password

    def preview(self):
        from dataclasses import asdict
        return dict(id=self.key, name=self.name, url=self.url, kind=self.kind,
            protocol=self.protocol, enabled=self.enabled, category=self.category,
            priority=self.priority, username_present=bool(self.username),
            password_present=bool(self.password), retention=asdict(self.retention),
            revision=self.revision)

    @property
    def revision(self):
        from dataclasses import asdict
        from json import dumps
        return sha256(dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def retention_evaluation(policy, *, current_ratio, seeding_seconds, requirements=None):
    """Pure policy eligibility, not permission to delete. Safe-import gates follow.

    User and tracker predicates are conjoined, preserving both/either semantics;
    merging numeric maxima alone incorrectly weakens some AND/OR combinations.
    """
    if policy.mode in ('client_managed', 'keep'):
        return dict(eligible=False, reason=policy.mode, user_satisfied=False, tracker_satisfied=False)
    try:
        observed_ratio = ratio(current_ratio)
        if type(seeding_seconds) is not int or seeding_seconds < 0:
            raise DownloadFailure(E.CONFIGURATION)
    except DownloadFailure:
        return dict(eligible=False, reason='observation_unavailable', user_satisfied=False, tracker_satisfied=False)

    def satisfied(mode, target, seconds):
        r = target is not None and observed_ratio >= ratio(target)
        t = seconds is not None and seeding_seconds >= seconds
        return {'ratio': r, 'seedtime': t, 'either': r or t, 'both': r and t, 'after_import': True}.get(mode, False)

    user = satisfied(policy.mode, policy.ratio_target, policy.seed_seconds)
    tracker = True
    source = requirements or {}
    if policy.respect_minimums and source:
        mode = source.get('seedtype', 'either')
        target, seconds = source.get('minimumratio'), source.get('minimumseedtime')
        try:
            if (mode not in ('ratio', 'seedtime', 'both', 'either') or source.get('invalid')
                    or seconds is not None and (type(seconds) is not int or not 0 <= seconds <= 315360000)
                    or mode in ('ratio', 'both') and target is None
                    or mode in ('seedtime', 'both') and seconds is None):
                raise DownloadFailure(E.CONFIGURATION)
            tracker = satisfied(mode, target, seconds)
        except DownloadFailure:
            tracker = False
    return dict(eligible=user and tracker, reason='requirements_satisfied' if user and tracker else
                'tracker_requirements' if not tracker else 'user_requirements',
                user_satisfied=user, tracker_satisfied=tracker)

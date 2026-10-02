"""Detached history query contracts, not execution permissions."""

from dataclasses import dataclass
from typing import Optional

POLICY = 'maintenance-history/v1'
DOMAINS = ('organization', 'metadata_repair', 'provider_switch', 'content_claim',
           'content_coverage', 'intake')
OPERATIONS = ('archive_normalization', 'rename', 'folder_organization', 'comicinfo_repair',
              'duplicate_quarantine', 'duplicate_restore', 'local_organization',
              'metadata_repair', 'provider_switch', 'content_claim',
              'content_coverage', 'intake', 'unsupported_history')
STATES = ('pending', 'active', 'complete', 'failed', 'recovery_required', 'unknown')
CAPABILITIES = ('unchecked', 'unsupported', 'fresh_operation_required')


class HistoryError(ValueError):
    """Only controlled codes cross the history service boundary."""


@dataclass(frozen=True)
class HistoryFilter:
    domain: Optional[str] = None
    operation: Optional[str] = None
    state: Optional[str] = None
    inverse: Optional[str] = None
    volume_id: Optional[int] = None
    file_id: Optional[int] = None
    batch_id: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None

    def __post_init__(self):
        for value, allowed in ((self.domain, DOMAINS), (self.operation, OPERATIONS),
                               (self.state, STATES), (self.inverse, CAPABILITIES)):
            if value is not None and value not in allowed:
                raise HistoryError('invalid_history_filter')
        for value in (self.volume_id, self.file_id):
            if value is not None and (type(value) is not int or value <= 0):
                raise HistoryError('invalid_history_filter')
        if self.batch_id is not None and (not isinstance(self.batch_id, str)
                                         or not 1 <= len(self.batch_id) <= 512):
            raise HistoryError('invalid_history_filter')
        for value in (self.since, self.until):
            if value is not None:
                # A fixed UTC millisecond representation sorts lexically.
                from datetime import datetime
                try:
                    parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')
                    if parsed.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' != value:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise HistoryError('invalid_history_date') from None
        if self.since and self.until and self.since > self.until:
            raise HistoryError('invalid_history_date')


@dataclass(frozen=True)
class HistoryCursor:
    time: str
    domain: str
    identifier: str
    filters: HistoryFilter
    version: str = POLICY

    def __post_init__(self):
        if (self.version != POLICY or self.domain not in DOMAINS
                or not isinstance(self.time, str) or len(self.time) != 24
                or not isinstance(self.identifier, str) or not 1 <= len(self.identifier) <= 128
                or not isinstance(self.filters, HistoryFilter)):
            raise HistoryError('invalid_history_cursor')
        HistoryFilter(since=self.time)


def page_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HistoryError('invalid_history_page')

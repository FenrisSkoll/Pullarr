"""Advisory filesystem evidence; never import or execution commands."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

MONITOR_POLICY = 'kapowarr-folder-monitor/v1'


class ChangeKind(Enum):
    APPEARED = 'appeared'
    MODIFIED = 'modified'
    DISAPPEARED = 'disappeared'
    MOVED = 'moved_hint'
    DIRECTORY = 'directory_changed'
    OVERFLOW = 'event_loss'
    UNAVAILABLE = 'root_unavailable'
    AVAILABLE = 'root_available'
    RECONCILE = 'reconciliation_requested'


class ObservationSource(Enum):
    POLLING = 'polling'
    STARTUP = 'startup_reconciliation'
    RECONCILIATION = 'requested_reconciliation'
    NOTIFICATION = 'notification_hint'


@dataclass(frozen=True)
class MonitorObservation:
    root_id: int
    path: str
    kind: ChangeKind
    observed_at: float
    source: ObservationSource
    generation: int = 0
    old_path: Optional[str] = None


@dataclass(frozen=True)
class PathStamp:
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    directory: bool = False


@dataclass(frozen=True)
class MonitorLimits:
    scan_interval: int = 300
    stability_interval: int = 30
    entries_per_tick: int = 512
    work_per_tick: int = 8
    max_entries: int = 250000
    max_depth: int = 64

    def __post_init__(self) -> None:
        if min(self.scan_interval, self.stability_interval, self.entries_per_tick,
               self.work_per_tick, self.max_entries, self.max_depth) <= 0:
            raise ValueError('Monitoring limits must be positive')


@dataclass(frozen=True)
class ReevaluationResult:
    status: str
    reason: str
    job_id: Optional[str] = None

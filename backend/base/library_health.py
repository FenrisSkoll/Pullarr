"""Read-only observations, never repair intent or publication equivalence."""

import json
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Any, Optional, Tuple

POLICY = 'kapowarr-library-health/v1'


class HealthLevel(str, Enum):
    INVENTORY = 'inventory'
    ARCHIVE = 'archive'
    DEEP = 'deep'


class HealthSeverity(str, Enum):
    ERROR = 'error'
    WARNING = 'warning'
    DEVIATION = 'policy_deviation'
    INFORMATION = 'informational'


class InspectionStatus(str, Enum):
    COMPLETE = 'complete'
    PARTIAL = 'partial'
    UNAVAILABLE = 'unavailable'
    UNSUPPORTED = 'unsupported'
    SKIPPED = 'skipped_by_policy'
    FAILED = 'failed'
    BOUNDED = 'bounded'
    CANCELLED = 'cancelled'


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def fingerprint(value: Any) -> str:
    return sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class HealthScope:
    kind: str = 'library'
    ids: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if (self.kind not in ('library', 'root', 'volumes')
                or type(self.ids) is not tuple or len(self.ids) > 1000
                or any(type(i) is not int or i <= 0 for i in self.ids)
                or len(set(self.ids)) != len(self.ids)
                or (self.kind == 'library' and self.ids)
                or (self.kind == 'root' and len(self.ids) != 1)
                or (self.kind == 'volumes' and not self.ids)):
            raise ValueError('Invalid health scope')


@dataclass(frozen=True)
class HealthLimits:
    rows: int = 20000
    files: int = 10000
    entries: int = 20000
    archives: int = 100
    hashes: int = 100
    hash_bytes: int = 1024 * 1024 * 1024
    findings: int = 2000
    result_bytes: int = 8 * 1024 * 1024
    seconds: int = 120

    def __post_init__(self) -> None:
        ceilings = (20000, 10000, 20000, 1000, 1000, 8 * 1024**3, 10000, 32 * 1024**2, 600)
        for value, ceiling in zip(self.__dict__.values(), ceilings):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError('Invalid health limit')


@dataclass(frozen=True)
class HealthFinding:
    id: str
    category: str
    code: str
    severity: HealthSeverity
    inspection: InspectionStatus
    level: HealthLevel
    volume_id: Optional[int]
    issue_ids: Tuple[int, ...]
    file_id: Optional[int]
    path: Optional[str]
    explanation: str
    evidence_json: str
    state_digest: str
    provenance: str = POLICY
    actionable_later: bool = False
    repair_known: bool = False

    def view(self) -> dict:
        value = dict(self.__dict__)
        value['evidence'] = json.loads(value.pop('evidence_json'))
        return value


@dataclass(frozen=True)
class HealthReport:
    id: str
    scope: HealthScope
    level: HealthLevel
    started_at: str
    completed_at: str
    state: InspectionStatus
    reasons: Tuple[str, ...]
    counts_json: str
    findings: Tuple[HealthFinding, ...]
    state_digest: str
    policy: str = POLICY

    def summary(self) -> dict:
        return dict(id=self.id, scope=dict(kind=self.scope.kind, ids=self.scope.ids),
                    level=self.level.value, started_at=self.started_at, completed_at=self.completed_at,
                    state=self.state.value, reasons=self.reasons, counts=json.loads(self.counts_json),
                    state_digest=self.state_digest, policy=self.policy,
                    message='Observations only; unrequested probes do not establish health.')

    def page(self, *, offset: int = 0, limit: int = 50, category: Optional[str] = None,
             severity: Optional[HealthSeverity] = None, volume_id: Optional[int] = None,
             inspection: Optional[InspectionStatus] = None, actionable: Optional[bool] = None) -> dict:
        if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid health page')
        if severity is not None and not isinstance(severity, HealthSeverity):
            raise ValueError('Invalid severity')
        if inspection is not None and not isinstance(inspection, InspectionStatus):
            raise ValueError('Invalid inspection state')
        if volume_id is not None and (type(volume_id) is not int or volume_id <= 0):
            raise ValueError('Invalid volume')
        if actionable is not None and type(actionable) is not bool:
            raise ValueError('Invalid actionable filter')
        if category is not None and category not in ('filesystem', 'association', 'archive', 'comicinfo', 'identity', 'policy', 'duplicate', 'metadata'):
            raise ValueError('Invalid category')
        selected = tuple(f for f in self.findings if
            (category is None or f.category == category) and (severity is None or f.severity == severity)
            and (volume_id is None or f.volume_id == volume_id)
            and (inspection is None or f.inspection == inspection)
            and (actionable is None or f.actionable_later == actionable))
        return dict(total=len(selected), offset=offset, limit=limit,
                    findings=[f.view() for f in selected[offset:offset + limit]])

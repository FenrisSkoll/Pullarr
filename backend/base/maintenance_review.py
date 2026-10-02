"""Observation, reviewed intent and execution are deliberately separate."""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from backend.base.library_health import (HealthFinding,
                                         HealthReport, fingerprint)

POLICY = 'kapowarr-maintenance-review/v1'


class ReviewError(ValueError):
    """Controlled reason codes only; never provider/OS exception text."""


class Action(str, Enum):
    NONE = 'no_action'
    ACKNOWLEDGE = 'acknowledge'
    LATER = 'review_later'
    METADATA = 'metadata_repair'
    COMICINFO = 'comicinfo_repair'
    RENAME = 'rename'
    FOLDER = 'folder_organization'
    ASSOCIATION = 'association_repair'
    DUPLICATE = 'duplicate_review'
    KEEP = 'keep_both'


class Capability(str, Enum):
    PREVIEW = 'supported_preview'
    LATER = 'supported_later'
    BLOCKED = 'blocked'
    NOT_APPLICABLE = 'not_applicable'
    UNSUPPORTED = 'unsupported'
    STALE = 'stale'
    UNAVAILABLE = 'unavailable'


@dataclass(frozen=True)
class FindingFilter:
    category: Optional[str] = None
    severity: Optional[str] = None
    volume_id: Optional[int] = None
    inspection: Optional[str] = None
    selected: Optional[bool] = None
    excluded: Optional[bool] = None
    capability: Optional[str] = None

    def __post_init__(self):
        allowed = dict(category=('filesystem', 'association', 'archive', 'comicinfo', 'identity', 'policy', 'duplicate', 'metadata'),
                       severity=('error', 'warning', 'policy_deviation', 'informational'),
                       inspection=('complete', 'partial', 'unavailable', 'unsupported', 'skipped_by_policy', 'failed', 'bounded', 'cancelled'),
                       capability=tuple(c.value for c in Capability))
        for key, values in allowed.items():
            value = getattr(self, key)
            if value is not None and value not in values:
                raise ReviewError('invalid_filter')
        if self.volume_id is not None and (type(self.volume_id) is not int or self.volume_id <= 0):
            raise ReviewError('invalid_filter')
        if any(v is not None and type(v) is not bool for v in (self.selected, self.excluded)):
            raise ReviewError('invalid_filter')

    def matches(self, item: 'ReviewItem') -> bool:
        f = item.finding
        return all(expected is None or expected == actual for expected, actual in (
            (self.category, f.category), (self.severity, f.severity.value), (self.volume_id, f.volume_id),
            (self.inspection, f.inspection.value), (self.selected, item.selected),
            (self.excluded, item.excluded), (self.capability, item.capability.value)))


@dataclass(frozen=True)
class Edit:
    finding_id: str
    selected: bool
    excluded: bool
    action: Action

    def __post_init__(self):
        if (not isinstance(self.finding_id, str) or len(self.finding_id) != 64
                or type(self.selected) is not bool or type(self.excluded) is not bool
                or self.selected and self.excluded or not isinstance(self.action, Action)):
            raise ReviewError('invalid_edit')


@dataclass(frozen=True)
class ReviewItem:
    finding: HealthFinding
    selected: bool = False
    excluded: bool = False
    action: Action = Action.NONE
    capability: Capability = Capability.NOT_APPLICABLE
    blockers: Tuple[str, ...] = ()
    preview_json: str = '{}'
    freshness_json: str = '{}'
    recovery: str = 'No execution or undo is authorized.'

    def view(self):
        return dict(finding=self.finding.view(), selected=self.selected, excluded=self.excluded,
                    action=self.action.value, capability=self.capability.value, blockers=self.blockers,
                    preview=json.loads(self.preview_json), freshness=json.loads(self.freshness_json),
                    recovery=self.recovery, apply_available=False)


@dataclass(frozen=True)
class Worklist:
    id: str
    report: HealthReport
    created_at: float
    expires_at: float
    revision: int
    items: Tuple[ReviewItem, ...]
    collisions_json: str = '[]'
    selection_filter_json: str = '{}'

    @property
    def manifest_digest(self):
        return fingerprint(dict(policy=POLICY, id=self.id, scan=self.report.id,
            snapshot=self.report.state_digest, revision=self.revision,
            items=[dict(id=i.finding.id, evidence=i.finding.state_digest, selected=i.selected,
                excluded=i.excluded, action=i.action.value, capability=i.capability.value,
                blockers=i.blockers, preview=i.preview_json, freshness=i.freshness_json)
                for i in self.items], collisions=self.collisions_json, filter=self.selection_filter_json))

    def summary(self):
        selected = [i for i in self.items if i.selected]
        status = ('stale' if any(i.capability == Capability.STALE for i in selected) else
                  'blocked' if any(i.blockers for i in selected) else 'reviewable')
        return dict(id=self.id, scan_id=self.report.id, snapshot_digest=self.report.state_digest,
                    revision=self.revision, manifest_digest=self.manifest_digest, policy=POLICY,
                    created_at=self.created_at, expires_at=self.expires_at, status=status,
                    source_completeness=self.report.state.value, source_reasons=self.report.reasons,
                    selected=len(selected), excluded=sum(i.excluded for i in self.items),
                    total=len(self.items), apply_available=False,
                    batch_semantics='Reviewed together; future independent jobs can complete partially. No filesystem-wide atomicity.')

    def page(self, offset=0, limit=50, filters: FindingFilter = FindingFilter()):
        validate_page(offset, limit)
        if not isinstance(filters, FindingFilter):
            raise ReviewError('invalid_filter')
        rows = [i for i in self.items if filters.matches(i)]
        return dict(total=len(rows), offset=offset, limit=limit, revision=self.revision,
                    items=[i.view() for i in rows[offset:offset + limit]])

    def collisions(self, offset=0, limit=50):
        validate_page(offset, limit)
        rows = json.loads(self.collisions_json)
        return dict(total=len(rows), offset=offset, limit=limit, items=rows[offset:offset + limit])


def validate_page(offset, limit):
    if type(offset) is not int or not 0 <= offset <= 2000 or type(limit) is not int or not 1 <= limit <= 100:
        raise ReviewError('invalid_page')

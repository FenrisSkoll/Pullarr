"""Duplicate observations and operator choices, never execution permission."""

import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Tuple

from backend.base.library_health import fingerprint

POLICY = 'kapowarr-duplicate-review/v1'


class DuplicateReviewError(ValueError):
    """Controlled local reason, not an OS/provider response."""


class DuplicateKind(str, Enum):
    EXACT = 'exact_bytes'
    PUBLICATION = 'same_direct_publication'
    COVERAGE = 'overlapping_content_coverage'
    WEAK = 'probable_duplicate'
    COLLISION = 'path_collision'


class DuplicateAction(str, Enum):
    KEEP = 'keep_all'
    ACKNOWLEDGE = 'acknowledge'
    LATER = 'review_later'
    QUARANTINE = 'quarantine_selected'


@dataclass(frozen=True)
class DuplicateChoice:
    group_id: str
    action: DuplicateAction
    quarantine: Tuple[int, ...] = ()

    def __post_init__(self):
        if (type(self.group_id) is not str or len(self.group_id) != 64
                or not isinstance(self.action, DuplicateAction)
                or type(self.quarantine) is not tuple
                or any(type(i) is not int or i <= 0 for i in self.quarantine)
                or len(set(self.quarantine)) != len(self.quarantine)
                or bool(self.quarantine) != (self.action == DuplicateAction.QUARANTINE)):
            raise DuplicateReviewError('invalid_duplicate_choice')


@dataclass(frozen=True)
class DuplicateGroup:
    id: str
    kind: DuplicateKind
    finding_ids: Tuple[str, ...]
    file_ids: Tuple[int, ...]
    evidence_json: str
    blockers: Tuple[str, ...] = ()
    action: DuplicateAction = DuplicateAction.LATER
    quarantine: Tuple[int, ...] = ()
    impact_json: str = '[]'

    def summary(self):
        return dict(id=self.id, kind=self.kind.value, finding_ids=self.finding_ids,
                    files=len(self.file_ids), action=self.action.value,
                    quarantine=self.quarantine, blockers=self.blockers,
                    capability=('blocked' if self.blockers else 'quarantine')
                    if self.action == DuplicateAction.QUARANTINE else 'review_only',
                    apply_available=self.action == DuplicateAction.QUARANTINE and not self.blockers)


def page_args(offset, limit):
    if type(offset) is not int or not 0 <= offset <= 2000 or type(limit) is not int or not 1 <= limit <= 100:
        raise DuplicateReviewError('invalid_duplicate_page')


@dataclass(frozen=True)
class DuplicateReview:
    id: str
    origin: tuple
    revision: int
    expires_at: float
    source_completeness: str
    source_reasons: tuple
    state_digest: str
    files_json: str
    ownership_json: str
    groups: Tuple[DuplicateGroup, ...]
    hash_bytes: int
    stale: bool = False
    execution_json: str = '[]'

    @property
    def digest(self):
        return fingerprint(dict(policy=POLICY, **asdict(self)))

    def summary(self):
        return dict(id=self.id, origin=self.origin, revision=self.revision, digest=self.digest,
                    expires_at=self.expires_at, groups=len(self.groups), hash_bytes=self.hash_bytes,
                    source_completeness=self.source_completeness, source_reasons=self.source_reasons,
                    status='stale' if self.stale else 'ready' if self.execution_json != '[]' else 'review_only',
                    apply_available=not self.stale and self.execution_json != '[]',
                    recovery='Conditional journaled restore; no permanent purge.' if self.execution_json != '[]'
                    else 'Quarantine execution evidence has not been prepared; removal unavailable.')

    def page(self, offset=0, limit=50):
        page_args(offset, limit)
        return dict(total=len(self.groups), offset=offset, limit=limit,
                    items=[g.summary() for g in self.groups[offset:offset + limit]])

    def detail(self, group_id, offset=0, limit=50):
        page_args(offset, limit)
        group = next((g for g in self.groups if g.id == group_id), None)
        if group is None:
            raise DuplicateReviewError('unknown_duplicate_group')
        files = {f['id']: f for f in json.loads(self.files_json)}
        page_ids = group.file_ids[offset:offset + limit]
        execution = [dict(file_id=i['file_id'], source=i['source'], target=i['target'],
                          retained_file_ids=i['retained_ids'], same_device=i['location']['device'],
                          direct_before=[r for r in i['quarantine_before']['direct'] if r['file_id'] == i['file_id']],
                          general_before=[r for r in i['quarantine_before']['general'] if r['file_id'] == i['file_id']],
                          effects=i['effects'], recovery='conditional_restore_no_purge')
                     for i in json.loads(self.execution_json)
                     if i['group_id'] == group_id and i['file_id'] in page_ids]
        return dict(group=group.summary(), evidence=json.loads(group.evidence_json),
                    impact=json.loads(group.impact_json), total=len(group.file_ids),
                    offset=offset, limit=limit,
                    members=[files[i] for i in page_ids], quarantine_execution=execution,
                    meaning='Content coverage is not duplicate publication ownership; bytes imply no quality winner.')

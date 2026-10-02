"""Frozen reviewed directory intent. A worklist is provenance, not permission."""

import json
from dataclasses import asdict, dataclass
from typing import Tuple

from backend.base.folder_inventory import FolderInventory
from backend.base.library_health import fingerprint
from backend.internals.folder_ownership import FolderOwnership

POLICY = 'kapowarr-bulk-folder/v1'


class FolderReviewError(ValueError):
    pass


@dataclass(frozen=True)
class FolderItem:
    finding_id: str
    ownership: FolderOwnership
    inventory: FolderInventory
    target: str
    custom_after: bool
    policy_json: str
    target_json: str
    blockers: Tuple[str, ...]
    journal_bytes: int = 0

    @property
    def no_changes(self):
        return self.target == self.ownership.source and self.custom_after == self.ownership.custom


@dataclass(frozen=True)
class FolderReview:
    id: str
    origin: tuple
    items: Tuple[FolderItem, ...]
    selected: Tuple[str, ...]
    revision: int
    expires_at: float
    collisions_json: str

    @property
    def digest(self):
        return fingerprint(dict(policy=POLICY, **asdict(self)))

    def page(self, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise FolderReviewError('invalid_page')
        return dict(id=self.id, origin=self.origin, revision=self.revision, digest=self.digest,
            selected=self.selected, total=len(self.items), expires_at=self.expires_at,
            collisions=json.loads(self.collisions_json),
            items=[dict(finding_id=i.finding_id, volume_id=i.ownership.volume_id,
                source=i.ownership.source, target=i.target, root_id=i.ownership.root_id,
                custom_before=i.ownership.custom, custom_after=i.custom_after, blockers=i.blockers,
                inventory_count=len(i.inventory.entries), registered_count=len(i.ownership.registrations),
                inventory_digest=i.inventory.digest, authority=asdict(i.ownership.authority),
                journal_bytes=i.journal_bytes,
                policy=json.loads(i.policy_json), target_observation=json.loads(i.target_json),
                state='blocked' if i.blockers else 'no_changes' if i.no_changes else 'reviewable',
                recovery='Journaled forward reconciliation; inverse subject to fresh tree/DB/path checks.')
                for i in self.items[offset:offset + limit]])

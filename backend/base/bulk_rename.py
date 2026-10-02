"""Frozen filename-only execution review; never client-supplied plans."""

import json
from dataclasses import dataclass
from typing import Tuple

from backend.base.library_health import HealthScope, fingerprint
from backend.base.organization_plan import OrganizationPlan
from backend.internals.provider_authority import AuthorityToken

POLICY = 'kapowarr-maintenance-rename/v1'


class RenameReviewError(ValueError):
    """Controlled codes, not filesystem or provider exception payloads."""


@dataclass(frozen=True)
class RenameItem:
    finding_id: str
    plan: OrganizationPlan
    authority: AuthorityToken
    state_digest: str
    source_stamp: str
    destination_stamp: str
    naming_evidence: str
    blockers: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RenameReview:
    id: str
    origin: tuple
    scope: HealthScope
    items: Tuple[RenameItem, ...]
    selected: Tuple[str, ...]
    revision: int
    expires_at: float
    collisions_json: str

    @property
    def digest(self):
        return fingerprint((POLICY, self.id, self.origin, repr(self.items), self.selected,
                            self.revision, self.collisions_json))

    def page(self, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise RenameReviewError('invalid_page')
        from backend.implementations.organization_plan import preview_plan
        return dict(id=self.id, revision=self.revision, digest=self.digest, total=len(self.items),
            selected=self.selected, origin=self.origin, expires_at=self.expires_at,
            collisions=json.loads(self.collisions_json),
            items=[dict(finding_id=i.finding_id, selected=i.finding_id in self.selected,
                authority=repr(i.authority), blockers=i.blockers, preview=preview_plan(i.plan),
                state='no_changes' if i.plan.source_path == i.plan.target_path else 'reviewable',
                recovery='Existing journal; conditional inverse requires later artifact/path/database checks.')
                for i in self.items[offset:offset + limit]])

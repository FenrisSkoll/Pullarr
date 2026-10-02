"""Provider-neutral rich acquisition input, separate from legacy issue DTOs.

A receipt proves the stated acquisition policy, not an atomic remote database
snapshot. No receipt here authorizes deletion of locally dependent issues.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Optional, Tuple

from backend.base.bibliography import IssueBibliography, PublicationFacts
from backend.base.issue_facts import IssueFacts, VariantOf
from backend.implementations.metadata.models import VolumeMetadata


@dataclass(frozen=True)
class SnapshotIssue:
    provider: str
    provider_id: str
    parent_id: str
    title: Optional[str]
    facts: IssueFacts
    variant_of: Optional[VariantOf] = None
    bibliography: Optional[IssueBibliography] = None

    @property
    def legacy_number(self) -> Optional[float]:
        number = self.facts.number.numeric
        if number is None:
            return None
        result = float(number)
        return result if isfinite(result) and Decimal.from_float(result) == number else None


@dataclass(frozen=True)
class SnapshotReceipt:
    policy: str
    membership_digest: str
    acquired_at: float
    expected_count: int


@dataclass(frozen=True)
class ProviderVolumeSnapshot:
    volume: VolumeMetadata
    issues: Tuple[SnapshotIssue, ...]
    receipt: SnapshotReceipt
    publication: Optional[PublicationFacts] = None

    def __post_init__(self) -> None:
        volume = self.volume
        ids = {i.provider_id for i in self.issues}
        if (volume.issues is not None or len(ids) != len(self.issues)
                or volume.issue_count != len(ids) or self.receipt.expected_count != len(ids)
                or any(i.provider != volume.provider or i.parent_id != volume.provider_id
                       or not i.provider_id or (i.bibliography is not None and
                           i.bibliography.provider != i.provider) for i in self.issues)):
            raise ValueError('Incoherent provider snapshot')
        for issue in self.issues:
            relation = issue.variant_of
            if relation is not None and (relation.provider != volume.provider
                    or relation.provider_id not in ids or relation.provider_id == issue.provider_id):
                raise ValueError('Unresolved snapshot variant relationship')
        parents = {i.provider_id: i.variant_of.provider_id for i in self.issues if i.variant_of}
        checked = set()
        for start in parents:
            visiting = set()
            current = start
            while current in parents and current not in checked:
                if current in visiting:
                    raise ValueError('Cyclic variant relationship')
                visiting.add(current)
                current = parents[current]
            checked.update(visiting)


class MetadataSnapshotProvider(ABC):
    """Independent add/refresh capability; it does not fabricate IssueMetadata."""

    scheduled_volume_limit = 1

    @abstractmethod
    async def fetch_snapshot(self, provider_id: str) -> ProviderVolumeSnapshot:
        ...

    async def fetch_snapshot_scheduled(self, provider_id: str) -> ProviderVolumeSnapshot:
        return await self.fetch_snapshot(provider_id)

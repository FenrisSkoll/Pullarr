"""Optional identity assertions, separate from serialized core metadata."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple

from typing_extensions import Literal

from backend.base.issue_facts import IssueFacts, VariantOf
from backend.implementations.metadata.format_evidence import \
    ProviderFormatEvidence
from backend.implementations.metadata.models import VolumeMetadata
from backend.implementations.metadata.provider import MetadataVolumeProvider
from backend.implementations.metadata.publication_evidence import \
    ProviderPublicationEvidence
from backend.implementations.metadata.snapshot import (
    MetadataSnapshotProvider, ProviderVolumeSnapshot)


@dataclass(frozen=True)
class IdentityAssertion:
    """A source assertion about one externally identified canonical entity.

    Owner IDs are provider IDs, not local database keys. The entity kind keeps
    volume and issue namespaces distinct. Provenance identifies the supplying
    source; it does not imply independent verification or confer authority.
    """

    entity: Literal['volume', 'issue']
    owner_provider: str
    owner_id: str
    provider: str
    provider_id: str
    provenance: str


@dataclass(frozen=True)
class ProviderIssueFacts:
    provider: str
    provider_id: str
    parent_id: str
    facts: IssueFacts
    variant_of: Optional[VariantOf] = None


@dataclass(frozen=True)
class VolumeFetchResult:
    metadata: VolumeMetadata
    enrichment: Tuple[IdentityAssertion, ...]
    format_evidence: Optional[ProviderFormatEvidence] = None
    publication_evidence: Optional[ProviderPublicationEvidence] = None
    issue_facts: Tuple[ProviderIssueFacts, ...] = ()
    snapshot: Optional[ProviderVolumeSnapshot] = None


class MetadataEnrichmentProvider(ABC):
    """Optional full-fetch capability; no last-response state or extra lookup.

    Ordinary providers keep fetch_volume unchanged. Consumers of this capability
    explicitly persist the metadata and resolve assertion owners to local keys
    before invoking conflict-detecting identity storage. An absent assertion is
    not a request to delete an existing reference or change selected authority.
    """

    @abstractmethod
    async def fetch_volume_enriched(self, provider_id: str) -> VolumeFetchResult:
        ...


async def fetch_volume_result(provider: MetadataVolumeProvider | MetadataSnapshotProvider, provider_id: str) -> VolumeFetchResult:
    if isinstance(provider, MetadataSnapshotProvider):
        snapshot = await provider.fetch_snapshot(provider_id)
        return VolumeFetchResult(snapshot.volume, (), snapshot=snapshot)
    if isinstance(provider, MetadataEnrichmentProvider):
        return await provider.fetch_volume_enriched(provider_id)
    return VolumeFetchResult(await provider.fetch_volume(provider_id), ())


class MetadataScheduledProvider(ABC):
    """Optional capacity-bounded single-volume scheduling, not bulk fetching."""

    @abstractmethod
    async def fetch_volume_scheduled(self, provider_id: str) -> VolumeFetchResult:
        ...

"""Independent, deliberately narrow metadata provider capabilities."""

from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata,
                                                     VolumeSearchResult)


class MetadataSearchProvider(ABC):
    search_label = ''
    search_result_limit = 250
    search_supports_year = False

    def search_unavailable(self) -> Optional[str]:
        """Aggregate preflight only; no network and no credential exposure."""
        return None

    async def search_aggregate(self, query: str, year: Optional[int] = None) -> List[VolumeSearchResult]:
        """Opt-in bounded orchestration seam; legacy search is unchanged."""
        return await self.search_volumes(query)

    @abstractmethod
    async def search_volumes(self, query: str) -> List[VolumeSearchResult]:
        """Search without filtering translations or reordering candidates.

        Providers interpret their own supported query syntax. An empty list
        means no matches. Errors propagate to the caller; this first boundary
        deliberately does not redefine existing exception semantics.
        """
        ...


class MetadataArtworkProvider(ABC):
    """One optional, charged metadata request; never a full volume/issue fetch."""

    @abstractmethod
    def search_artwork_url(self, provider_id: str, hint: str) -> Optional[str]:
        ...


class MetadataBulkVolumeProvider(ABC):
    """Optional two-stage batch fetching, independent of single-volume fetch.

    Callers can select which volumes need issues after inspecting volume
    metadata. No fallback to repeated single fetches is implied: their failure,
    completeness and request-cost semantics may differ.
    """

    @abstractmethod
    async def fetch_volumes(
        self, provider_ids: Sequence[str]
    ) -> List[VolumeMetadata]:
        """Fetch volume metadata/covers, with issues=None (not fetched).

        Missing results are allowed; preserve result order. This is not a
        promise that all requested identities were successfully fetched.
        """
        ...

    @abstractmethod
    async def fetch_issues(
        self, volume_provider_ids: Sequence[str]
    ) -> List[IssueMetadata]:
        """Fetch available issues for volumes; partial/empty results allowed.

        Preserve provider order, parent namespaces and existing exceptions.
        Missing results alone are never proof that local issues were deleted.
        """
        ...


class MetadataVolumeProvider(ABC):
    @abstractmethod
    async def fetch_volume(self, provider_id: str) -> VolumeMetadata:
        """Fetch one volume, its cover and available issue metadata.

        The ID is an opaque string in this provider's namespace. Returned
        volume and issue identities retain that namespace. Issue count does
        not guarantee completeness; do not infer deletion from missing issues.
        Existing provider exceptions propagate without translation.
        """
        ...

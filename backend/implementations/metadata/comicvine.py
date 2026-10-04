"""Adapters retaining the existing ComicVine integration behavior."""

from typing import List, Sequence, Union

from backend.base.definitions import (IssueMetadata as CVIssueMetadata,
                                      VolumeMetadata as CVVolumeMetadata)
from backend.implementations.comicvine import ComicVine
from backend.implementations.metadata.enrichment import (
    MetadataEnrichmentProvider, ProviderIssueFacts, VolumeFetchResult)
from backend.implementations.metadata.models import (IssueMetadata,
                                                     PublicationRelation,
                                                     VolumeMetadata,
                                                     VolumeSearchResult)
from backend.implementations.metadata.provider import (
    MetadataBulkVolumeProvider, MetadataSearchProvider, MetadataVolumeProvider)
from backend.implementations.metadata.switch_target import \
    MetadataReviewProvider
from backend.internals.issue_facts import mapped_facts


class ComicVineMetadataProvider(
    MetadataSearchProvider, MetadataVolumeProvider, MetadataBulkVolumeProvider,
    MetadataEnrichmentProvider, MetadataReviewProvider
):
    search_label = 'ComicVine'
    search_result_limit = 50

    def search_unavailable(self):
        from backend.internals.settings import Settings
        return None if Settings().sv.comicvine_api_key.strip() else 'auth_required'

    async def search_aggregate(self, query, year=None):
        return self._search_results(await ComicVine(safe_search_errors=True).search_volumes(query))

    def __init__(self) -> None:
        # One settings/key/date snapshot across the two refresh fetch stages.
        # Lazy construction retains credential-free capability resolution.
        self._bulk_client: Union[ComicVine, None] = None

    def _get_bulk_client(self) -> ComicVine:
        if self._bulk_client is None:
            self._bulk_client = ComicVine()
        return self._bulk_client

    async def search_volumes(self, query: str) -> List[VolumeSearchResult]:
        """Delegate requests, normalization, local lookup and errors unchanged.

        Settings and status handling remain in ComicVine. In particular, do
        not enable its import-only allow_rate_limit_reached option here.
        """
        results = await ComicVine().search_volumes(query)
        return self._search_results(results)

    @staticmethod
    def _search_results(results):
        return [VolumeSearchResult(
            provider='comicvine',
            provider_id=str(result['comicvine_id']),
            title=result['title'],
            year=result['year'],
            volume_number=result['volume_number'],
            cover_link=result['cover_link'],
            description=result['description'],
            site_url=result['site_url'],
            aliases=result['aliases'],
            publisher=result['publisher'],
            issue_count=result['issue_count'],
            translated=result['translated'],
            already_added=result['already_added'],
            relations=[PublicationRelation(**r) for r in result.get('search_relations', [])]
        ) for result in results]

    async def fetch_volume(self, provider_id: str) -> VolumeMetadata:
        """Reuse normalization, issue pagination, cover download and errors.

        In particular, the existing fetch may return partial/empty issues
        after an issue-request rate limit; do not reinterpret that behavior.
        """
        result = await ComicVine().fetch_volume(provider_id)
        metadata = self._volume_metadata(result)
        # Full fetch has attempted issues, even if nothing was returned.
        if metadata.issues is None:
            metadata.issues = []
        return metadata

    async def fetch_volume_enriched(self, provider_id: str) -> VolumeFetchResult:
        metadata = await self.fetch_volume(provider_id)
        # The established CV mapper has already selected a date and transformed
        # its label. Do not claim the discarded fields were original evidence.
        return VolumeFetchResult(metadata, (), issue_facts=tuple(
            ProviderIssueFacts('comicvine', i.provider_id, i.volume_provider_id,
                mapped_facts(i.issue_number, i.date, 'comicvine_mapped'))
            for i in metadata.issues or ()))

    async def fetch_review(self, provider_id, issue_limit):
        client = ComicVine(safe_search_errors=True, review_issue_limit=issue_limit)
        return VolumeFetchResult(self._volume_metadata(await client.fetch_volume(provider_id)), ())

    async def fetch_volumes(
        self, provider_ids: Sequence[str]
    ) -> List[VolumeMetadata]:
        results = await self._get_bulk_client().fetch_volumes(provider_ids)
        return [self._volume_metadata(result) for result in results]

    async def fetch_issues(
        self, volume_provider_ids: Sequence[str]
    ) -> List[IssueMetadata]:
        results = await self._get_bulk_client().fetch_issues(volume_provider_ids)
        return [self._issue_metadata(issue) for issue in results]

    @staticmethod
    def _issue_metadata(issue: CVIssueMetadata) -> IssueMetadata:
        return IssueMetadata(
            provider='comicvine',
            provider_id=str(issue['comicvine_id']),
            volume_provider_id=str(issue['volume_id']),
            issue_number=issue['issue_number'],
            calculated_issue_number=issue['calculated_issue_number'],
            title=issue['title'],
            date=issue['date'],
            description=issue['description']
        )

    @classmethod
    def _volume_metadata(cls, result: CVVolumeMetadata) -> VolumeMetadata:
        return VolumeMetadata(
            provider='comicvine',
            provider_id=str(result['comicvine_id']),
            title=result['title'],
            year=result['year'],
            volume_number=result['volume_number'],
            cover_link=result['cover_link'],
            cover=result['cover'],
            description=result['description'],
            site_url=result['site_url'],
            aliases=result['aliases'],
            publisher=result['publisher'],
            issue_count=result['issue_count'],
            translated=result['translated'],
            issues=(None if result['issues'] is None else [
                cls._issue_metadata(issue) for issue in result['issues']
            ])
        )

"""Synthetic providers for Collections acceptance; no accounts or HTTP."""

from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata,
                                                     VolumeSearchResult)
from backend.implementations.metadata.provider import (MetadataSearchProvider,
                                                       MetadataVolumeProvider)

HOSTILE = '<img src=x onerror="window.hostile=true">'


class FixtureProvider(MetadataSearchProvider, MetadataVolumeProvider):
    provider = 'comicvine'
    search_label = 'Fixture'
    search_result_limit = 50

    async def search_volumes(self, query):
        return [VolumeSearchResult(self.provider, str(identity), f'External {identity} {HOSTILE}', 2020, 1,
            None, None, None, [], 'Fixture publisher', 1, False, None) for identity in (900, 910)]

    async def fetch_volume(self, provider_id):
        return VolumeMetadata(self.provider, provider_id, 'External ' + provider_id, 2020, 1, None, None,
            'Fixture metadata', 'https://example.invalid', [], 'Fixture publisher', 1, False,
            [IssueMetadata(self.provider, str(int(provider_id) + 1), provider_id, '1', 1.0, 'Issue', '2020-01-01', '')])


class MetronFixture(FixtureProvider):
    provider = 'metron'


class GCDFixture(FixtureProvider):
    provider = 'gcd'

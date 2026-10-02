"""One provider registry with independent, typed capability accessors."""

from typing import Dict, Type, Union

from backend.implementations.metadata.comicvine import \
    ComicVineMetadataProvider
from backend.implementations.metadata.gcd import GcdMetadataProvider
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.metadata.provider import (
    MetadataBulkVolumeProvider, MetadataSearchProvider, MetadataVolumeProvider)
from backend.implementations.metadata.snapshot import MetadataSnapshotProvider

PROVIDERS: Dict[str, Type[Union[
    MetadataSearchProvider, MetadataVolumeProvider, MetadataBulkVolumeProvider, MetadataSnapshotProvider
]]] = {
    'comicvine': ComicVineMetadataProvider,
    'metron': MetronMetadataProvider,
    'gcd': GcdMetadataProvider}


def get_search_provider(provider: str = 'comicvine') -> MetadataSearchProvider:
    """Create a search provider by key; unknown keys raise KeyError.

    ComicVine remains the default. Explicit provider requests retain their
    namespace; the legacy public API must not publish foreign IDs as CV IDs.
    """
    instance = PROVIDERS[provider]()
    if not isinstance(instance, MetadataSearchProvider):
        raise TypeError(f'Metadata provider {provider} does not support search')
    return instance


def get_bulk_volume_provider(
    provider: str = 'comicvine'
) -> MetadataBulkVolumeProvider:
    """Resolve optional two-stage bulk fetch; no implicit per-volume fallback."""
    instance = PROVIDERS[provider]()
    if not isinstance(instance, MetadataBulkVolumeProvider):
        raise TypeError(
            f'Metadata provider {provider} does not support bulk volume fetch')
    return instance


def get_volume_provider(provider: str = 'comicvine') -> MetadataVolumeProvider | MetadataSnapshotProvider:
    """Create a single-volume provider; unknown keys raise KeyError.

    Capability checks prevent resolving a search-only provider for adding.
    Persistence accepts registered providers. Credential checks happen during
    operations, never during capability resolution.
    """
    instance = PROVIDERS[provider]()
    if not isinstance(instance, (MetadataVolumeProvider, MetadataSnapshotProvider)):
        raise TypeError(
            f'Metadata provider {provider} does not support volume fetch')
    return instance

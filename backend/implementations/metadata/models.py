"""Provider-neutral search and fetch metadata, not persisted identity."""

import re
from dataclasses import dataclass, field
from typing import List, Union

from backend.base.definitions import UnsupportedLegacyIssue


@dataclass(frozen=True)
class PublicationRelation:
    source_provider: str
    source_id: str
    relation_type: str
    target_provider: str
    target_id: str
    target_title: str
    provenance: str

    def __post_init__(self):
        if (self.source_provider not in ('comicvine', 'metron', 'gcd')
                or self.target_provider not in ('comicvine', 'metron', 'gcd')
                or self.relation_type not in ('continues_from', 'continues_as', 'related_series')
                or not re.fullmatch(r'[1-9][0-9]{0,18}', self.source_id)
                or not re.fullmatch(r'[1-9][0-9]{0,18}', self.target_id)
                or (self.source_provider, self.source_id) == (self.target_provider, self.target_id)
                or len(self.target_title) > 500 or len(self.provenance) > 100):
            raise ValueError('Invalid publication relationship')


@dataclass
class VolumeSearchResult:
    """A search candidate and its optional match to a local library volume.

    provider_id is an opaque string in the namespace identified by provider.
    already_added is a local volume primary key, never an external ID.
    Search includes a cover URL, not downloaded image bytes or issue records.
    """

    provider: str
    provider_id: str
    title: str
    year: Union[int, None]
    volume_number: int
    cover_link: Union[str, None]
    description: Union[str, None]
    site_url: Union[str, None]
    aliases: List[str]
    publisher: Union[str, None]
    issue_count: int
    translated: bool
    already_added: Union[int, None]
    relations: List[PublicationRelation] = field(default_factory=list)
    search_origin: str = 'direct'
    relation_reason: Union[PublicationRelation, None] = None
    rank_components: dict = field(default_factory=dict)
    artwork_hint: Union[str, None] = field(default=None, repr=False, compare=False)


@dataclass
class IssueMetadata:
    """An issue and its external parent, all in the provider's namespace.

    volume_provider_id is not a local database key. Titles are source titles,
    including generic titles such as TPB; they do not encode a format enum.
    """

    provider: str
    provider_id: str
    volume_provider_id: str
    issue_number: str
    calculated_issue_number: float
    title: Union[str, None]
    date: Union[str, None]
    description: Union[str, None]

    def __post_init__(self) -> None:
        if self.calculated_issue_number is None:
            raise UnsupportedLegacyIssue()


@dataclass
class VolumeMetadata:
    """Volume metadata with downloaded cover and optional issue fetch result.

    issue_count is the source's advertised count, not necessarily len(issues).
    issues=None means not fetched, unlike an attempted fetch returning [].
    Partial issue lists remain possible. Special-version determination stays
    in the application and consumes titles, description and issue dates.
    """

    provider: str
    provider_id: str
    title: str
    year: Union[int, None]
    volume_number: int
    cover_link: Union[str, None]
    cover: Union[bytes, None]
    description: Union[str, None]
    site_url: Union[str, None]
    aliases: List[str]
    publisher: Union[str, None]
    issue_count: int
    translated: bool
    issues: Union[List[IssueMetadata], None]

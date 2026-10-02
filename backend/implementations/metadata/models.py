"""Provider-neutral search and fetch metadata, not persisted identity."""

from dataclasses import dataclass
from typing import List, Union

from backend.base.definitions import UnsupportedLegacyIssue


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

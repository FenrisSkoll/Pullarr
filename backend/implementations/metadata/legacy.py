"""Explicit ComicVine-only compatibility for existing persistence inputs."""

from typing import Sequence, Tuple

from backend.base.definitions import (IssueMetadata as LegacyIssueMetadata,
                                      VolumeMetadata as LegacyVolumeMetadata)
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)


def legacy_volume_identities(
    comicvine_ids: Sequence[int]
) -> Tuple[str, Tuple[str, ...]]:
    """Bridge schema-51 volume IDs to an explicit provider-qualified batch.

    This is only for persisted comicvine_id columns, not arbitrary future IDs.
    Replace this bridge with stored source selection during identity migration.
    """
    return 'comicvine', tuple(str(identity) for identity in comicvine_ids)


def legacy_issue_metadata(issue: IssueMetadata) -> LegacyIssueMetadata:
    """Convert one issue without changing its external parent or metadata."""
    if issue.provider != 'comicvine':
        raise ValueError(
            'Legacy persistence requires ComicVine volume and issue identities')
    return {
        'comicvine_id': int(issue.provider_id),
        'volume_id': int(issue.volume_provider_id),
        'issue_number': issue.issue_number,
        'calculated_issue_number': issue.calculated_issue_number,
        'title': issue.title,
        'date': issue.date,
        'description': issue.description
    }


def legacy_volume_metadata(volume: VolumeMetadata) -> LegacyVolumeMetadata:
    """Restore legacy integer IDs, never mislabel another provider's identity.

    Validate every namespace before returning persistence input. The external
    issue parent is preserved here; Library.add replaces it with its local key.
    This mapper performs no database writes or metadata normalization.
    """
    if volume.provider != 'comicvine' or any(
        issue.provider != 'comicvine' for issue in volume.issues or []
    ):
        raise ValueError(
            'Legacy persistence requires ComicVine volume and issue identities')

    return {
        'comicvine_id': int(volume.provider_id),
        'title': volume.title,
        'year': volume.year,
        'volume_number': volume.volume_number,
        'cover_link': volume.cover_link,
        'cover': volume.cover,
        'description': volume.description,
        'site_url': volume.site_url,
        'aliases': volume.aliases,
        'publisher': volume.publisher,
        'issue_count': volume.issue_count,
        'translated': volume.translated,
        'already_added': None,
        'issues': (None if volume.issues is None else [
            legacy_issue_metadata(issue) for issue in volume.issues
        ])
    }

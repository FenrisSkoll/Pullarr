"""Neutral persistence inputs with an optional, genuine ComicVine projection."""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Optional

from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)
from backend.internals.db import KapowarrCursor
from backend.internals.issue_facts import mapped_facts, write_facts
from backend.internals.provider_identity import (ExternalIdentity,
                                                 ProviderIdentityDB)


@dataclass(frozen=True)
class ProviderVolumeIdentity:
    provider: str
    provider_id: str


def comicvine_reference(provider: str, provider_id: str) -> Optional[int]:
    if not isinstance(provider_id, str):
        raise ValueError('Provider identity must be a nonempty string')
    if not provider_id:
        raise ValueError('Provider identity must be a nonempty string')
    return int(provider_id) if provider == 'comicvine' else None


def issue_input(issue: IssueMetadata, provider: str) -> Dict[str, Any]:
    if issue.provider != provider:
        raise ValueError('Expected ComicVine volume and issue identities' if provider ==
                         'comicvine' else 'Provider issue namespace mismatch')
    result = asdict(issue)
    result['comicvine_id'] = comicvine_reference(provider, issue.provider_id)
    return result


def volume_input(volume: VolumeMetadata, provider: str) -> Dict[str, Any]:
    if volume.provider != provider:
        raise ValueError('Expected ComicVine volume and issue identities' if provider ==
                         'comicvine' else 'Provider volume namespace mismatch')
    result = asdict(volume)
    result['comicvine_id'] = comicvine_reference(provider, volume.provider_id)
    if provider != 'comicvine':
        result['site_url'] = volume.site_url or ''
        if any(i.volume_provider_id != volume.provider_id for i in volume.issues or []):
            raise ValueError('Issue parent differs from fetched volume')
    result['issues'] = None if volume.issues is None else [
        issue_input(i, provider) for i in volume.issues]
    return result


def fetch_input(result: VolumeFetchResult, provider: str) -> Dict[str, Any]:
    """Adapt rich persistence without constructing an unsupported legacy DTO."""
    data = volume_input(result.metadata, provider)
    if result.snapshot is not None:
        if result.snapshot.volume is not result.metadata:
            raise ValueError('Snapshot metadata owner mismatch')
        data['issues'] = [{
            'provider': i.provider, 'provider_id': i.provider_id,
            'volume_provider_id': i.parent_id, 'comicvine_id': None,
            'issue_number': i.facts.number.raw_label or '',
            'calculated_issue_number': i.legacy_number, 'title': i.title,
            'date': (i.facts.operational_date.exact_day.isoformat()
                     if i.facts.operational_date and i.facts.operational_date.exact_day else None),
            'description': None} for i in result.snapshot.issues]
    return data


def write_issue_batch(cursor: KapowarrCursor, sql: str,
                      values: Iterable[Dict[str, Any]], provider: str,
                      existing: Optional[Dict[str, int]] = None) -> None:
    # Preserve executemany's pending transaction/partial-success contract.
    # Releasing a top-level savepoint would otherwise commit each issue.
    if not cursor.connection.in_transaction:
        cursor.execute('BEGIN')
    if existing is None:
        existing = dict(cursor.execute('''SELECT e.provider_id,e.issue_id
            FROM issue_external_ids e JOIN issues i ON i.id=e.issue_id
            JOIN volumes v ON v.id=i.volume_id
            WHERE e.provider=? AND v.metadata_provider=?''', (provider, provider)))
    for row in values:
        cursor.execute('SAVEPOINT provider_issue')
        try:
            cursor.execute(sql, row)
            if row['provider_id'] not in existing:
                local_id = cursor.lastrowid
                if provider != 'comicvine':
                    ProviderIdentityDB.put_issue_identity(ExternalIdentity(
                        local_id, provider, row['provider_id'], 'provider'))
            else:
                local_id = existing[row['provider_id']]
            write_facts(cursor, local_id, mapped_facts(
                row['issue_number'], row['date'], provider + '_mapped'))
            cursor.execute('RELEASE provider_issue')
            existing[row['provider_id']] = local_id
        except BaseException:
            cursor.execute('ROLLBACK TO provider_issue')
            cursor.execute('RELEASE provider_issue')
            raise

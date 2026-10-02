"""Batched, read-only file association evidence for organizer discovery."""

from datetime import datetime, timezone
from typing import Any, Collection, Dict, List

from backend.base.import_candidate import (ClaimRole, EvidenceSource,
                                           ExistingFileIdentity,
                                           LocalAssociation, Provenance,
                                           ProviderIdentityClaim,
                                           ProviderReference, ResourceKind)
from backend.internals.db import get_db
from backend.internals.provider_identity import MetadataIdentityError


def load_existing_import_identities(
    paths: Collection[str], registered_providers: Collection[str], cursor: Any = None
) -> Dict[str, ExistingFileIdentity]:
    """One SELECT per 400 requested paths, no network/read repair or ID fallback.

    Include all existing issue and general-file associations, including forced
    ones. External volume references remain references, not selected authority.
    Unrelated invalid volumes are outside this query's scope.
    """
    ordered = sorted(set(paths))
    if not ordered:
        return {}
    cursor = get_db() if cursor is None else cursor
    observed_at = datetime.now(timezone.utc)
    result: Dict[str, ExistingFileIdentity] = {}
    for offset in range(0, len(ordered), 400):
        batch = ordered[offset:offset + 400]
        placeholders = ','.join('?' for _ in batch)
        rows = cursor.execute(f'''
            WITH bindings AS (
                SELECT b.file_id,i.volume_id,i.id AS issue_id,b.forced
                FROM issues_files b JOIN issues i ON i.id=b.issue_id
                UNION ALL
                SELECT file_id,volume_id,NULL,forced FROM volume_files
            )
            SELECT f.filepath,f.id,b.volume_id,b.issue_id,b.forced,
                   v.metadata_provider,s.provider_id,v.comicvine_id,
                   s.last_fetch,v.last_cv_fetch,
                   e.provider,e.provider_id,e.provenance,v.title,i.issue_number,si.provider_id
            FROM active_files f LEFT JOIN bindings b ON b.file_id=f.id
            LEFT JOIN volumes v ON v.id=b.volume_id
            LEFT JOIN issues i ON i.id=b.issue_id
            LEFT JOIN issue_external_ids si ON si.issue_id=i.id AND si.provider=v.metadata_provider
            LEFT JOIN volume_external_ids s
                ON s.volume_id=v.id AND s.provider=v.metadata_provider
            LEFT JOIN volume_external_ids e ON e.volume_id=v.id
            WHERE f.filepath IN ({placeholders})
            ORDER BY f.filepath,b.volume_id,b.issue_id,e.provider
        ''', batch)
        associations: Dict[str, List[LocalAssociation]] = {}
        references: Dict[str, List[ProviderIdentityClaim]] = {}
        file_ids: Dict[str, int] = {}
        for (path, file_id, volume_id, issue_id, forced, provider, provider_id,
             cv_id, fetched, cv_fetched, ref_provider, ref_id, ref_origin,
             volume_title, issue_number, issue_provider_id) in rows:
            file_ids[path] = file_id
            associations.setdefault(path, [])
            references.setdefault(path, [])
            if volume_id is None:
                continue
            if provider not in registered_providers:
                raise MetadataIdentityError('Unregistered selected provider')
            if not isinstance(provider_id, str) or not provider_id:
                raise MetadataIdentityError('Missing selected volume identity')
            if provider == 'comicvine' and (
                cv_id is None or provider_id != str(cv_id) or fetched != cv_fetched
            ):
                raise MetadataIdentityError('ComicVine volume identity shadow conflict')
            association = LocalAssociation(
                volume_id, issue_id, bool(forced),
                ProviderReference(provider, ResourceKind.VOLUME, provider_id),
                Provenance(EvidenceSource.MANUAL if forced else EvidenceSource.DATABASE,
                           'issues_files' if issue_id is not None else 'volume_files',
                           observed_at=observed_at), volume_title, issue_number,
                ProviderReference(provider, ResourceKind.ISSUE, issue_provider_id)
                if issue_provider_id is not None else None)
            if association not in associations[path]:
                associations[path].append(association)
            if ref_provider is not None and ref_provider != provider:
                claim = ProviderIdentityClaim(
                    ProviderReference(ref_provider, ResourceKind.VOLUME, ref_id),
                    ClaimRole.CROSS_REFERENCE,
                    Provenance(EvidenceSource.DATABASE, 'volume_external_ids', ref_origin,
                               observed_at))
                if claim not in references[path]:
                    references[path].append(claim)
        result.update({path: ExistingFileIdentity(
            file_id, tuple(associations[path]), tuple(references[path]))
            for path, file_id in file_ids.items()})
    return result

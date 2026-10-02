"""Explicit persistence of provider assertions; core metadata is never parsed."""

from sqlite3 import IntegrityError

from backend.base.definitions import ApiResponse, KapowarrException
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.internals.db import get_db
from backend.internals.issue_facts import write_facts
from backend.internals.provider_identity import (ExternalIdentity,
                                                 ProviderIdentityDB)


class IdentityEnrichmentConflict(KapowarrException):
    @property
    def api_response(self) -> ApiResponse:
        return {'code': 409, 'error': 'IdentityEnrichmentConflict', 'result': {
            'reason': 'External identity conflict; no automatic merge or provider switch'}}


def persist_enrichment(result: VolumeFetchResult, volume_id: int) -> None:
    """Caller owns transaction. All owners must belong to this fetched volume."""
    if result.snapshot is not None:
        from backend.implementations.metadata.snapshot_persistence import \
            persist_snapshot_facts
        persist_snapshot_facts(result.snapshot, volume_id)
        return
    if not result.enrichment and not result.issue_facts:
        return
    metadata = result.metadata
    cursor = get_db()
    if ProviderIdentityDB.selected_provider(volume_id) != metadata.provider:
        raise IdentityEnrichmentConflict()
    owners = dict(cursor.execute('''SELECT e.provider_id,i.id FROM issues i
        JOIN issue_external_ids e ON e.issue_id=i.id
        WHERE i.volume_id=? AND e.provider=?''', (volume_id, metadata.provider)))
    fetched = {issue.provider_id for issue in metadata.issues or []}
    try:
        seen = set()
        for item in result.issue_facts:
            if (item.provider != metadata.provider or item.parent_id != metadata.provider_id
                    or item.provider_id not in fetched or item.provider_id in seen):
                raise IdentityEnrichmentConflict()
            seen.add(item.provider_id)
            write_facts(cursor, owners[item.provider_id], item.facts)
            relation = item.variant_of
            if relation is not None:
                if (relation.provider, relation.provider_id) == (item.provider, item.provider_id):
                    raise IdentityEnrichmentConflict()
                cursor.execute('''INSERT INTO issue_variant_of VALUES(?,?,?,?)
                    ON CONFLICT(issue_id) DO UPDATE SET base_provider=excluded.base_provider,
                    base_provider_id=excluded.base_provider_id,provenance=excluded.provenance''',
                    (owners[item.provider_id], relation.provider, relation.provider_id, relation.provenance))
        for assertion in result.enrichment:
            if assertion.owner_provider != metadata.provider or assertion.provider == metadata.provider:
                raise IdentityEnrichmentConflict()
            is_volume = assertion.entity == 'volume'
            if is_volume:
                if assertion.owner_id != metadata.provider_id:
                    raise IdentityEnrichmentConflict()
                local = volume_id
            elif assertion.entity == 'issue' and assertion.owner_id in fetched:
                local = owners[assertion.owner_id]
            else:
                raise IdentityEnrichmentConflict()
            existing = (ProviderIdentityDB.volume_identities(local, assertion.provider)
                        if is_volume else ProviderIdentityDB.issue_identities(local, assertion.provider))
            if existing:
                if existing[0].provider_id != assertion.provider_id:
                    raise IdentityEnrichmentConflict()
                # Retain established provenance and timestamps on a matching ref.
                continue
            if is_volume and cursor.execute('''SELECT 1 FROM volume_external_ids
                    WHERE provider=? AND provider_id=? AND volume_id!=? LIMIT 1''',
                    (assertion.provider, assertion.provider_id, local)).fetchone():
                raise IdentityEnrichmentConflict()
            identity = ExternalIdentity(local, assertion.provider, assertion.provider_id, assertion.provenance)
            if assertion.provider == 'comicvine':
                ProviderIdentityDB.put_comicvine_reference(identity, is_volume)
            elif is_volume:
                ProviderIdentityDB.put_volume_identity(identity)
            else:
                ProviderIdentityDB.put_issue_identity(identity)
    except (IntegrityError, ValueError, KeyError):
        raise IdentityEnrichmentConflict() from None

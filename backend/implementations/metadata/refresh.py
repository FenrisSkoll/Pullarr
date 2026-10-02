"""Complete single-fetch reconciliation. Existing bulk reconciliation is separate."""

from asyncio import run
from typing import Dict, Optional, Tuple

from backend.base.logging import LOGGER
from backend.implementations.metadata.enrichment import (
    MetadataScheduledProvider, VolumeFetchResult, fetch_volume_result)
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.format_evidence import \
    validate_format_evidence
from backend.implementations.metadata.identity_enrichment import \
    persist_enrichment
from backend.implementations.metadata.persistence import (volume_input,
                                                          write_issue_batch)
from backend.implementations.metadata.provider import MetadataVolumeProvider
from backend.implementations.metadata.publication_evidence import \
    validate_publication_evidence
from backend.implementations.metadata.snapshot import MetadataSnapshotProvider
from backend.internals.db import get_db
from backend.internals.provider_authority import capture_refresh, guarded_stage
from backend.internals.provider_identity import (MetadataIdentityError,
                                                 ProviderIdentityDB)


def refresh_single_group(provider: MetadataVolumeProvider | MetadataSnapshotProvider, provider_key: str,
                         identities: Dict[str, Tuple[int, Optional[float]]],
                         current_time, volume_id, update_websocket):
    from backend.implementations.file_matching import scan_files
    from backend.implementations.volumes import (
        Issue, evaluate_volume_classification)
    from backend.internals.classification_provenance import apply, revisions
    ProviderIdentityDB.validate_issue_metadata_identities(provider_key)
    tokens = capture_refresh(get_db(), provider_key, identities)
    work = list(identities.items())
    if volume_id is None and isinstance(provider, MetadataSnapshotProvider):
        now = current_time.timestamp()
        # Persist an attempt cooldown separately from successful metadata fetch
        # time: an unavailable oldest publication must not starve every peer.
        attempts = dict(get_db().execute("SELECT key,value FROM config WHERE key LIKE 'provider_snapshot_due:%'"))
        work = [entry for entry in work if float(attempts.get(
            'provider_snapshot_due:' + str(entry[1][0]), 0)) <= now]
        work.sort(key=lambda entry: (entry[1][1] or 0, entry[1][0]))
        work = work[:provider.scheduled_volume_limit]
    for provider_id, (local, _) in work:
        expected_revision = revisions(get_db(), [local])[local]
        if volume_id is None and isinstance(provider, MetadataSnapshotProvider):
            cursor = get_db()
            if cursor.connection.in_transaction:
                raise MetadataIdentityError('Scheduled snapshot requires no pending metadata writes')
            with guarded_stage(cursor, (tokens[local],)):
                cursor.execute('''INSERT INTO config(key,value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                    ('provider_snapshot_due:' + str(local), current_time.timestamp() + 86400))
        try:
            if volume_id is None and isinstance(provider, MetadataSnapshotProvider):
                snapshot = run(provider.fetch_snapshot_scheduled(provider_id))
                result = VolumeFetchResult(snapshot.volume, (), snapshot=snapshot)
            elif volume_id is None and isinstance(provider, MetadataScheduledProvider):
                result = run(provider.fetch_volume_scheduled(provider_id))
            else:
                result = run(fetch_volume_result(provider, provider_id))
        except MetadataProviderError as error:
            if volume_id is not None:
                raise
            LOGGER.warning('Metadata refresh deferred: %s (%s), retry at %s',
                           error.provider, error.reason, error.retry_at)
            if update_websocket:
                from backend.internals.server import TaskStatusEvent, WebSocket
                WebSocket().emit(TaskStatusEvent(str(error)))
            if error.reason in ('not_found', 'malformed'):
                continue  # A bad resource must not starve other volumes.
            break  # Other provider groups may still run; never fallback.
        metadata = result.metadata
        if metadata.provider != provider_key or metadata.provider_id != provider_id:
            raise MetadataIdentityError('Fetched volume differs from selected identity')
        if result.snapshot is not None:
            from backend.implementations.metadata.snapshot_persistence import \
                reconcile_snapshot
            reconcile_snapshot(result, local, expected_revision=expected_revision,
                               expected_authority=tokens[local])
            continue
        validate_format_evidence(result.format_evidence, provider_key, provider_id)
        validate_publication_evidence(result.publication_evidence, provider_key, provider_id)
        if metadata.issues is None or len(metadata.issues) != metadata.issue_count:
            raise MetadataIdentityError('Single-fetch refresh requires a complete issue snapshot')
        ids = {issue.provider_id for issue in metadata.issues}
        if len(ids) != len(metadata.issues):
            raise MetadataIdentityError('Duplicate issue in metadata snapshot')
        data = volume_input(metadata, provider_key)
        cursor = get_db()
        existing = dict(cursor.execute('''SELECT e.provider_id,i.id FROM issues i
            JOIN issue_external_ids e ON e.issue_id=i.id
            WHERE i.volume_id=? AND e.provider=?''', (local, provider_key)))
        owners = {}
        ordered_ids = list(ids)
        for start in range(0, len(ordered_ids), 750):
            batch = ordered_ids[start:start + 750]
            placeholders = ','.join('?' for _ in batch)
            owners.update(cursor.execute('''SELECT e.provider_id,i.volume_id FROM issues i
                JOIN issue_external_ids e ON e.issue_id=i.id WHERE e.provider=?
                AND e.provider_id IN (''' + placeholders + ')', (provider_key, *batch)))
        if any(identity in owners and owners[identity] != local for identity in ids):
            raise MetadataIdentityError('Issue identity already belongs to another local volume')
        monitor = cursor.execute('SELECT monitor_new_issues FROM volumes WHERE id=?', (local,)).fetchone()[0]
        with guarded_stage(cursor, (tokens[local],)):
            cursor.execute('''UPDATE volumes SET title=?,alt_title=?,year=?,publisher=?,
                volume_number=?,description=?,site_url=? WHERE id=?''', (
                data['title'], (data['aliases'] or [None])[0], data['year'], data['publisher'],
                data['volume_number'], data['description'], data['site_url'], local))
            cursor.execute('UPDATE volumes_covers SET cover=? WHERE volume_id=?', (data['cover'], local))
            rows = [dict(issue, id=existing.get(issue['provider_id']), volume_id=local, monitored=monitor)
                    for issue in data['issues']]
            write_issue_batch(cursor, '''INSERT INTO issues(id,volume_id,comicvine_id,
                issue_number,calculated_issue_number,title,date,description,monitored)
                VALUES (:id,:volume_id,:comicvine_id,:issue_number,:calculated_issue_number,
                    :title,:date,:description,:monitored)
                ON CONFLICT(id) DO UPDATE SET issue_number=excluded.issue_number,
                    calculated_issue_number=excluded.calculated_issue_number,
                    title=excluded.title,date=excluded.date,description=excluded.description''', rows, provider_key,
                              dict(existing))
            # Conflicts roll back all metadata before file-linked deletion is attempted.
            persist_enrichment(result, local)
            for identity, issue_id in existing.items():
                if identity not in ids:
                    Issue(issue_id).delete()
            cursor.execute('''UPDATE volume_external_ids SET last_fetch=?
                WHERE volume_id=? AND provider=?''', (current_time.timestamp(), local, provider_key))
            candidate = evaluate_volume_classification(local, result.format_evidence, result.publication_evidence)
            apply(cursor, local, candidate.value, decision=candidate, expected_revision=expected_revision)
        scan_files(local, update_websocket=update_websocket, expected_authority=tokens[local])

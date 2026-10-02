"""Seeding observations and fail-closed cleanup of Pullarr-owned jobs only."""
import json
from dataclasses import replace
from time import time

from backend.base.download_job import DownloadErrorCode as E, DownloadFailure
from backend.base.managed_client import RetentionPolicy, retention_evaluation
from backend.base.organization_job import OrganizationError
from backend.implementations.organization_filesystem import (artifact,
                                                             execution_gate)
from backend.internals.organization_jobs import canonical, digest


def recover_submissions(store, configs, factory):
    """An ambiguous upload is reconciled by exact retained hashes, never retried."""
    from backend.base.torrent import TorrentIdentity
    clients = {c.key:c for c in configs if getattr(c,'kind','')=='qbittorrent' and c.enabled}
    rows = store.db.execute('''SELECT t.*,d.client_id,d.client_instance,d.intent FROM acquisition_torrents t
        JOIN acquisition_downloads d ON d.id=t.download_id
        WHERE d.nzo_id IS NULL AND d.state='ambiguous' ORDER BY d.updated_at,d.id LIMIT 16''').fetchall()
    for row in rows:
        config = clients.get(row['client_id'])
        if config is None or config.instance != row['client_instance']:
            continue
        try:
            remote = factory(config).find_submission(TorrentIdentity(row['infohash_v1'],row['infohash_v2']),
                                                    json.loads(row['intent'])['candidate_id'])
            with store.transaction():
                changed = store.db.execute('''UPDATE acquisition_downloads SET nzo_id=?,state='submitted',error=NULL
                    WHERE id=? AND nzo_id IS NULL AND state='ambiguous' ''', (remote,row['download_id']))
                if changed.rowcount:
                    store.event(row['download_id'],'submitted')
        except DownloadFailure:
            continue


def observe_torrents(store, configs, factory, clock=time):
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_torrents'").fetchone():
        return
    rows = store.db.execute('''SELECT t.*,d.client_id,d.client_instance,d.nzo_id FROM acquisition_torrents t
        JOIN acquisition_downloads d ON d.id=t.download_id
        WHERE t.state NOT IN ('removed_keep','removed_data') AND d.nzo_id IS NOT NULL
        ORDER BY COALESCE(t.observed_at,0),t.download_id LIMIT 1000''').fetchall()
    clients = {c.key: c for c in configs if getattr(c, 'kind', '') == 'qbittorrent' and c.enabled}
    groups = {}
    for row in rows:
        config = clients.get(row['client_id'])
        if config and config.instance == row['client_instance']:
            groups.setdefault(config.key, []).append(row)
    for key, group in groups.items():
        client = factory(clients[key])
        for offset in range(0, len(group), 100):
            batch = group[offset:offset + 100]
            try:
                remote = {r['hash']: r for r in client.info(tuple(r['nzo_id'] for r in batch))}
            except DownloadFailure:
                continue  # Offline is not deleted or completed.
            for row in batch:
                value = remote.get(row['nzo_id'])
                safe = {'available': value is not None}
                state = 'review'
                if value is not None:
                    for field in ('ratio', 'seeding_time', 'progress', 'dlspeed', 'upspeed', 'size'):
                        observed = value.get(field)
                        if type(observed) in (int, float) and 0 <= observed <= 10**18:
                            safe[field] = observed
                    safe['category_matches'] = value.get('category') == client.config.category
                    safe['status'] = value.get('state') if value.get('state') in {
                        'uploading','stalledUP','queuedUP','stoppedUP','pausedUP','forcedUP',
                        'downloading','stalledDL','queuedDL','stoppedDL','pausedDL','checkingUP',
                        'checkingDL','checkingResumeData','moving','error','missingFiles'} else 'unknown'
                    state = 'seeding' if safe['status'] in ('uploading','stalledUP','forcedUP') else 'retained'
                if row['cleanup_receipt'] and json.loads(row['cleanup_receipt']).get('outcome') == 'pending':
                    state = 'review'  # A lost deletion response is not permission to retry it.
                encoded = canonical(safe)
                store.db.execute('''UPDATE acquisition_torrents SET observation=?,state=?,observed_at=?,
                    cleanup_revision=cleanup_revision+CASE WHEN observation<>? THEN 1 ELSE 0 END WHERE download_id=?''',
                    (encoded, state, clock(), encoded, row['download_id']))


def cleanup_preview(store, identifier, *, delete_data, manual=False, clock=time):
    row = store.db.execute('''SELECT t.*,d.client_id,d.client_instance,d.nzo_id,d.state download_state
        FROM acquisition_torrents t JOIN acquisition_downloads d ON d.id=t.download_id WHERE t.download_id=?''', (identifier,)).fetchone()
    if row is None or type(delete_data) is not bool:
        raise DownloadFailure(E.CONFIGURATION)
    observations = json.loads(row['observation'])
    reasons = []
    if row['cleanup_receipt'] and json.loads(row['cleanup_receipt']).get('outcome') == 'pending':
        reasons.append('cleanup_reconciliation_required')
    if row['state'] in ('removed_keep','removed_data'):
        reasons.append('already_removed')
    if row['observed_at'] is None or not 0 <= clock() - row['observed_at'] <= 60:
        reasons.append('fresh_client_observation_required')
    if not observations.get('available') or not observations.get('category_matches'):
        reasons.append('client_relationship_changed')
    if row['download_state'] != 'completed' or observations.get('progress') != 1:
        reasons.append('download_not_complete')
    intake = store.db.execute("SELECT id,state FROM acquisition_intakes WHERE kind='qbittorrent' AND download_id=?", (identifier,)).fetchone()
    if not intake or intake['state'] != 'completed':
        reasons.append('intake_not_complete')
    evidence = []
    if intake:
        artifacts = store.db.execute('''SELECT a.*,f.filepath,f.size,j.state job_state,j.intent FROM acquisition_artifacts a
            LEFT JOIN active_files f ON f.id=a.final_file_id LEFT JOIN organization_jobs j ON j.id=a.organization_job_id
            WHERE a.intake_id=? AND a.state<>'prepared' ORDER BY a.id''', (intake['id'],)).fetchall()
        if not artifacts:
            reasons.append('no_imported_artifacts')
        for item in artifacts:
            if item['state'] != 'organized' or item['job_state'] != 'completed' or not item['filepath']:
                reasons.append('artifact_review_required')
                continue
            try:
                current = artifact(item['filepath'])
                from backend.internals.organization_reservations import \
                    load_reservations
                if load_reservations(store.db.cursor()).conflicts(item['filepath']):
                    reasons.append('organization_work_pending')
                receipts = store.db.execute('SELECT evidence FROM organization_steps WHERE job_id=? ORDER BY ordinal DESC',
                                            (item['organization_job_id'],)).fetchall()
                expected = next((json.loads(r[0])['artifact_after'] for r in receipts if 'artifact_after' in json.loads(r[0])), None)
                intent = json.loads(item['intent'])
                if expected is None and 'upgrade_effect' in intent:
                    expected = intent['incoming']
                if expected is not None and current['sha256'] != expected['sha256']:
                    # Container maintenance is not a new acquisition. Follow only
                    # completed, verified same-file journal lineage; never title,
                    # size similarity or an arbitrary latest maintenance receipt.
                    from backend.features.organization_archive import \
                        maintained_hash
                    expected = dict(expected, sha256=maintained_hash(store.db, item['final_file_id'], expected['sha256']))
                if expected is None or current['sha256'] != expected['sha256'] or current['size'] != item['size']:
                    reasons.append('library_identity_changed')
                evidence.append((item['final_file_id'], current))
            except (OSError, ValueError, KeyError, OrganizationError):
                reasons.append('library_verification_unavailable')
    configured = RetentionPolicy(**json.loads(row['policy']))
    if manual:
        configured = replace(configured, mode='after_import')
    policy = retention_evaluation(configured,
        current_ratio=observations.get('ratio'), seeding_seconds=observations.get('seeding_time'),
        requirements=json.loads(row['requirements']))
    if not policy['eligible']:
        reasons.append(policy['reason'])
    receipt = dict(download_id=identifier, revision=row['cleanup_revision'], delete_data=delete_data, manual=manual,
                   client_instance=row['client_instance'], remote_hash=row['nzo_id'], evidence=evidence,
                   eligible=not reasons, reasons=sorted(set(reasons)), policy=policy)
    receipt['confirmation'] = digest(canonical(receipt))
    return receipt


def cleanup(store, identifier, config, client, confirmation, *, delete_data, manual=False, clock=time):
    # Organizer/intake cannot commit a conflicting operation during the review.
    with execution_gate(store.path + '.intake'), execution_gate(store.path):
        observe_torrents(store, (config,), lambda _: client, clock)
        preview = cleanup_preview(store, identifier, delete_data=delete_data, manual=manual, clock=clock)
        if not preview['eligible'] or preview['confirmation'] != confirmation or config.instance != preview['client_instance']:
            raise DownloadFailure(E.DRIFT)
        row = store.db.execute('SELECT * FROM acquisition_torrents WHERE download_id=?', (identifier,)).fetchone()
        properties = client.properties(preview['remote_hash'])
        if (row['infohash_v1'] and properties.get('infohash_v1') != row['infohash_v1']
                or row['infohash_v2'] and properties.get('infohash_v2') != row['infohash_v2']):
            raise DownloadFailure(E.SELECTION)
        if delete_data:
            files = client.files(preview['remote_hash'])
            if any(f['priority'] <= 0 or f['progress'] != 1 for f in files):
                raise DownloadFailure(E.SELECTION)
            current = client.observe((preview['remote_hash'],))[preview['remote_hash']]
            previous = store.db.execute('SELECT observation FROM acquisition_downloads WHERE id=?', (identifier,)).fetchone()
            if tuple(json.loads(previous[0]).get('paths', ())) != current.paths:
                raise DownloadFailure(E.DRIFT)
        # Persist reviewed intent before the external destructive boundary.
        store.db.execute("UPDATE acquisition_torrents SET cleanup_receipt=?,state='review' WHERE download_id=?",
                         (canonical(dict(preview, outcome='pending')), identifier))
        client.remove(preview['remote_hash'], delete_data=delete_data)
        store.db.execute('UPDATE acquisition_torrents SET state=?,cleanup_receipt=?,cleanup_revision=cleanup_revision+1 WHERE download_id=?',
                         ('removed_data' if delete_data else 'removed_keep', canonical(dict(preview, outcome='confirmed')), identifier))
        return {'state': 'removed_data' if delete_data else 'removed_keep'}


def automatic_cleanup(store, configs, factory, clock=time):
    """Narrow existing observer extension; no additional scheduler."""
    clients = {c.key: c for c in configs if getattr(c, 'kind', '') == 'qbittorrent' and c.enabled}
    rows = store.db.execute('''SELECT t.download_id,t.policy,d.client_id,d.client_instance
        FROM acquisition_torrents t JOIN acquisition_downloads d ON d.id=t.download_id
        WHERE t.state IN ('seeding','retained') AND t.observed_at>=?
        AND json_extract(t.policy,'$.mode') NOT IN ('client_managed','keep')
        ORDER BY t.observed_at,t.download_id LIMIT 16''', (clock() - 60,)).fetchall()
    for row in rows:
        config = clients.get(row['client_id'])
        if not config or config.instance != row['client_instance']:
            continue
        # Disabling cleanup in current configuration is an immediate safety stop.
        if config.retention.mode in ('client_managed', 'keep'):
            continue
        try:
            facts = store.db.execute('SELECT observation,requirements FROM acquisition_torrents WHERE download_id=?',
                                     (row['download_id'],)).fetchone()
            observed = json.loads(facts['observation'])
            # A later stricter user policy must not be weakened by the historic
            # selection snapshot. Both must permit automatic cleanup.
            current = retention_evaluation(config.retention, current_ratio=observed.get('ratio'),
                seeding_seconds=observed.get('seeding_time'), requirements=json.loads(facts['requirements']))
            if not current['eligible']:
                continue
            preview = cleanup_preview(store, row['download_id'], delete_data=True, clock=clock)
            if preview['eligible']:
                cleanup(store, row['download_id'], config, factory(config), preview['confirmation'], delete_data=True, clock=clock)
        except (DownloadFailure, OrganizationError, OSError):
            continue  # Retain data and durable intent; never guess remote success.

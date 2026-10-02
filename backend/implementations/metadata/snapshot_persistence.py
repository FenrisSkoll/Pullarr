"""Transactional rich facts and conservative refresh; no filesystem effects."""

import json
from dataclasses import asdict

from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.persistence import fetch_input
from backend.implementations.metadata.snapshot import ProviderVolumeSnapshot
from backend.internals.db import get_db
from backend.internals.issue_facts import write_facts
from backend.internals.provider_authority import capture, guarded_stage
from backend.internals.provider_identity import (ExternalIdentity,
                                                 MetadataIdentityError,
                                                 ProviderIdentityDB)


def validate_snapshot_owners(snapshot: ProviderVolumeSnapshot, volume_id=None) -> None:
    """Batched identity/fact-ownership guard, inside the caller's transaction."""
    cursor = get_db()
    if volume_id is None and cursor.execute('''SELECT 1 FROM volume_external_ids
            WHERE provider=? AND provider_id=?''',
            (snapshot.volume.provider, snapshot.volume.provider_id)).fetchone():
        # Repeat the pre-fetch add check in the persistence transaction. This
        # also protects zero-issue snapshots when another add won during HTTP.
        raise MetadataIdentityError('Provider volume identity already has an owner')
    ids = tuple(i.provider_id for i in snapshot.issues)
    incoming = {i.provider_id: i for i in snapshot.issues}
    for start in range(0, len(ids), 750):
        batch = ids[start:start + 750]
        marks = ','.join('?' for _ in batch)
        rows = cursor.execute('''SELECT e.provider_id,i.volume_id,n.provenance
            FROM issue_external_ids e JOIN issues i ON i.id=e.issue_id
            LEFT JOIN issue_number_facts n ON n.issue_id=i.id
            WHERE e.provider=? AND e.provider_id IN (''' + marks + ')',
            (snapshot.volume.provider, *batch)).fetchall()
        seen = set()
        for identity, owner, provenance in rows:
            if owner != volume_id or identity in seen:
                raise MetadataIdentityError('Provider issue identity already has another owner')
            seen.add(identity)
            if provenance is not None and provenance != incoming[identity].facts.number.provenance:
                raise MetadataIdentityError('Snapshot may not replace another fact authority')
        dates = cursor.execute('''SELECT e.provider_id,d.provenance FROM issue_external_ids e
            JOIN issue_date_facts d ON d.issue_id=e.issue_id
            WHERE e.provider=? AND e.provider_id IN (''' + marks + ')',
            (snapshot.volume.provider, *batch)).fetchall()
        for identity, provenance in dates:
            if provenance not in {d.provenance for d in incoming[identity].facts.dates}:
                raise MetadataIdentityError('Snapshot may not replace another date authority')


def persist_snapshot_facts(snapshot: ProviderVolumeSnapshot, volume_id: int) -> None:
    """Caller owns transaction; metadata and facts become visible together."""
    cursor = get_db()
    if not cursor.connection.in_transaction:
        raise MetadataIdentityError('Snapshot persistence requires caller transaction')
    volume = snapshot.volume
    if ProviderIdentityDB.selected_provider(volume_id) != volume.provider:
        raise MetadataIdentityError('Snapshot differs from selected authority')
    selected = cursor.execute('''SELECT provider_id FROM volume_external_ids
        WHERE volume_id=? AND provider=?''', (volume_id, volume.provider)).fetchone()
    if selected is None or selected[0] != volume.provider_id:
        raise MetadataIdentityError('Snapshot volume identity differs')
    owners = dict(cursor.execute('''SELECT e.provider_id,i.id FROM issues i
        JOIN issue_external_ids e ON e.issue_id=i.id WHERE i.volume_id=? AND e.provider=?''',
        (volume_id, volume.provider)))
    for issue in snapshot.issues:
        local = owners.get(issue.provider_id)
        if local is None:
            raise MetadataIdentityError('Missing snapshot issue owner')
        write_facts(cursor, local, issue.facts)
        relation = issue.variant_of
        # Complete membership/details authorize replacing this provider's exact
        # relation, not aliasing or deleting the separate local issue.
        cursor.execute('DELETE FROM issue_variant_of WHERE issue_id=? AND base_provider=?',
                       (local, volume.provider))
        if relation is not None:
            cursor.execute('INSERT INTO issue_variant_of VALUES(?,?,?,?)',
                (local, relation.provider, relation.provider_id, relation.provenance))
    from backend.internals.bibliography import persist_bibliography
    persist_bibliography(cursor, snapshot, volume_id, owners)
    missing = sorted(set(owners) - {i.provider_id for i in snapshot.issues})
    receipt = dict(asdict(snapshot.receipt), provider=volume.provider,
                   provider_id=volume.provider_id,
                   retained_missing_issue_ids=[owners[i] for i in missing])
    cursor.execute('''INSERT INTO config(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
        ('provider_snapshot:' + str(volume_id), json.dumps(receipt, separators=(',', ':'))))


def reconcile_snapshot(result: VolumeFetchResult, volume_id: int, *, expected_revision=None,
                       expected_authority=None) -> None:
    """Never delete remote-missing local issues, files, associations or intent.

    Membership-coherent HTTP is not a transactional remote catalog snapshot.
    Retained missing IDs are reported through the local rich API receipt.
    """
    snapshot = result.snapshot
    if snapshot is None:
        raise ValueError('Rich snapshot required')
    data = fetch_input(result, snapshot.volume.provider)
    cursor = get_db()
    from backend.internals.classification_provenance import apply, revisions
    if expected_revision is None:
        expected_revision = revisions(cursor, [volume_id])[volume_id]
    # Direct local reconciliation callers have no outstanding acquisition. The
    # production refresh path MUST pass its original pre-fetch token explicitly.
    if expected_authority is None:
        expected_authority = capture(cursor, (volume_id,))[volume_id]
    if expected_authority.volume_id != volume_id:
        raise MetadataIdentityError('Snapshot authority belongs to another volume')
    with guarded_stage(cursor, (expected_authority,)):
        validate_snapshot_owners(snapshot, volume_id)
        selected = ProviderIdentityDB.selected_provider(volume_id)
        if selected != snapshot.volume.provider:
            raise MetadataIdentityError('Selected authority changed during acquisition')
        monitor = cursor.execute('SELECT monitor_new_issues FROM volumes WHERE id=?', (volume_id,)).fetchone()[0]
        existing = dict(cursor.execute('''SELECT e.provider_id,i.id FROM issues i
            JOIN issue_external_ids e ON e.issue_id=i.id WHERE i.volume_id=? AND e.provider=?''',
            (volume_id, selected)))
        cursor.execute('''UPDATE volumes SET title=?,year=?,publisher=?,description=?,site_url=? WHERE id=?''',
            (data['title'], data['year'], data['publisher'], data['description'], data['site_url'], volume_id))
        for row in data['issues']:
            identity = row['provider_id']
            local = existing.get(identity)
            values = (row['issue_number'], row['calculated_issue_number'], row['title'], row['date'])
            if local is None:
                local = cursor.execute('''INSERT INTO issues(volume_id,issue_number,calculated_issue_number,
                    title,date,monitored) VALUES(?,?,?,?,?,?)''', (volume_id, *values, monitor)).lastrowid
                ProviderIdentityDB.put_issue_identity(ExternalIdentity(local, selected, identity, 'provider'))
            else:
                cursor.execute('''UPDATE issues SET issue_number=?,calculated_issue_number=?,title=?,date=?
                    WHERE id=?''', (*values, local))
        persist_snapshot_facts(snapshot, volume_id)
        cursor.execute('''UPDATE volume_external_ids SET last_fetch=? WHERE volume_id=? AND provider=?''',
            (snapshot.receipt.acquired_at, volume_id, selected))
        from backend.implementations.volumes import \
            evaluate_volume_classification
        candidate = evaluate_volume_classification(volume_id)
        apply(cursor, volume_id, candidate.value, decision=candidate, expected_revision=expected_revision)

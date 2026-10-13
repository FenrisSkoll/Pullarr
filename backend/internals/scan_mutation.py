"""Serialized legacy scan admission against the shared organizer reservations.

Owned transactions use BEGIN IMMEDIATE. Borrowed manual transactions promote to
a writer with a zero-row UPDATE and use a savepoint, never committing caller work.
The writer is held through filesystem cleanup. No persistent scan job is needed.
"""

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.base.switch_review import SwitchReviewError
from backend.internals.organization_reservations import load_reservations
from backend.internals.provider_authority import require_current

_owner: ContextVar[Any] = ContextVar('scan_mutation_connection', default=None)


def require_scan_guard(cursor):
    if _owner.get() is not cursor.connection or not cursor.connection.in_transaction:
        raise RuntimeError('Mutating scan requires its serialized admission boundary')


def require_unreserved(cursor, path):
    require_scan_guard(cursor)
    if load_reservations(cursor).conflicts(path):
        raise OrganizationError(ExecutionCode.BUSY, 'scan_deferred_organization_reservation')


def unmatched_reserved(cursor, index=None):
    """Global legacy orphan pruning must share the caller's writer boundary."""
    if not cursor.connection.in_transaction:
        raise RuntimeError('Orphan reservation check requires a writer transaction')
    index = index or load_reservations(cursor)
    if not index.keys:
        return False
    rows = cursor.execute('''SELECT filepath FROM active_files f
        WHERE NOT EXISTS(SELECT 1 FROM issues_files x WHERE x.file_id=f.id)
        AND NOT EXISTS(SELECT 1 FROM volume_files x WHERE x.file_id=f.id)''')
    return any(index.conflicts(row[0]) for row in rows)


@contextmanager
def _writer_mutation(cursor):
    """Hold cooperative exclusion through DB and filesystem continuations."""
    connection = cursor.connection
    borrowed = connection.in_transaction
    if borrowed:
        cursor.execute('SAVEPOINT scan_mutation')
    else:
        cursor.execute('BEGIN IMMEDIATE')
    token = _owner.set(connection)
    try:
        if borrowed:
            # Acquire the same SQLite writer exclusion without changing a row.
            # A read-snapshot promotion failure aborts before filesystem work.
            cursor.execute('UPDATE volumes SET id=id WHERE 0')
        yield
        require_scan_guard(cursor)
        if borrowed:
            cursor.execute('RELEASE SAVEPOINT scan_mutation')
        else:
            connection.commit()
    except BaseException:
        if connection.in_transaction:
            if borrowed:
                cursor.execute('ROLLBACK TO SAVEPOINT scan_mutation')
                cursor.execute('RELEASE SAVEPOINT scan_mutation')
            else:
                connection.rollback()
        raise
    finally:
        _owner.reset(token)


@contextmanager
def scan_mutation(cursor, volume_id, authority=None, *, prune_unmatched=True):
    if authority is not None and cursor.connection.in_transaction:
        raise SwitchReviewError('authority_write_requires_transaction_boundary')
    if authority is not None and authority.volume_id != volume_id:
        raise ValueError('Scan authority belongs to another volume')
    with _writer_mutation(cursor):
        if authority is not None:
            require_current(cursor, (authority,))
        row = cursor.execute('SELECT folder FROM volumes WHERE id=?', (volume_id,)).fetchone()
        if row is None or not row[0]:
            raise OrganizationError(ExecutionCode.STALE, 'Scan volume folder unavailable')
        index = load_reservations(cursor)
        if index.conflicts(row[0]):
            raise OrganizationError(ExecutionCode.BUSY, 'scan_deferred_organization_reservation')
        if prune_unmatched and unmatched_reserved(cursor, index):
            raise OrganizationError(ExecutionCode.BUSY, 'scan_deferred_organization_reservation')
        yield


@contextmanager
def deletion_mutation(cursor, *, volume_id=None, issue_id=None, file_id=None, cleanup_root=False):
    """Legacy deletion must not destroy a registered job's pre-marker identity.

    Resolve scopes only after writer admission. Keep it through physical unlink,
    association/monitoring changes and parent cleanup; never commit borrowed work.
    This does not turn legacy deletion into a recoverable quarantine operation.
    """
    with _writer_mutation(cursor):
        if sum(value is not None for value in (volume_id, issue_id, file_id)) != 1:
            raise ValueError('Exactly one deletion scope required')
        rows = cursor.execute('''SELECT DISTINCT f.filepath FROM files f WHERE
            f.id=:file OR f.id IN (SELECT x.file_id FROM issues_files x
                JOIN issues i ON i.id=x.issue_id WHERE i.id=:issue OR i.volume_id=:volume)
            OR f.id IN (SELECT file_id FROM volume_files WHERE volume_id=:volume)
            LIMIT 20001''', dict(file=file_id, issue=issue_id, volume=volume_id)).fetchall()
        folders = cursor.execute('''SELECT DISTINCT v.folder,r.folder FROM volumes v
            JOIN root_folders r ON r.id=v.root_folder WHERE v.id=:volume
            OR v.id IN (SELECT volume_id FROM issues WHERE id=:issue)
            OR v.id IN (SELECT i.volume_id FROM issues_files x JOIN issues i ON i.id=x.issue_id WHERE x.file_id=:file)
            OR v.id IN (SELECT volume_id FROM volume_files WHERE file_id=:file)
            LIMIT 20001''', dict(file=file_id, issue=issue_id, volume=volume_id)).fetchall()
        if len(rows) > 20000 or len(folders) > 20000:
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Deletion scope bounded')
        paths = [row[0] for row in rows]
        paths.extend(row[0] for row in folders)
        if cleanup_root:
            paths.extend(row[1] for row in folders)
        if volume_id is not None and not cleanup_root:
            # Only metadata-only volume removal may retire an old, inert local
            # import claim. Source still exists, target does not, and no worker
            # can be in RUNNING state. An ambiguous effect stays protected.
            index_before = load_reservations(cursor)
            owners = {owner for path in paths if path for owner in index_before.conflicts(path)}
            owners.update(row[0] for row in cursor.execute('''SELECT id FROM organization_jobs
                WHERE (state IN ('pending','running','recovery_required') OR
                    (state='failed' AND EXISTS(SELECT 1 FROM organization_reservations
                        WHERE job_id=organization_jobs.id)))
                AND json_valid(intent) AND json_extract(intent,'$.volume_id')=? LIMIT 20001''',
                (volume_id,)))
            if len(owners) > 20000:
                raise OrganizationError(ExecutionCode.BUSY, 'deletion_job_scope_bounded')
            for owner in owners:
                job = cursor.execute('SELECT state,intent FROM organization_jobs WHERE id=?',
                                     (owner,)).fetchone()
                if job is None:
                    continue
                if job[0] not in ('recovery_required', 'failed'):
                    raise OrganizationError(ExecutionCode.BUSY, 'deletion_active_job')
                try:
                    intent = json.loads(job[1])
                    source, target = intent['source'], intent['target']
                except (ValueError, KeyError, TypeError):
                    raise OrganizationError(ExecutionCode.BUSY, 'deletion_recovery_ambiguous') from None
                if (not intent.get('library_import') or intent.get('volume_id') != volume_id
                        or not os.path.isfile(source) or os.path.lexists(target)
                        or cursor.execute('''SELECT 1 FROM organization_steps
                            WHERE job_id=? AND kind<>'ensure_target_directory'
                            AND state='succeeded' LIMIT 1''', (owner,)).fetchone()):
                    raise OrganizationError(ExecutionCode.BUSY, 'deletion_recovery_ambiguous')
                cursor.execute("UPDATE organization_jobs SET state='failed',error='abandoned_after_metadata_removal' WHERE id=? AND state='recovery_required'", (owner,))
                cursor.execute('DELETE FROM organization_reservations WHERE job_id=?', (owner,))
        index = load_reservations(cursor)
        if any(index.conflicts(path) for path in paths if path):
            raise OrganizationError(ExecutionCode.BUSY, 'deletion_deferred_organization_reservation')
        yield

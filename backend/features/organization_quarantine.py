"""Explicit retained-artifact effects within the existing organizer journal.

This is not a public registration API. Only a reviewed duplicate registrar may
construct forward intent. Inverse registration uses OrganizationExecutor's
separate preview/confirmation boundary. No purge, copy/delete or rematching.
"""

import json
import os
import re
from copy import deepcopy
from pathlib import Path

from backend.base.duplicate_review import DuplicateReviewError
from backend.base.organization_job import (EXECUTOR_POLICY, ExecutionCode,
                                           JobState, OrganizationError,
                                           StepState, UndoPreview)
from backend.implementations.duplicate_evidence import HashBudget, stamp
from backend.implementations.organization_filesystem import (rename_no_replace,
                                                             safe_path,
                                                             sync_directory)
from backend.implementations.quarantine_location import DIRECTORY
from backend.internals.organization_jobs import (MAX_PAYLOAD, canonical,
                                                 digest, now)
from backend.internals.organization_reservations import (ReservationIndex,
                                                         load_reservations,
                                                         path_key)
from backend.internals.quarantine_state import (identity, require_owned,
                                                snapshot)
from backend.internals.switch_review import dependencies

VERSION = 'retained-artifact/v1'
MOVE = 'quarantine_file/v1'
RECORD = 'reconcile_quarantine/v1'
RESTORE = 'restore_quarantined_file/v1'
RESTORE_RECORD = 'reconcile_restored_file/v1'


def artifact_identity(value):
    # A namespace rename changes ctime on Linux. Digest, device, inode, mode,
    # byte count and mtime remain required; no arbitrary evidence is dropped.
    return {**value, 'stamp': list(value['stamp'][:5])}


class QuarantineEffect:
    def __init__(self, executor):
        self.executor, self.store = executor, executor.store

    def validate(self, intent):
        try:
            before = intent['quarantine_before']
            fid = intent['file_id']
            inverse = intent['inverse']
            original, retained = intent['original'], intent['quarantine_target']
            file = next(r for r in before['files'] if r['id'] == fid)
            root = Path(intent['root'])
            expected = root.parent / DIRECTORY / str(intent['root_id']) / intent['location_id'] / Path(original).name
            if (not self.store.has_quarantine_state or intent['quarantine_effect'] != VERSION
                    or 'directory_effect' in intent or intent['version'] != EXECUTOR_POLICY
                    or type(inverse) is not bool or type(fid) is not int or fid <= 0
                    or not re.fullmatch('[a-f0-9]{32}', intent['location_id'])
                    or retained != str(expected) or root == root.parent
                    or root not in Path(original).parents or file['filepath'] != original
                    or intent['effects'] != ([RESTORE, RESTORE_RECORD] if inverse else [MOVE, RECORD])
                    or (intent['source'], intent['target']) != ((retained, original) if inverse else (original, retained))
                    or intent['hash']['algorithm'] != 'sha256/v1'
                    or not re.fullmatch('[a-f0-9]{64}', intent['hash']['digest'])
                    or len(intent['hash']['stamp']) != 6
                    or not intent['retained_ids'] or fid in intent['retained_ids']
                    or {fid, *intent['retained_ids']} != {r['id'] for r in before['files']}
                    or set(intent['retained_hashes']) != {str(i) for i in intent['retained_ids']}
                    or any((h['algorithm'], h['digest'], h['size']) !=
                           (intent['hash']['algorithm'], intent['hash']['digest'], intent['hash']['size'])
                           for h in intent['retained_hashes'].values())
                    or before['markers'] or len(canonical(intent)) > MAX_PAYLOAD):
                raise ValueError()
            self.executor._scope(original)
            safe_path(retained)
            if not any(r['id'] == intent['root_id'] and Path(r['folder']) == root for r in before['roots']):
                raise ValueError()
        except (KeyError, ValueError, TypeError, StopIteration):
            raise OrganizationError(ExecutionCode.CORRUPT, 'invalid_quarantine_effect') from None

    def observe(self, path, expected, *, moved=False):
        try:
            value = HashBudget().inspect(path)
        except DuplicateReviewError as error:
            raise OrganizationError(ExecutionCode.SOURCE, str(error)) from None
        if (artifact_identity(value) != artifact_identity(expected) if moved
                else canonical(value) != canonical(expected)):
            raise OrganizationError(ExecutionCode.SOURCE, 'quarantine_artifact_changed')
        return value

    def current(self, intent):
        before = intent['quarantine_before']
        return snapshot(self.store.db.cursor(), [r['id'] for r in before['files']],
            historical_issues=[r['id'] for r in before['issues']],
            historical_volumes=[r['id'] for r in before['volumes']])

    def check_database(self, job, intent):
        before, fid = intent['quarantine_before'], intent['file_id']
        current = self.current(intent)
        recorded = job.steps[1].state == StepState.SUCCEEDED
        inactive = not recorded if intent['inverse'] else recorded
        marker = next((r for r in current['markers'] if r['file_id'] == fid), None)
        if inactive:
            owner = intent['quarantine_job'] if intent['inverse'] else job.id
            if (marker is None or marker['job_id'] != owner or marker['version'] != 1
                    or marker['original_filepath'] != intent['original']
                    or marker['quarantine_filepath'] != intent['quarantine_target']
                    or any(r['file_id'] == fid for key in ('direct', 'general') for r in current[key])):
                raise OrganizationError(ExecutionCode.CONFLICT, 'quarantine_lifecycle_changed')
            row = next(r for r in current['files'] if r['id'] == fid)
            if row['filepath'] != intent['quarantine_target']:
                raise OrganizationError(ExecutionCode.CONFLICT, 'quarantine_path_changed')
            row['filepath'] = intent['original']
            current['markers'].remove(marker)
            for key in ('direct', 'general'):
                current[key].extend(r for r in before[key] if r['file_id'] == fid)
                current[key].sort(key=lambda r: (r['file_id'], r.get('issue_id', r.get('volume_id'))))
        elif marker is not None:
            raise OrganizationError(ExecutionCode.CONFLICT, 'unexpected_quarantine_marker')
        moved = job.steps[0].state != StepState.PENDING and not os.path.lexists(intent['source'])
        # Inverse is always conditional on historical identity, not old titles.
        compare = identity if moved or intent['inverse'] else lambda v: {k: x for k, x in v.items() if k != 'ownership'}
        if canonical(compare(current)) != canonical(compare(before)):
            raise OrganizationError(ExecutionCode.STALE, 'quarantine_domain_changed')
        if not intent['inverse'] and not recorded:
            require_owned(current, {fid, *intent.get('batch_removed', ())})
        index = load_reservations(self.store.db.cursor())
        for path in (intent['source'], intent['target']):
            if index.conflicts(path, exclude=job.id):
                raise OrganizationError(ExecutionCode.BUSY)
            row = self.store.db.execute('SELECT job_id FROM organization_reservations WHERE path_key=?', (path_key(path),)).fetchone()
            if job.state != JobState.COMPLETED and (row is None or row[0] != job.id):
                raise OrganizationError(ExecutionCode.BUSY)
        for volume in before['volumes']:
            for dep in dependencies(self.store.db.cursor(), volume['id']):
                if dep.get('category') == 'organization':
                    sibling = self.store.db.execute('SELECT batch_id FROM organization_jobs WHERE id=?', (dep['id'],)).fetchone()
                    if dep['id'] == job.id or job.batch_id and sibling and sibling[0] == job.batch_id:
                        continue
                raise OrganizationError(ExecutionCode.BUSY, 'quarantine_active_dependency')
        return current

    def paths(self, intent):
        """Scope admission for storage; never expands ordinary executor roots."""
        root, storage = Path(intent['root']), Path(intent['quarantine_target']).parent.parent
        roots = self.store.db.execute('SELECT id,folder FROM root_folders ORDER BY id LIMIT 1001').fetchall()
        if len(roots) > 1000 or ReservationIndex((r[1], str(r[0])) for r in roots).conflicts(str(storage)):
            raise OrganizationError(ExecutionCode.UNSAFE_PATH, 'quarantine_root_overlap')
        for path in (root, Path(intent['original']).parent, storage, Path(intent['quarantine_target'])):
            safe_path(str(path))
        parent = Path(intent['target']).parent
        while not parent.exists():
            parent = parent.parent
        if (not Path(intent['original']).parent.is_dir() or not parent.is_dir()
                or not root.is_dir() or not parent.stat().st_dev
                or parent.stat().st_dev != intent['hash']['stamp'][0]
                or root.stat().st_dev != intent['hash']['stamp'][0]):
            raise OrganizationError(ExecutionCode.UNSAFE_PATH, 'quarantine_device_or_parent_changed')
        keys = {path_key(intent['original']), path_key(intent['quarantine_target'])}
        storage_index = ReservationIndex(((str(storage), 'quarantine'),))
        for n, row in enumerate(self.store.db.execute('''SELECT f.id,f.filepath,q.file_id FROM files f
                LEFT JOIN quarantined_files q ON q.file_id=f.id ORDER BY f.id LIMIT 20001'''), 1):
            if n > 20000:
                raise OrganizationError(ExecutionCode.UNSUPPORTED, 'quarantine_path_bound')
            if row[0] != intent['file_id'] and path_key(row[1]) in keys:
                raise OrganizationError(ExecutionCode.CONFLICT, 'quarantine_foreign_path_owner')
            if row[2] is None and storage_index.conflicts(row[1]):
                raise OrganizationError(ExecutionCode.CONFLICT, 'quarantine_storage_registered')

    def initial(self, job, intent, *, record=True):
        self.check_database(job, intent)
        self.paths(intent)
        if os.path.lexists(intent['target']):
            raise OrganizationError(ExecutionCode.OCCUPIED)
        evidence = self.observe(intent['source'], intent['hash'], moved=intent['inverse'])
        value = dict(artifact=evidence, artifact_digest=digest(canonical(evidence)))
        if record:
            with self.store.transaction():
                self.store.event(job.id, 'validated', value)
        return value

    def start(self, job, intent, ordinal, validation):
        self.check_database(job, intent)
        value = dict(artifact=validation['artifact'])
        with self.store.transaction():
            self.store.checkpoint(job.id, ordinal, StepState.STARTED, value)
        self.executor.hook('after_started', job.id, ordinal)
        return value

    def reconcile(self, job, intent, ordinal, value):
        if ordinal == 1:
            self.check_database(job, intent)
            return False
        source, target = os.path.lexists(intent['source']), os.path.lexists(intent['target'])
        if source == target:
            raise OrganizationError(ExecutionCode.CONFLICT, 'quarantine_ambiguous_paths')
        self.observe(intent['target'] if target else intent['source'], intent['hash'], moved=target or intent['inverse'])
        return target

    def setup(self, intent):
        if intent['inverse']:
            return
        storage = Path(intent['quarantine_target']).parent
        root_parent = Path(intent['root']).parent
        current = root_parent
        for part in storage.relative_to(root_parent).parts:
            current /= part
            safe_path(str(current))
            try:
                current.mkdir()
                sync_directory(str(current.parent))
            except FileExistsError:
                if not current.is_dir():
                    raise OrganizationError(ExecutionCode.UNSAFE_PATH)
            if current.stat().st_dev != intent['hash']['stamp'][0]:
                raise OrganizationError(ExecutionCode.UNSUPPORTED, 'cross_device_quarantine_not_supported')

    def execute(self, job, intent, ordinal, value):
        self.executor.hook('before_effect', job.id, ordinal)
        if ordinal == 0:
            # Hash outside the writer, then recheck the complete descriptor/path
            # stamp inside it. The OS gate and reservations exclude cooperative
            # writers; as with other organizer effects, external writers are not
            # locked. A post-move digest check detects interference, never undoes.
            evidence = self.observe(intent['source'], intent['hash'], moved=intent['inverse'])
            retained = []
            if not intent['inverse']:
                for row in intent['quarantine_before']['files']:
                    if row['id'] in intent['retained_ids']:
                        retained.append((row['filepath'], self.observe(row['filepath'], intent['retained_hashes'][str(row['id'])])))
            with self.store.transaction():
                self.check_database(job, intent)
                self.paths(intent)
                if os.path.lexists(intent['target']):
                    raise OrganizationError(ExecutionCode.OCCUPIED)
                if list(stamp(os.lstat(intent['source']))) != list(evidence['stamp']):
                    raise OrganizationError(ExecutionCode.SOURCE)
                if any(list(stamp(os.lstat(path))) != list(observed['stamp']) for path, observed in retained):
                    raise OrganizationError(ExecutionCode.SOURCE, 'quarantine_retained_copy_changed')
                self.executor.hook('before_quarantine_setup', job.id, ordinal)
                self.setup(intent)
                safe_path(intent['source'])
                safe_path(intent['target'])
                if list(stamp(os.lstat(intent['source']))) != list(evidence['stamp']):
                    raise OrganizationError(ExecutionCode.SOURCE)
                rename_no_replace(intent['source'], intent['target'])
            self.executor.hook('after_effect', job.id, ordinal)
            self.observe(intent['target'], intent['hash'], moved=True)
            self.executor._receipt(job.id, ordinal, value)
            return
        if ordinal != 1:
            raise OrganizationError(ExecutionCode.CORRUPT)
        evidence = self.observe(intent['target'], intent['hash'], moved=True)
        self.executor.hook('before_db_commit', job.id, ordinal)
        with self.store.transaction():
            self.check_database(job, intent)
            self.paths(intent)
            if os.path.lexists(intent['source']) or list(stamp(os.lstat(intent['target']))) != list(evidence['stamp']):
                raise OrganizationError(ExecutionCode.CONFLICT)
            self.reconcile_database(job, intent, ordinal)
            self.store.checkpoint(job.id, ordinal, StepState.SUCCEEDED, value)
            self.executor.hook('quarantine_before_commit', job.id, ordinal)
        self.executor.hook('after_db_commit', job.id, ordinal)

    def reconcile_database(self, job, intent, ordinal):
        fid = intent['file_id']
        db, before = self.store.db, intent['quarantine_before']
        if intent['inverse']:
            db.execute('DELETE FROM quarantined_files WHERE file_id=? AND job_id=?', (fid, intent['quarantine_job']))
            self.executor.hook('quarantine_marker_removed', job.id, ordinal)
        self.executor.hook('quarantine_before_filepath', job.id, ordinal)
        if db.execute('UPDATE files SET filepath=? WHERE id=? AND filepath=?',
                (intent['target'], fid, intent['source'])).rowcount != 1:
            raise OrganizationError(ExecutionCode.CONFLICT)
        if not intent['inverse']:
            self.executor.hook('quarantine_before_marker', job.id, ordinal)
            db.execute('INSERT INTO quarantined_files VALUES(?,?,1,?,?,?)',
                (fid, job.id, intent['original'], intent['quarantine_target'], now()))
            self.executor.hook('quarantine_marker_inserted', job.id, ordinal)
        for key, table, columns in (('direct', 'issues_files', ('file_id', 'issue_id', 'forced')),
                                    ('general', 'volume_files', ('file_id', 'volume_id', 'file_type', 'forced'))):
            for row in before[key]:
                if row['file_id'] != fid:
                    continue
                if intent['inverse']:
                    db.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)})',
                               tuple(row[c] for c in columns))
                else:
                    changed = db.execute(f'DELETE FROM {table} WHERE ' + ' AND '.join(f'{c} IS ?' for c in columns),
                                         tuple(row[c] for c in columns)).rowcount
                    if changed != 1:
                        raise OrganizationError(ExecutionCode.CONFLICT)
            self.executor.hook('quarantine_' + key + '_reconciled', job.id, ordinal)

    def finish(self, job, intent, validation):
        evidence = self.observe(intent['target'], intent['hash'], moved=True)
        with self.store.transaction():
            self.check_database(job, intent)
            if (os.path.lexists(intent['source']) or list(stamp(os.lstat(intent['target']))) != list(evidence['stamp'])
                    or any(s.state != StepState.SUCCEEDED for s in job.steps)):
                raise OrganizationError(ExecutionCode.CONFLICT)
            self.store.state(job.id, JobState.COMPLETED)
            self.store.db.execute('DELETE FROM organization_reservations WHERE job_id=?', (job.id,))
            self.store.db.execute('UPDATE organization_jobs SET claim=NULL WHERE id=?', (job.id,))

    def inverse(self, job, intent):
        result = deepcopy(intent)
        result.update(inverse=True, quarantine_job=job.id, source=intent['target'], target=intent['source'],
                      effects=[RESTORE, RESTORE_RECORD])
        return result

    def preview_undo(self, job, intent):
        reasons = []
        if job.state != JobState.COMPLETED or intent['inverse']:
            reasons.append('completed_forward_job_required')
        if self.store.db.execute('SELECT 1 FROM organization_jobs WHERE inverse_of=?', (job.id,)).fetchone():
            reasons.append('inverse_job_already_exists')
        if not reasons:
            try:
                self.validate(intent)
                self.check_database(job, intent)
                self.paths(intent)
                self.observe(intent['target'], intent['hash'], moved=True)
                if os.path.lexists(intent['source']):
                    reasons.append('restore_path_occupied')
            except (OrganizationError, OSError, ValueError):
                reasons.append('quarantine_restore_state_changed')
        inverse = self.inverse(job, intent) if not reasons else None
        return UndoPreview(job.id, not reasons, intent['target'], intent['source'], tuple(reasons),
                           digest(canonical(inverse)) if inverse else None)

    def inspect(self, job, intent):
        self.validate(intent)
        return dict(id=job.id, state=job.state.value, quarantine_effect=VERSION,
            file_id=intent['file_id'], inverse_of=job.inverse_of, error=job.error,
            steps=[dict(ordinal=s.ordinal, kind=s.kind, state=s.state.value) for s in job.steps])

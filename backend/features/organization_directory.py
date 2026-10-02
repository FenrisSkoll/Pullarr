"""Versioned volume-tree effects dispatched by OrganizationExecutor.

Uses its existing job/step/event state machine, OS gate and SQLite checkpoints.
No queue, independent journal, root migration, merging, scanning or renaming of
children. Registration is internal to the separately reviewed folder service.
"""

import json
import os
from dataclasses import asdict
from pathlib import Path

from backend.base.folder_inventory import InventoryLimits
from backend.base.library_health import canonical, fingerprint
from backend.base.organization_job import (EXECUTOR_POLICY, ExecutionCode,
                                           JobState, OrganizationError,
                                           StepState, UndoPreview)
from backend.implementations.folder_inventory import inspect_folder
from backend.implementations.organization_filesystem import rename_no_replace
from backend.internals.folder_ownership import load_ownership
from backend.internals.organization_jobs import MAX_PAYLOAD
from backend.internals.organization_reservations import path_key
from backend.internals.switch_review import dependencies

VERSION = 'volume-tree/v1'
MOVE = 'relocate_volume_tree/v1'
RECORD = 'reconcile_volume_tree/v1'


def projection(inventory):
    """Expected post-namespace identity; not a cryptographic contents proof.

    Only the moved top directory's ctime is omitted: rename changes it on Linux.
    Descendant paths are relative; descendant stamps retain ctime. Parent/root
    directory timestamps are not tree content and intentionally absent.
    """
    if not inventory.complete or inventory.source_stamp is None or inventory.root_stamp is None:
        raise OrganizationError(ExecutionCode.SOURCE, 'Complete safe tree required')
    top = asdict(inventory.source_stamp)
    top.pop('ctime_ns')
    return dict(version=VERSION, root=[inventory.root_stamp.device, inventory.root_stamp.inode], top=top,
        entries=[[e.relative, e.kind, asdict(e.stamp)] for e in inventory.entries])


def after_state(state_json, source, target, custom):
    value = json.loads(state_json)
    value['volume']['folder'] = target
    value['volume']['custom_folder'] = int(custom)
    for item in value['files']:
        item['filepath'] = str(Path(target) / Path(item['filepath']).relative_to(source))
    for item in value['registrations']:
        item['path'] = str(Path(target) / Path(item['path']).relative_to(source))
    return canonical(value)


def reconciliation_identity(state_json):
    """Fields needed to finish an already-proven historical namespace effect.

    Never writes metadata/config; those cease to be naming inputs after the move.
    Root/folder/custom ownership, file identity/size/path, all links, membership
    and exact selected authority remain required. Fetch timestamps are not IDs.
    """
    value = json.loads(state_json)
    volume = value['volume']
    return dict(volume={k: volume[k] for k in ('id', 'root_folder', 'folder', 'custom_folder')},
        root=value['root'], authority=value['authority'], files=value['files'],
        registrations=value['registrations'],
        issues=[(i['id'], i['volume_id']) for i in value['issues']],
        refs=[(r['volume_id'], r['provider'], r['provider_id']) for r in value['refs']])


class DirectoryEffect:
    def __init__(self, executor):
        self.executor = executor
        self.store = executor.store

    def validate(self, intent):
        try:
            if (intent['directory_effect'] != VERSION or intent['version'] != EXECUTOR_POLICY
                    or intent['effects'] != [MOVE, RECORD] or type(intent['inverse']) is not bool
                    or intent['source'] == intent['target']
                    or path_key(intent['source']) == path_key(intent['target'])):
                raise ValueError()
            for path in (intent['source'], intent['target']):
                self.executor._scope(path)
                if Path(intent['root']) not in Path(path).parents:
                    raise ValueError()
            if (Path(intent['source']) in Path(intent['target']).parents
                    or Path(intent['target']) in Path(intent['source']).parents):
                raise ValueError()
            before = json.loads(intent['tree_database_before'])
            if (before['volume']['id'] != intent['volume_id'] or before['volume']['folder'] != intent['source']
                    or before['root'][1] != intent['root']
                    or intent['tree_database_after'] != after_state(intent['tree_database_before'],
                        intent['source'], intent['target'], intent['custom_after'])
                    or intent['tree_projection']['version'] != VERSION):
                raise ValueError()
            if len(canonical(intent)) > MAX_PAYLOAD:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise OrganizationError(ExecutionCode.CORRUPT, 'Invalid directory effect') from None

    def check_database(self, job, intent):
        expected = intent['tree_database_before']
        if any(s.kind == RECORD and s.state == StepState.SUCCEEDED for s in job.steps):
            expected = intent['tree_database_after']
        batch = load_ownership(self.store.db.cursor(), (intent['volume_id'],))
        moved = (job.steps[0].state != StepState.PENDING and not os.path.lexists(intent['source'])
                 and os.path.lexists(intent['target']))
        current = batch.volumes[0].state_json
        matches = (reconciliation_identity(current) == reconciliation_identity(expected)
                   if moved else current == expected)
        if not matches:
            raise OrganizationError(ExecutionCode.STALE, 'Folder metadata/authority/ownership changed')
        if moved and self.observe(intent, intent['target']) != intent['tree_projection']:
            raise OrganizationError(ExecutionCode.CONFLICT, 'Directory projection mismatch')
        if any(d.get('category') != 'organization' or d.get('id') != job.id
               for d in dependencies(self.store.db.cursor(), intent['volume_id'])):
            raise OrganizationError(ExecutionCode.BUSY, 'Active volume dependency')
        for path in (intent['source'], intent['target']):
            if batch.reservations.conflicts(path, exclude=job.id):
                raise OrganizationError(ExecutionCode.BUSY)
            row = self.store.db.execute('SELECT job_id FROM organization_reservations WHERE path_key=?', (path_key(path),)).fetchone()
            if job.state != JobState.COMPLETED and (row is None or row[0] != job.id):
                raise OrganizationError(ExecutionCode.BUSY)
        # Recheck global destination ownership independently of sibling jobs'
        # expected folder changes; a batch must not stale itself after job one.
        for vid, folder in batch.owners:
            if vid != intent['volume_id'] and (overlaps(folder, intent['target']) or overlaps(folder, intent['source'])):
                raise OrganizationError(ExecutionCode.CONFLICT, 'Target ownership changed')
        if any(rid != batch.volumes[0].root_id and (overlaps(root, intent['source']) or overlaps(root, intent['target']))
               for rid, root in batch.roots):
            raise OrganizationError(ExecutionCode.CONFLICT, 'Root ownership changed')
        mapped = {r['id'] for r in json.loads(expected)['files']}
        if any(fid not in mapped and overlaps(path, intent['target']) for fid, path, _ in batch.all_files):
            raise OrganizationError(ExecutionCode.CONFLICT, 'Target file ownership changed')
        return batch.volumes[0]

    def observe(self, intent, location):
        report = inspect_folder(intent['root'], location, intent['volume_id'],
                                limits=InventoryLimits(entries=20000, path_bytes=4 * 1024 * 1024, seconds=120))
        return projection(report)

    def initial(self, job, intent, *, record=True):
        self.check_database(job, intent)
        if os.path.lexists(intent['target']):
            raise OrganizationError(ExecutionCode.OCCUPIED)
        if self.observe(intent, intent['source']) != intent['tree_projection']:
            raise OrganizationError(ExecutionCode.SOURCE)
        value = dict(artifact=intent['tree_projection'], artifact_digest=fingerprint(intent['tree_projection']))
        if record:
            with self.store.transaction():
                self.store.event(job.id, 'validated', value)
        return value

    def start(self, job, intent, ordinal, validation):
        self.check_database(job, intent)
        value = dict(tree_projection=validation['artifact'])
        with self.store.transaction():
            self.store.checkpoint(job.id, ordinal, StepState.STARTED, value)
        self.executor.hook('after_started', job.id, ordinal)
        return value

    def reconcile(self, job, intent, ordinal, value):
        if job.steps[ordinal].kind == RECORD:
            # DB updates and SUCCEEDED receipt share one commit.
            self.check_database(job, intent)
            return False
        source, target = os.path.lexists(intent['source']), os.path.lexists(intent['target'])
        if source == target:
            raise OrganizationError(ExecutionCode.CONFLICT, 'Ambiguous directory relocation')
        location = intent['source'] if source else intent['target']
        if self.observe(intent, location) != intent['tree_projection']:
            raise OrganizationError(ExecutionCode.CONFLICT, 'Directory projection mismatch')
        return target

    def execute(self, job, intent, ordinal, value):
        self.executor.hook('before_effect', job.id, ordinal)
        if job.steps[ordinal].kind == MOVE:
            # No content hashing. Complete stat/membership recheck is serialized
            # with cooperating DB writers; the namespace operation is exclusive.
            with self.store.transaction():
                self.check_database(job, intent)
                if os.path.lexists(intent['target']):
                    raise OrganizationError(ExecutionCode.OCCUPIED)
                if self.observe(intent, intent['source']) != intent['tree_projection']:
                    raise OrganizationError(ExecutionCode.SOURCE)
                rename_no_replace(intent['source'], intent['target'])
            self.executor.hook('after_effect', job.id, ordinal)
            if self.observe(intent, intent['target']) != intent['tree_projection']:
                raise OrganizationError(ExecutionCode.CONFLICT)
            self.executor._receipt(job.id, ordinal, value)
        elif job.steps[ordinal].kind == RECORD:
            if os.path.lexists(intent['source']) or self.observe(intent, intent['target']) != intent['tree_projection']:
                raise OrganizationError(ExecutionCode.CONFLICT)
            self.executor.hook('before_db_commit', job.id, ordinal)
            with self.store.transaction():
                self.check_database(job, intent)
                if os.path.lexists(intent['source']) or self.observe(intent, intent['target']) != intent['tree_projection']:
                    raise OrganizationError(ExecutionCode.CONFLICT)
                old = json.loads(intent['tree_database_before'])
                new = json.loads(intent['tree_database_after'])
                for before, after in zip(old['files'], new['files']):
                    changed = self.store.db.execute('UPDATE files SET filepath=? WHERE id=? AND filepath=?',
                        (after['filepath'], before['id'], before['filepath'])).rowcount
                    if changed != 1:
                        raise OrganizationError(ExecutionCode.CONFLICT)
                    self.executor.hook('directory_path_updated', job.id, ordinal)
                self.executor.hook('before_directory_volume_update', job.id, ordinal)
                self.store.db.execute('UPDATE volumes SET folder=?,custom_folder=? WHERE id=?',
                    (intent['target'], int(intent['custom_after']), intent['volume_id']))
                self.store.checkpoint(job.id, ordinal, StepState.SUCCEEDED, value)
            self.executor.hook('after_db_commit', job.id, ordinal)
        else:
            raise OrganizationError(ExecutionCode.CORRUPT)

    def finish(self, job, intent, validation):
        with self.store.transaction():
            self.check_database(job, intent)
            if (os.path.lexists(intent['source']) or self.observe(intent, intent['target']) != intent['tree_projection']
                    or any(s.state != StepState.SUCCEEDED for s in job.steps)):
                raise OrganizationError(ExecutionCode.CONFLICT)
            self.store.state(job.id, JobState.COMPLETED)
            self.store.db.execute('DELETE FROM organization_reservations WHERE job_id=?', (job.id,))
            self.store.db.execute('UPDATE organization_jobs SET claim=NULL WHERE id=?', (job.id,))

    def inverse(self, job, intent):
        value = dict(intent)
        current = load_ownership(self.store.db.cursor(), (intent['volume_id'],)).volumes[0].state_json
        restore_custom = bool(json.loads(intent['tree_database_before'])['volume']['custom_folder'])
        value.update(source=intent['target'], target=intent['source'], inverse=True,
            tree_database_before=current,
            tree_database_after=after_state(current, intent['target'], intent['source'], restore_custom),
            custom_after=restore_custom)
        return value

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
                if os.path.lexists(intent['source']) or self.observe(intent, intent['target']) != intent['tree_projection']:
                    reasons.append('tree_changed_or_restore_path_occupied')
            except (OrganizationError, OSError, ValueError):
                reasons.append('recorded_state_changed')
        inverse = self.inverse(job, intent) if not reasons else None
        return UndoPreview(job.id, not reasons, intent['target'], intent['source'], tuple(reasons),
                           fingerprint(inverse) if inverse else None)

    def inspect(self, job, intent):
        self.validate(intent)
        observations = {}
        for key in ('source', 'target'):
            try:
                value = self.observe(intent, intent[key]) if os.path.lexists(intent[key]) else None
                observations[key] = dict(exists=value is not None,
                    projection_matches=value == intent['tree_projection'])
            except (OrganizationError, OSError):
                observations[key] = dict(unavailable=True)
        try:
            self.check_database(job, intent)
            observations['database'] = dict(matches=True)
        except (OrganizationError, OSError, ValueError):
            observations['database'] = dict(matches=False)
        return dict(id=job.id, state=job.state.value, source=job.source, target=job.target,
            directory_effect=VERSION, observations=observations, error=job.error,
            steps=[dict(ordinal=s.ordinal, kind=s.kind, state=s.state.value) for s in job.steps],
            inverse_of=job.inverse_of)


def overlaps(left, right):
    a, b = path_key(left), path_key(right)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)

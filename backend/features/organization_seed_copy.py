"""Journaled, source-preserving staging for managed torrent payloads.

The source is never deleted. An incomplete exclusive copy is reviewable, not
silently replaced on restart. Subsequent organizer effects own library admission.
"""
import errno
import os
from pathlib import Path

from backend.base.organization_job import (EXECUTOR_POLICY, ExecutionCode,
                                           JobState, OrganizationError,
                                           StepState)
from backend.implementations.organization_filesystem import (artifact,
                                                             execution_gate,
                                                             matches,
                                                             safe_path,
                                                             sync_directory)
from backend.internals.organization_jobs import canonical, digest

VERSION = 'seed-copy/v1'
EFFECTS = ['preserve_seed_payload/v1']


def register_seed_copy(executor, source, target, batch_id, *, create_parent=False):
    executor._scope(source)
    executor._scope(target)
    parent = Path(target).parent
    if source == target or not (parent.parent if create_parent else parent).is_dir():
        raise OrganizationError(ExecutionCode.UNSAFE_PATH)
    existing = executor.store.db.execute('SELECT id FROM organization_jobs WHERE batch_id=?', (batch_id,)).fetchone()
    if existing:
        return existing[0]
    observed = artifact(source)
    intent = dict(version=EXECUTOR_POLICY, seed_copy_effect=VERSION, effects=EFFECTS,
                  source=source, target=target, incoming=observed, inverse=False, create_parent=create_parent)
    with execution_gate(executor.store.path):
        return executor.store.create(intent, digest(canonical(intent)),
            tuple(os.path.normpath(p).casefold() for p in ((source, target, str(parent)) if create_parent else (source, target))), batch_id)


class SeedCopyEffect:
    def __init__(self, executor):
        self.e, self.store = executor, executor.store

    def validate(self, intent):
        if (intent.get('seed_copy_effect') != VERSION or intent.get('version') != EXECUTOR_POLICY
                or intent.get('effects') != EFFECTS or intent.get('inverse') is not False
                or intent.get('source') == intent.get('target')):
            raise OrganizationError(ExecutionCode.CORRUPT)
        for key in ('source', 'target'):
            self.e._scope(intent[key])
        if intent.get('create_parent') and not Path(intent['target']).parent.name.startswith('.pullarr-seed-'):
            raise OrganizationError(ExecutionCode.CORRUPT)

    def check_database(self, job, intent):
        if self.store.db.execute('SELECT 1 FROM files WHERE filepath IN (?,?)',
                                 (intent['source'], intent['target'])).fetchone():
            raise OrganizationError(ExecutionCode.CONFLICT)
        return {}

    def initial(self, job, intent, record=True):
        self.check_database(job, intent)
        if not matches(intent['source'], intent['incoming']) or os.path.lexists(intent['target']):
            raise OrganizationError(ExecutionCode.SOURCE)
        if intent.get('create_parent') and os.path.lexists(Path(intent['target']).parent):
            raise OrganizationError(ExecutionCode.OCCUPIED)
        value = dict(artifact=intent['incoming'], artifact_digest=digest(canonical(intent['incoming'])))
        if record:
            with self.store.transaction():
                self.store.event(job.id, 'validated', value)
        return value

    def start(self, job, intent, ordinal, validation):
        value = dict(artifact_before=intent['incoming'])
        with self.store.transaction():
            self.store.checkpoint(job.id, ordinal, StepState.STARTED, value)
        self.e.hook('after_started', job.id, ordinal)
        return value

    def reconcile(self, job, intent, ordinal, value):
        if not matches(intent['source'], intent['incoming']):
            raise OrganizationError(ExecutionCode.SOURCE)
        if not os.path.lexists(intent['target']):
            return False
        copied = artifact(intent['target'])
        if any(copied[k] != intent['incoming'][k] for k in ('size', 'sha256')):
            raise OrganizationError(ExecutionCode.CONFLICT)
        value['artifact_after'] = copied
        value['import_method'] = 'hardlink' if os.path.samefile(intent['source'], intent['target']) else 'copy'
        return True

    def execute(self, job, intent, ordinal, value):
        if not matches(intent['source'], intent['incoming']):
            raise OrganizationError(ExecutionCode.SOURCE)
        safe_path(intent['target'])
        if intent.get('create_parent'):
            parent = Path(intent['target']).parent
            if not parent.exists():
                parent.mkdir()
                sync_directory(str(parent.parent))
            elif any(parent.iterdir()):
                raise OrganizationError(ExecutionCode.OCCUPIED)
        try:
            os.link(intent['source'], intent['target'])
        except OSError as error:
            if error.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP):
                raise
            # Exclusive creation: never replace an unrelated or partial artifact.
            with open(intent['source'], 'rb') as source, open(intent['target'], 'xb') as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        sync_directory(str(Path(intent['target']).parent))
        self.e.hook('after_effect', job.id, ordinal)
        if not self.reconcile(job, intent, ordinal, value):
            raise OrganizationError(ExecutionCode.CONFLICT)
        self.e._receipt(job.id, ordinal, value)

    def finish(self, job, intent, validation):
        if not self.reconcile(job, intent, 0, {}):
            raise OrganizationError(ExecutionCode.CONFLICT)
        with self.store.transaction():
            self.store.state(job.id, JobState.COMPLETED)
            self.store.db.execute('DELETE FROM organization_reservations WHERE job_id=?', (job.id,))
            self.store.db.execute('UPDATE organization_jobs SET claim=NULL WHERE id=?', (job.id,))

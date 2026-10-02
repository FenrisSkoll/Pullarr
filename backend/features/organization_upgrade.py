"""Verified replacement effect on the existing OrganizationJob executor.

No route accepts this intent. The intake's canonical matcher supplies identity;
quality verification precedes registration. Old bytes survive an atomic swap
until the ownership/assessment/provenance transaction has committed.
"""

import json
import os
from pathlib import Path

from backend.base.identification import MatchState
from backend.base.organization_job import (EXECUTOR_POLICY, ExecutionCode,
                                           JobState, OrganizationError,
                                           StepState)
from backend.base.quality import ClaimedQuality, QualityError, compare
from backend.implementations.file_quality import analyze
from backend.implementations.organization_filesystem import (artifact,
                                                             execution_gate,
                                                             matches,
                                                             safe_path,
                                                             sync_directory)
from backend.internals.organization_jobs import canonical, digest
from backend.internals.quality import QualityStore

VERSION = 'verified-upgrade/v1'
EFFECTS = ['preserve_upgrade_original/v1', 'replace_verified_upgrade/v1',
           'reconcile_verified_upgrade/v1', 'retire_upgrade_original/v1']


def register_upgrade(executor, plan, provenance_id, batch_id):
    store = executor.store
    existing = store.db.execute('SELECT id FROM organization_jobs WHERE batch_id=?', (batch_id,)).fetchone()
    if existing:
        return existing[0]
    selected = plan.identification.selected
    if (plan.identification.state != MatchState.AUTOMATIC or selected is None
            or len(selected.local_issue_ids) != 1):
        raise QualityError('identity_failed')
    issue_id = selected.local_issue_ids[0]
    quality = QualityStore(store.db.cursor())
    provenance = quality.detail(provenance_id)
    state = quality.issue_states([issue_id])[0]
    if (provenance['reason'] != 'upgrade' or provenance['issue_id'] != issue_id
            or not state['upgrade_eligible'] or len(state['files']) != 1):
        raise QualityError('target_changed')
    current = state['files'][0]
    profile = state['assignment']['profile']
    if (profile['id'], profile['revision']) != (provenance['profile_id'], provenance['profile_revision']):
        raise QualityError('profile_changed')
    fid = current['file_id']
    # A shared direct file or content-claim artifact is not an issue replacement.
    if store.db.execute('SELECT COUNT(*) FROM issues_files WHERE file_id=?', (fid,)).fetchone()[0] != 1:
        raise QualityError('shared_file')
    if store.db.execute('SELECT 1 FROM volume_files WHERE file_id=?', (fid,)).fetchone():
        raise QualityError('shared_file')
    if store.db.execute('SELECT 1 FROM file_content_coverage WHERE file_id=? AND retired_at IS NULL', (fid,)).fetchone():
        raise QualityError('content_claim_file')
    target = store.db.execute('SELECT filepath FROM active_files WHERE id=?', (fid,)).fetchone()[0]
    source = plan.source_path
    if store.db.execute('SELECT 1 FROM files WHERE filepath=?', (source,)).fetchone():
        raise QualityError('source_already_owned')
    executor._scope(source)
    executor._scope(target)
    incoming = analyze(source, verify_pixels=True)
    if incoming['sha256'] == current['facts']['sha256']:
        raise QualityError('equal')
    old = artifact(target)
    if old['sha256'] != current['facts']['sha256']:
        raise QualityError('current_file_changed')
    verified = compare(profile, ClaimedQuality(**provenance['claims']),
                       current=ClaimedQuality(**current['claims']), verified=incoming, post_import=True)
    if verified['result'] != 'upgrade':
        raise QualityError(verified['reason'] or verified['result'])
    observed = artifact(source)
    if observed['sha256'] != incoming['sha256'] or observed['device'] != old['device']:
        raise QualityError('same_device_required')
    backup = str(Path(target).parent / ('.kapowarr-upgrade-'+provenance_id+'.tmp'))
    safe_path(backup)
    if os.path.lexists(backup):
        raise QualityError('backup_occupied')
    volume = store.db.execute('SELECT root_folder,folder FROM volumes WHERE id=?', (state['volume_id'],)).fetchone()
    root = store.db.execute('SELECT folder FROM root_folders WHERE id=?', (volume[0],)).fetchone()[0]
    intent = dict(version=EXECUTOR_POLICY, upgrade_effect=VERSION, effects=EFFECTS,
        source=source, target=target, backup=backup, folder=volume[1], root=root,
        volume_id=state['volume_id'], issue_id=issue_id, file_id=fid, provenance_id=provenance_id,
        previous_acquisition=current['acquisition_id'], old=old, incoming=observed,
        facts=incoming, profile_id=profile['id'], profile_revision=profile['revision'], inverse=False)
    with execution_gate(store.path):
        return store.create(intent, digest(canonical(intent)),
            tuple(os.path.normpath(p).casefold() for p in (source,target,backup)), batch_id)


class UpgradeEffect:
    def __init__(self, executor):
        self.e, self.store = executor, executor.store

    def validate(self, intent):
        try:
            if (intent['upgrade_effect'] != VERSION or intent['version'] != EXECUTOR_POLICY
                    or intent['effects'] != EFFECTS or intent['inverse'] is not False
                    or intent['backup'] != str(Path(intent['target']).parent / ('.kapowarr-upgrade-'+intent['provenance_id']+'.tmp'))
                    or intent['source'] == intent['target'] or intent['old']['sha256'] == intent['incoming']['sha256']):
                raise ValueError()
            for key in ('source', 'target', 'backup'):
                self.e._scope(intent[key])
        except (ValueError, KeyError, TypeError):
            raise OrganizationError(ExecutionCode.CORRUPT) from None

    def check_database(self, job, intent):
        row = self.store.db.execute('SELECT filepath,size FROM active_files WHERE id=?', (intent['file_id'],)).fetchone()
        committed = job.steps[2].state == StepState.SUCCEEDED
        size = intent['incoming']['size'] if committed else intent['old']['size']
        if row is None or tuple(row) != (intent['target'], size):
            raise OrganizationError(ExecutionCode.CONFLICT)
        links = self.store.db.execute('SELECT issue_id FROM issues_files WHERE file_id=?', (intent['file_id'],)).fetchall()
        if [r[0] for r in links] != [intent['issue_id']]:
            raise OrganizationError(ExecutionCode.CONFLICT)
        if self.store.db.execute('SELECT 1 FROM file_content_coverage WHERE file_id=? AND retired_at IS NULL', (intent['file_id'],)).fetchone():
            raise OrganizationError(ExecutionCode.CONFLICT)
        if self.store.db.execute('SELECT 1 FROM volume_files WHERE file_id=?', (intent['file_id'],)).fetchone():
            raise OrganizationError(ExecutionCode.CONFLICT)
        if self.store.db.execute('SELECT 1 FROM files WHERE filepath=? AND id<>?', (intent['source'], intent['file_id'])).fetchone():
            raise OrganizationError(ExecutionCode.CONFLICT)
        if not committed:
            state = QualityStore(self.store.db.cursor()).issue_states([intent['issue_id']])[0]
            profile = state['assignment']['profile']
            if (profile is None or (profile['id'],profile['revision']) != (intent['profile_id'],intent['profile_revision'])
                    or not state['monitored'] or not state['volume_monitored']):
                raise OrganizationError(ExecutionCode.STALE)
        return {}

    def initial(self, job, intent, record=True):
        self.check_database(job, intent)
        if (not matches(intent['source'], intent['incoming']) or not matches(intent['target'], intent['old'])
                or os.path.lexists(intent['backup'])):
            raise OrganizationError(ExecutionCode.SOURCE)
        value = dict(artifact=intent['incoming'], artifact_digest=digest(canonical(intent['incoming'])))
        if record:
            with self.store.transaction():
                self.store.event(job.id, 'validated', value)
        return value

    def start(self, job, intent, ordinal, validation):
        self.check_database(job, intent)
        value = dict(artifact_before=intent['incoming'])
        with self.store.transaction():
            self.store.checkpoint(job.id, ordinal, StepState.STARTED, value)
        self.e.hook('after_started', job.id, ordinal)
        return value

    def reconcile(self, job, intent, ordinal, value):
        if ordinal == 0:
            if os.path.lexists(intent['backup']):
                if not matches(intent['backup'], intent['old']):
                    raise OrganizationError(ExecutionCode.CONFLICT)
                return True
            if not matches(intent['target'], intent['old']):
                raise OrganizationError(ExecutionCode.SOURCE)
            return False
        if ordinal == 1:
            if not matches(intent['backup'], intent['old']):
                raise OrganizationError(ExecutionCode.SOURCE)
            if not os.path.lexists(intent['source']) and matches(intent['target'], intent['incoming']):
                return True
            if matches(intent['source'], intent['incoming']) and matches(intent['target'], intent['old']):
                return False
            raise OrganizationError(ExecutionCode.CONFLICT)
        if ordinal == 2:
            if not matches(intent['target'], intent['incoming']):
                raise OrganizationError(ExecutionCode.SOURCE)
            return False  # DB and checkpoint commit together.
        if not matches(intent['target'], intent['incoming']):
            raise OrganizationError(ExecutionCode.SOURCE)
        if not os.path.lexists(intent['backup']):
            return True
        if not matches(intent['backup'], intent['old']):
            raise OrganizationError(ExecutionCode.CONFLICT)
        return False

    def execute(self, job, intent, ordinal, value):
        self.check_database(job, intent)
        if ordinal == 0:
            # Exclusive hard link preserves old bytes without temporary absence
            # at the library path. It cannot overwrite another backup.
            if not matches(intent['target'], intent['old']):
                raise OrganizationError(ExecutionCode.SOURCE)
            os.link(intent['target'], intent['backup'])
            sync_directory(str(Path(intent['target']).parent))
        elif ordinal == 1:
            if not (matches(intent['source'], intent['incoming']) and matches(intent['target'], intent['old'])
                    and matches(intent['backup'], intent['old'])):
                raise OrganizationError(ExecutionCode.SOURCE)
            os.replace(intent['source'], intent['target'])
            sync_directory(str(Path(intent['target']).parent))
            sync_directory(str(Path(intent['source']).parent))
        elif ordinal == 2:
            with self.store.transaction():
                self.check_database(job, intent)
                if not matches(intent['target'], intent['incoming']):
                    raise OrganizationError(ExecutionCode.SOURCE)
                self.store.db.execute('UPDATE files SET size=? WHERE id=?', (intent['incoming']['size'],intent['file_id']))
                quality = QualityStore(self.store.db.cursor())
                assessment = quality.assessment(intent['file_id'], intent['facts'])
                self.store.db.execute('''UPDATE acquisition_provenance SET state='imported',file_id=?,assessment_id=?,
                    supersedes=?,updated_at=? WHERE id=?''', (intent['file_id'],assessment,intent['previous_acquisition'],
                    quality.clock(),intent['provenance_id']))
                self.store.checkpoint(job.id, ordinal, StepState.SUCCEEDED, value)
            self.e.hook('after_effect',job.id,ordinal)
            return
        else:
            if not matches(intent['backup'], intent['old']) or not matches(intent['target'], intent['incoming']):
                raise OrganizationError(ExecutionCode.SOURCE)
            os.unlink(intent['backup'])
            sync_directory(str(Path(intent['target']).parent))
        self.e.hook('after_effect',job.id,ordinal)
        self.e._receipt(job.id,ordinal,value)

    def finish(self, job, intent, validation):
        self.check_database(job,intent)
        if not matches(intent['target'],intent['incoming']) or os.path.lexists(intent['backup']) or os.path.lexists(intent['source']):
            raise OrganizationError(ExecutionCode.CONFLICT)
        with self.store.transaction():
            self.store.state(job.id,JobState.COMPLETED)
            self.store.db.execute('DELETE FROM organization_reservations WHERE job_id=?',(job.id,))
            self.store.db.execute('UPDATE organization_jobs SET claim=NULL WHERE id=?',(job.id,))

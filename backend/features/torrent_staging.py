"""Admit torrent source copies through OrganizationJobs before ordinary intake."""
import json
from hashlib import sha256
from pathlib import Path

from backend.base.acquisition_intake import IntakeErrorCode as E, IntakeFailure
from backend.base.organization_job import JobState
from backend.features.organization_seed_copy import register_seed_copy
from backend.implementations.organization_filesystem import artifact, safe_path
from backend.internals.organization_jobs import canonical


def staging_root(store, volume_id):
    row = store.db.execute('SELECT folder FROM volumes WHERE id=?', (volume_id,)).fetchone()
    if row is None or not row[0] or not Path(row[0]).is_dir():
        raise IntakeFailure(E.PATH_UNAVAILABLE)
    safe_path(row[0])
    return row[0]


def stage_payloads(store, executor, intake_id, paths, volume_id):
    root = staging_root(store, volume_id)
    result = []
    for path in paths:
        source = Path(path)
        token = sha256(canonical((intake_id, path)).encode()).hexdigest()[:24]
        target = str(Path(root) / ('.pullarr-seed-' + token) / source.name)
        job_id = register_seed_copy(executor, path, target, 'seed-stage:' + token, create_parent=True)
        intent = executor.store.intent(job_id)
        store.db.execute('''INSERT OR IGNORE INTO acquisition_seed_artifacts
            (intake_id,source_path,staged_path,copy_job_id,source_identity) VALUES(?,?,?,?,?)''',
            (intake_id, path, target, job_id, canonical(intent['incoming'])))
        job = executor.apply_job(job_id)
        if job.state != JobState.COMPLETED:
            raise IntakeFailure(E.RECOVERY)
        if artifact(path) != intent['incoming']:
            raise IntakeFailure(E.UNSTABLE)
        method = json.loads(job.steps[0].evidence)['import_method']
        store.db.execute('UPDATE acquisition_seed_artifacts SET import_method=? WHERE intake_id=? AND source_path=?',
                         (method, intake_id, path))
        result.append(target)
    return tuple(result)

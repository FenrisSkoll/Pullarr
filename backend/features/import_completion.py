"""Server-authoritative terminal receipt for reviewed Library Import jobs."""

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from backend.base.definitions import FileConstants
from backend.base.organization_job import (JobState, OrganizationError,
                                           StepState)
from backend.features.managed_permissions import (managed_directory_problem,
                                                  managed_file_problem)
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.organization_filesystem import matches, safe_path


def _maintained_archive(db: sqlite3.Connection, executor: OrganizationExecutor,
                        original: str, expected: dict) -> tuple[str, dict, str | None, int | None]:
    """Follow only completed, byte-verified archive journals for this import."""
    path, artifact = original, expected
    last_job, file_id = None, None
    seen = set()
    for _ in range(100):
        rows = db.execute('''SELECT id FROM organization_jobs
            WHERE state='completed' AND json_extract(intent,'$.archive_effect')='archive-normalization/v1'
            AND json_extract(intent,'$.original')=? ORDER BY created_at,id LIMIT 101''', (path,)).fetchall()
        if len(rows) > 100:
            break
        candidate = None
        for row in rows:
            if row[0] in seen:
                continue
            intent = executor.store.intent(row[0])
            old = intent.get('old', {})
            if (old.get('sha256') == artifact.get('sha256') and
                    old.get('size') == artifact.get('size') and
                    (file_id is None or intent.get('file_id') == file_id)):
                candidate = (row[0], intent)
                break
        if candidate is None:
            break
        job_id, intent = candidate
        job = executor.store.get(job_id)
        if (job.state != JobState.COMPLETED or any(step.state != StepState.SUCCEEDED
                for step in job.steps) or not intent.get('receipt', {}).get('pages_preserved')):
            break
        seen.add(job_id)
        path, artifact = intent['target'], intent['incoming']
        file_id, last_job = intent['file_id'], job_id
    return path, artifact, last_job, file_id


def verify_import_job(database: str, job_id: str, plan: dict) -> dict:
    """Verify journal bytes, exact canonical owner and *reviewed* issue set.

    This read-only check is deliberately repeated for historical reads. A prior
    completed journal state alone is not a current claim that the file exists.
    """
    result = dict(source=plan['source'], target=plan.get('target'),
                  publication=plan.get('publication'), authority=plan.get('authority'),
                  issue_ids=plan.get('issue_ids', []),
                  issue_labels=plan.get('issue_labels', []),
                  job_id=job_id, state='recovery_required', error='verification_failed')
    if plan.get('status') != 'ready' or not plan.get('target'):
        result['error'] = 'plan_not_ready'
        return result
    try:
        with closing(sqlite3.connect(database)) as db:
            row = db.execute('''SELECT v.id,v.folder,v.metadata_provider,e.provider_id,r.folder
                FROM volumes v JOIN volume_external_ids e ON e.volume_id=v.id
                    AND e.provider=v.metadata_provider
                JOIN root_folders r ON r.id=v.root_folder
                WHERE v.folder=?''', (str(Path(plan['target']).parent),)).fetchone()
            if row is None:
                result['error'] = 'canonical_publication_missing'
                return result
            volume_id, folder, provider, provider_id, root = row
            if (provider != plan['authority']['provider'] or str(provider_id) != str(plan['authority']['id'])
                    or not Path(plan['target']).is_relative_to(root)):
                result['error'] = 'publication_identity_changed'
                return result
            executor = OrganizationExecutor(database, (root,))
            try:
                job = executor.store.get(job_id)
                intent = executor.store.intent(job_id)
                if (job.state != JobState.COMPLETED or not intent.get('library_import')
                        or job.source != plan['source'] or job.target != plan['target']
                        or intent['volume_id'] != volume_id or intent['folder'] != folder
                        or any(step.state != StepState.SUCCEEDED for step in job.steps)):
                    result['state'] = job.state.value
                    result['error'] = job.error or 'journal_not_complete'
                    return result
                expected = executor._expected_artifact(job, executor._validation(job_id))
                if expected.get('sha256') != plan.get('validated_archive_sha256'):
                    result['error'] = 'archive_validation_evidence_missing_or_changed'
                    return result
                active_target, active_artifact, maintenance_job, maintained_id = _maintained_archive(
                    db, executor, plan['target'], expected)
                safe_path(active_target)
                info = os.stat(active_target, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or Path(active_target).suffix.lower() not in
                        {suffix.lower() for suffix in FileConstants.CONTENT_EXTENSIONS}
                        or Path(active_target).parent != Path(folder)
                        or not matches(active_target, active_artifact)):
                    result['error'] = 'archive_missing_or_changed'
                    return result
                if managed_directory_problem(folder) or managed_file_problem(active_target):
                    result['error'] = 'destination_permission_denied'
                    return result
                owned = db.execute('SELECT id FROM files WHERE filepath=?', (active_target,)).fetchall()
                if len(owned) != 1 or maintained_id is not None and owned[0][0] != maintained_id:
                    result['error'] = 'file_record_missing_or_duplicate'
                    return result
                file_id = owned[0][0]
                links = db.execute('''SELECT i.id,i.volume_id,e.provider,e.provider_id
                    FROM issues_files b JOIN issues i ON i.id=b.issue_id
                    LEFT JOIN issue_external_ids e ON e.issue_id=i.id AND e.provider=?
                    WHERE b.file_id=?''', (provider, file_id)).fetchall()
                if (len(links) != len(plan['issue_ids']) or
                        any(link[1] != volume_id or link[2] != provider for link in links) or
                        {str(link[3]) for link in links} != set(plan['issue_ids'])):
                    result['error'] = 'reviewed_issue_links_missing_or_changed'
                    return result
                result.update(state='completed', error=None, file_id=file_id,
                              volume_id=volume_id, local_issue_ids=[link[0] for link in links],
                              operation='associate' if job.source == job.target else 'move',
                              maintenance_review=f'/maintenance#archive-maintenance')
                if maintenance_job:
                    result.update(target=active_target, import_target=plan['target'],
                                  maintenance_job_id=maintenance_job, conversion_status='completed')
                return result
            finally:
                executor.close()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, OrganizationError):
        result['error'] = 'verification_unavailable'
        return result

"""Serial completed-artifact intake through the existing organizer.

No release acquisition, provider calls, library helper mutations or cleanup.
The OS gate prevents live claim theft; durable artifact/job correlations survive
process exit. Incomplete organization work is never replaced by a new plan.
"""

import json
import os
import time
from hashlib import sha256
from typing import Callable

from backend.base.acquisition_intake import (AcquisitionKind,
                                             IntakeErrorCode as E,
                                             IntakeFailure)
from backend.base.import_candidate import DiscoveryScope
from backend.base.organization_job import JobState, OrganizationError
from backend.base.organization_plan import PlanningPolicy, PlanStatus
from backend.features.local_artifact_planning import preview_local_artifacts
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.acquisition_paths import (contained,
                                                       map_download_path,
                                                       observe_artifacts)
from backend.implementations.acquisition_preparation import prepare_artifacts
from backend.implementations.organization_filesystem import execution_gate
from backend.implementations.organization_plan import preview_plan
from backend.internals.acquisition_intakes import IntakeStore
from backend.internals.organization_jobs import canonical, now

STABILITY_SECONDS = 10
RETRY_SECONDS = 30


class IntakeCoordinator:
    def __init__(self, database: str, *, clock: Callable[[], float] = time.time,
                 checkpoint: Callable[[str, str], None] = lambda stage, identifier: None):
        self.store = IntakeStore(database)
        self.clock = clock
        self.checkpoint = checkpoint

    def close(self) -> None:
        self.store.close()

    def _artifact_state(self, artifact: str, state: str, error: E | None = None) -> None:
        self.store.db.execute('UPDATE acquisition_artifacts SET state=?,error=?,updated_at=? WHERE id=?',
                              (state, error.value if error else None, now(), artifact))
        if state == 'organized':
            self.store.db.execute('''UPDATE acquisition_artifacts SET final_file_id=(
                SELECT f.id FROM active_files f JOIN organization_jobs j ON j.id=organization_job_id
                WHERE f.filepath=json_extract(j.intent,'$.target')) WHERE id=?''', (artifact,))

    def _reconcile(self, identifier: str, executor: OrganizationExecutor) -> bool:
        """Recover even a crash between job creation and intake link receipt."""
        linked = False
        for artifact in self.store.artifacts(identifier):
            job_id = artifact['organization_job_id']
            if not job_id:
                jobs = self.store.db.execute('SELECT id FROM organization_jobs WHERE batch_id=?',
                                             ('intake-artifact:' + artifact['id'],)).fetchall()
                if len(jobs) > 1:
                    raise IntakeFailure(E.RECOVERY)
                if jobs:
                    job_id = jobs[0][0]
                    self.store.db.execute('UPDATE acquisition_artifacts SET organization_job_id=? WHERE id=?',
                                          (job_id, artifact['id']))
            if not job_id:
                continue
            linked = True
            job = executor.store.get(job_id)
            if job.state == JobState.COMPLETED:
                self._artifact_state(artifact['id'], 'organized')
            else:
                # A pending job may have been created before a crash, but this
                # worker does not infer permission to resume arbitrary effects.
                self._artifact_state(artifact['id'], 'review', E.RECOVERY)
        if linked:
            for artifact in self.store.artifacts(identifier):
                if not artifact['organization_job_id'] and artifact['state'] not in ('review', 'blocked', 'prepared'):
                    # Interrupted batches are inspectable rather than silently
                    # abandoned or re-enumerated after some sources moved.
                    self._artifact_state(artifact['id'], 'review', E.RECOVERY)
        return linked

    def _finish(self, identifier: str) -> None:
        artifacts = self.store.artifacts(identifier)
        if not artifacts:
            return
        states = {a['state'] for a in artifacts if a['state'] != 'prepared'}
        state = ('completed' if states <= {'organized', 'no_changes'} else
                 'partial' if states & {'organized', 'no_changes'} else
                 'review' if states <= {'review', 'blocked'} else 'observing')
        self.store.state(identifier, state)
        self._quality_outcome(identifier)

    def _quality_outcome(self, identifier):
        if not self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_provenance'").fetchone():
            return
        row = self.store.get(identifier)
        if row['state'] not in ('completed', 'partial', 'review', 'blocked'):
            return
        complete = row['state'] == 'completed'
        self.store.db.execute('''UPDATE acquisition_provenance SET state=?,error=?,updated_at=?
            WHERE client_kind=? AND client_job=? AND state NOT IN ('imported','rejected')''',
            ('imported' if complete else 'failed', None if complete else 'intake_review_required',
             self.clock(), row['kind'], row['download_id']))

    def process(self, identifier: str, *, defer_apply: bool = False) -> dict:
        with execution_gate(self.store.path + '.intake'):
            row = self.store.get(identifier)
            if row['state'] in ('completed', 'review', 'partial', 'blocked'):
                return self.store.preview(identifier)
            if row['next_observation'] > self.clock():
                return self.store.preview(identifier)
            try:
                self._process(identifier, defer_apply=defer_apply)
            except IntakeFailure as error:
                waiting = error.code in (E.PATH_UNAVAILABLE, E.UNSTABLE)
                attempts = min(row['retry_count'] + 1, 8)
                self.store.db.execute('UPDATE acquisition_intakes SET retry_count=? WHERE id=?', (attempts, identifier))
                self.store.state(identifier, 'waiting' if waiting else 'review', error.code,
                                 self.clock() + min(900, RETRY_SECONDS * 2 ** (attempts - 1)) if waiting else 0)
            except OrganizationError:
                self.store.state(identifier, 'review', E.ORGANIZATION)
            except PermissionError:
                self.store.state(identifier, 'review', E.PERMISSION)
            except OSError:
                self.store.state(identifier, 'waiting', E.PATH_UNAVAILABLE, self.clock() + RETRY_SECONDS)
            self._quality_outcome(identifier)
            return self.store.preview(identifier)

    def _process(self, identifier: str, *, defer_apply: bool = False) -> None:
        row = self.store.get(identifier)
        completion = self.store.completion(identifier)
        roots = tuple(r[0] for r in self.store.db.execute('SELECT folder FROM root_folders ORDER BY id'))
        if row['local_root'] and self.store.artifacts(identifier):
            # Journal acknowledgement must not depend on a subsequently removed
            # downloader mapping or an already-moved source path.
            recovery = OrganizationExecutor(self.store.path, (row['local_root'], *roots))
            try:
                if self._reconcile(identifier, recovery):
                    self._finish(identifier)
                    return
            finally:
                recovery.close()
        paths = completion.reported_paths
        if not paths:
            raise IntakeFailure(E.UNSUPPORTED_ARTIFACT)
        if completion.kind in (AcquisitionKind.SABNZBD, AcquisitionKind.NZBGET, AcquisitionKind.QBITTORRENT):
            mappings = self.store.mappings()
            if len(mappings) > 100:
                raise IntakeFailure(E.CONFIGURATION)
            mapped = [map_download_path(p, completion.client_id or '', completion.client_instance or '', mappings)
                      for p in paths]
            if len({m[1] for m in mapped}) != 1:
                raise IntakeFailure(E.PATH_MAPPING)
            paths, root = tuple(m[0] for m in mapped), mapped[0][1]
            fingerprint = sha256(canonical([m[2] for m in mapped]).encode()).hexdigest()
        else:
            root = row['local_root']
            if not root:
                raise IntakeFailure(E.CONFIGURATION)
            paths = tuple(contained(p, root) for p in paths)
            fingerprint = sha256(canonical((root, paths)).encode()).hexdigest()
        if row['mapping_fingerprint'] and (
                row['mapping_fingerprint'] != fingerprint or row['mapped_paths'] != canonical(paths)):
            raise IntakeFailure(E.PATH_MAPPING)
        self.store.db.execute('''UPDATE acquisition_intakes SET local_root=?,mapped_paths=?,mapping_fingerprint=?
            WHERE id=?''', (root, canonical(paths), fingerprint, identifier))
        executor = OrganizationExecutor(self.store.path, (root, *roots))
        try:
            # Inspect journal before touching potentially already-moved paths.
            if self._reconcile(identifier, executor):
                self._finish(identifier)
                return
            prepared_paths = tuple(json.loads(row['prepared_paths']))
            working_root = root
            if prepared_paths and completion.kind == AcquisitionKind.QBITTORRENT:
                from backend.features.torrent_staging import staging_root
                working_root = staging_root(self.store, completion.volume_id)
            observations = observe_artifacts(prepared_paths or paths, working_root, admit_payloads=not prepared_paths)
            old = {a['path']: a for a in self.store.artifacts(identifier) if a['state'] != 'prepared'}
            if set(old) - {a.path for a in observations}:
                raise IntakeFailure(E.PATH_UNAVAILABLE)
            stamp = self.clock()
            stable = True
            with self.store.transaction():
                for observation in observations:
                    prior = old.get(observation.path)
                    identity = sha256(canonical((identifier, observation.path)).encode()).hexdigest()
                    observation_stamp = canonical(observation.stamp)
                    if prior is None:
                        self.store.db.execute('''INSERT INTO acquisition_artifacts
                            (id,intake_id,path,stamp,stable_since,updated_at) VALUES(?,?,?,?,?,?)''',
                            (identity, identifier, observation.path, observation_stamp, stamp, now()))
                        stable = False
                    elif prior['stamp'] != observation_stamp:
                        self.store.db.execute('''UPDATE acquisition_artifacts SET stamp=?,stable_since=?,
                            state='observing',summary='{}',candidate_id=NULL,updated_at=? WHERE id=?''',
                            (observation_stamp, stamp, now(), identity))
                        stable = False
                    elif stamp - prior['stable_since'] < STABILITY_SECONDS:
                        stable = False
            if not stable:
                self.store.state(identifier, 'observing', E.UNSTABLE, stamp + STABILITY_SECONDS)
                return
            if not prepared_paths:
                payload_paths = tuple(a.path for a in observations)
                if completion.kind == AcquisitionKind.QBITTORRENT:
                    from backend.features.torrent_staging import (
                        stage_payloads, staging_root)
                    payload_paths = stage_payloads(self.store, executor, identifier, payload_paths, completion.volume_id)
                    working_root = staging_root(self.store, completion.volume_id)
                prepared_paths = prepare_artifacts(payload_paths, working_root,
                                                  json.loads(row['preparation']), identifier)
                self.store.db.execute('UPDATE acquisition_intakes SET prepared_paths=? WHERE id=?',
                                      (canonical(prepared_paths), identifier))
                if prepared_paths != tuple(a.path for a in observations):
                    with self.store.transaction():
                        for artifact in self.store.artifacts(identifier):
                            if artifact['path'] not in prepared_paths:
                                self._artifact_state(artifact['id'], 'prepared')
                    self.store.state(identifier, 'observing')
                    return
            policy = PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt',
                                    rename=bool(row['rename']))
            batch = preview_local_artifacts(self.store.db, tuple(a.path for a in observations),
                DiscoveryScope(identifier, working_root), policy,
                volume_id=completion.volume_id, issue_ids=completion.issue_ids)
            provenance = None
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_provenance'").fetchone():
                provenance = self.store.db.execute('''SELECT * FROM acquisition_provenance
                    WHERE client_kind=? AND client_job=? ORDER BY created_at DESC LIMIT 1''',
                    (completion.kind.value, completion.download_id)).fetchone()
            upgrading = provenance is not None and provenance['reason'] == 'upgrade'
            artifacts = {a['path']: a for a in self.store.artifacts(identifier)}
            # Persist previews for the complete batch before any mutation.
            with self.store.transaction():
                for plan in batch.plans:
                    artifact = artifacts[plan.source_path]
                    self.store.db.execute('''UPDATE acquisition_artifacts SET candidate_id=?,summary=?,
                        state=?,updated_at=? WHERE id=?''', (plan.identification.candidate.candidate_id,
                        canonical(compact_plan(plan)), 'ready' if upgrading or plan.status in (PlanStatus.READY, PlanStatus.NO_CHANGES)
                        else 'review', now(), artifact['id']))
            if not row['auto_apply']:
                self.store.state(identifier, 'review')
                return
            if defer_apply:
                self.store.state(identifier, 'ready')
                return
            for plan in batch.plans:
                artifact = artifacts[plan.source_path]
                if upgrading:
                    from backend.base.quality import QualityError
                    from backend.features.organization_upgrade import \
                        register_upgrade
                    try:
                        if len(batch.plans) != 1:
                            raise QualityError('identity_failed')
                        job_id = register_upgrade(executor, plan, provenance['id'], 'intake-artifact:'+artifact['id'])
                        self.store.db.execute('UPDATE acquisition_artifacts SET organization_job_id=? WHERE id=?', (job_id,artifact['id']))
                        job = executor.apply_job(job_id)
                        self._artifact_state(artifact['id'], 'organized' if job.state == JobState.COMPLETED else 'review',
                            None if job.state == JobState.COMPLETED else E.RECOVERY)
                    except QualityError as error:
                        reason = str(error)
                        self.store.db.execute("UPDATE acquisition_provenance SET state='rejected',error=?,updated_at=? WHERE id=?",
                            (reason,self.clock(),provenance['id']))
                        suppression = reason if reason in ('dimension_floor_failed','verification_unavailable','equal','downgrade','identity_failed') else 'integrity_failed'
                        with self.store.transaction():
                            self.store.db.execute('''INSERT OR REPLACE INTO quality_rejections VALUES(?,?,?,?,?,?)''',
                                (provenance['issue_id'],provenance['candidate_key'],provenance['profile_id'],provenance['profile_revision'],suppression,self.clock()))
                            self.store.db.execute('''DELETE FROM quality_rejections WHERE rowid IN
                                (SELECT rowid FROM quality_rejections ORDER BY created_at DESC LIMIT -1 OFFSET 10000)''')
                        self._artifact_state(artifact['id'], 'blocked', E.ORGANIZATION)
                    continue
                if plan.status not in (PlanStatus.READY, PlanStatus.NO_CHANGES):
                    continue
                if provenance is not None:
                    from backend.base.quality import (ClaimedQuality,
                                                      QualityError, compare)
                    from backend.implementations.file_quality import analyze
                    try:
                        policy_snapshot = json.loads(provenance['profile_snapshot'])
                        # Explicit verification requirements fail closed before
                        # ordinary import too. The legacy default admits formats
                        # whose raster metrics are not available.
                        if policy_snapshot.get('minimum_p10'):
                            facts = analyze(plan.source_path, verify_pixels=True)
                            outcome = compare(policy_snapshot, ClaimedQuality(**json.loads(provenance['claims'])),
                                              verified=facts, post_import=True)
                            if outcome['result'] != 'accepted':
                                raise QualityError(outcome['reason'])
                    except QualityError as error:
                        self.store.db.execute("UPDATE acquisition_provenance SET state='rejected',error=?,updated_at=? WHERE id=?",
                                              (str(error), self.clock(), provenance['id']))
                        self._artifact_state(artifact['id'], 'blocked', E.ORGANIZATION)
                        continue
                self.checkpoint('before_job', artifact['id'])
                self._artifact_state(artifact['id'], 'organizing')
                try:
                    job_id = executor.create_job(plan, batch_id='intake-artifact:' + artifact['id'])
                    self.checkpoint('after_job', artifact['id'])
                    self.store.db.execute('UPDATE acquisition_artifacts SET organization_job_id=? WHERE id=?',
                                          (job_id, artifact['id']))
                    self.checkpoint('before_apply', artifact['id'])
                    job = executor.apply_job(job_id)
                    self.checkpoint('after_apply', artifact['id'])
                    self._artifact_state(artifact['id'], 'organized' if job.state == JobState.COMPLETED else 'review',
                                         None if job.state == JobState.COMPLETED else E.RECOVERY)
                except OrganizationError:
                    # Independent ready siblings may continue; failed/partial
                    # work keeps its exact journal and is never replanned here.
                    self._artifact_state(artifact['id'], 'review', E.ORGANIZATION)
            self._finish(identifier)
        finally:
            executor.close()


def compact_plan(plan) -> dict:
    preview = preview_plan(plan)
    return {key: preview[key] for key in ('status', 'source', 'target', 'volume_id', 'issue_ids',
                                        'effects', 'diagnostics', 'identification_reasons')}


def act_on_linked_job(database: str, intake_id: str, artifact_id: str, action: str) -> dict:
    """Explicit operator recovery of the existing journal, never a replacement."""
    if action not in ('inspect', 'reconcile', 'resume'):
        raise IntakeFailure(E.CONFIGURATION)
    coordinator = IntakeCoordinator(database)
    try:
        with execution_gate(coordinator.store.path + '.intake'):
            row = coordinator.store.get(intake_id)
            artifact = next((a for a in coordinator.store.artifacts(intake_id) if a['id'] == artifact_id), None)
            if artifact is None or not artifact['organization_job_id'] or not row['local_root']:
                raise IntakeFailure(E.CONFIGURATION)
            roots = tuple(r[0] for r in coordinator.store.db.execute('SELECT folder FROM root_folders'))
            executor = OrganizationExecutor(database, (row['local_root'], *roots))
            try:
                job = artifact['organization_job_id']
                if action == 'reconcile':
                    executor.reconcile_job(job)
                elif action == 'resume':
                    executor.apply_job(job)
                result = executor.inspect_job(job)
                if action != 'inspect':
                    coordinator._reconcile(intake_id, executor)
                    coordinator._finish(intake_id)
                return result
            finally:
                executor.close()
    finally:
        coordinator.close()

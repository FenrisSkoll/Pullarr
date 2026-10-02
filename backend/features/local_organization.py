"""Explicit local preview/apply sessions reusing the Phase 4 domain pipeline.

Transient previews expire on restart. Once created, OrganizationJob journals,
not this session, own mutations and recovery. Registration is a separate,
explicit metadata operation and is not rolled back by later file review.
"""

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from uuid import uuid4

from backend.base.acquisition_intake import IntakeErrorCode as E, IntakeFailure
from backend.base.import_candidate import DiscoveryKind, DiscoveryScope
from backend.base.organization_plan import (OrganizationBatch,
                                            PlanningPolicy, PlanStatus)
from backend.features.acquisition_intake import compact_plan
from backend.features.local_artifact_planning import preview_local_artifacts
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.acquisition_paths import (contained,
                                                       observe_artifacts)


@dataclass
class LocalSession:
    database: str
    roots: tuple[str, ...]
    batch: OrganizationBatch
    expires: float
    jobs: dict[int, str] = field(default_factory=dict)
    claimed: bool = False


_sessions: dict[str, LocalSession] = {}
_lock = RLock()


def retain_preview(database: str, roots: tuple[str, ...], batch: OrganizationBatch) -> dict:
    with _lock:
        for key in tuple(_sessions):
            if _sessions[key].expires < time.monotonic():
                del _sessions[key]
        if len(_sessions) >= 16 or sum(len(s.batch.plans) for s in _sessions.values()) + len(batch.plans) > 4000:
            raise IntakeFailure(E.CONFIGURATION)
        key = uuid4().hex
        _sessions[key] = LocalSession(str(Path(database).absolute()), roots, batch, time.monotonic() + 600)
        return dict(id=key, expires_in=600, plans=[compact_plan(p) for p in batch.plans],
                    enumeration_complete=True)


def apply_preview(database: str, identifier: str) -> dict:
    with _lock:
        session = _sessions.get(identifier)
        if (session is None or session.database != str(Path(database).absolute())
                or session.expires < time.monotonic()):
            raise IntakeFailure(E.CONFIGURATION)
        if session.claimed:
            return dict(id=identifier, jobs=list(session.jobs.values()), replay=True)
        # A failed or interrupted apply is never blindly replayed.
        session.claimed = True
    executor = OrganizationExecutor(database, session.roots)
    try:
        for index, plan in enumerate(session.batch.plans):
            if plan.status in (PlanStatus.READY, PlanStatus.NO_CHANGES):
                session.jobs[index] = executor.create_job(plan, batch_id='local-preview:' + identifier)
        outcomes = []
        for index, job in session.jobs.items():
            result = executor.apply_job(job)
            outcomes.append(dict(source=session.batch.plans[index].source_path,
                                 job_id=job, state=result.state.value))
        return dict(id=identifier, jobs=outcomes,
                    review=[compact_plan(p) for i, p in enumerate(session.batch.plans) if i not in session.jobs])
    finally:
        executor.close()


def scan_preview(database: str, volume_id: int) -> dict:
    db = sqlite3.connect(database)
    try:
        row = db.execute('''SELECT v.folder,r.folder FROM volumes v
            JOIN root_folders r ON r.id=v.root_folder WHERE v.id=?''', (volume_id,)).fetchone()
        if row is None or not row[0]:
            raise IntakeFailure(E.CONFIGURATION)
        folder = contained(row[0], row[1])
        artifacts = observe_artifacts((folder,), row[1])
        scope = DiscoveryScope(uuid4().hex, folder, kind=DiscoveryKind.LIBRARY_SCAN)
        policy = PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt', move=False, rename=False)
        batch = preview_local_artifacts(db, tuple(a.path for a in artifacts), scope, policy, volume_id=volume_id)
        # No missing-path cleanup, even following a complete enumeration.
        return retain_preview(database, (row[1],), batch)
    finally:
        db.close()


def import_preview(database: str, matches: object, rename: bool) -> dict:
    from backend.features.library_import import normalize_import_matches
    from backend.implementations.volumes import Library
    from backend.internals.db import commit
    from backend.internals.provider_identity import ProviderIdentityDB

    normalized = normalize_import_matches(matches)
    if not 1 <= len(normalized) <= 1000 or len({p for _, p in normalized}) != len(normalized):
        raise IntakeFailure(E.CONFIGURATION)
    db = sqlite3.connect(database)
    try:
        roots = tuple(db.execute('SELECT id,folder FROM root_folders ORDER BY id'))
        authorized = []
        # Validate every requested scope before any provider registration.
        for identity, path in normalized:
            candidates = []
            for root_id, root in roots:
                if Path(root) in Path(path).parents:
                    candidates.append((root_id, root))
            if len(candidates) != 1:
                raise IntakeFailure(E.UNSAFE_PATH)
            root_id, root = candidates[0]
            safe = contained(path, root)
            observed = observe_artifacts((safe,), root)
            if len(observed) != 1 or observed[0].path != safe:
                raise IntakeFailure(E.UNSUPPORTED_ARTIFACT)
            authorized.append((identity, safe, root_id, root))
        volumes = {}
        contexts = {}
        operation = uuid4().hex
        for identity, path, root_id, root in authorized:
            if identity not in volumes:
                try:
                    existing = ProviderIdentityDB.find_selected_volume(identity.provider, identity.provider_id)
                except ValueError:
                    raise IntakeFailure(E.IDENTIFICATION) from None
                if existing is None:
                    existing = Library.add_metadata(identity, root_id, True, organizer_registration=True)
                    commit()
                volumes[identity] = existing
            contexts[path] = (DiscoveryScope(operation, root), volumes[identity])
        first_scope = next(iter(contexts.values()))[0]
        batch = preview_local_artifacts(db, tuple(contexts), first_scope,
            PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt', rename=rename),
            path_contexts=contexts)
        return retain_preview(database, tuple(r[1] for r in roots), batch)
    finally:
        db.close()

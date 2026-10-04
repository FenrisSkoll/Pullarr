"""Explicit local preview/apply sessions reusing the Phase 4 domain pipeline.

Transient previews expire on restart. Import file effects belong to durable
OrganizationJobs; managed associations use a scoped database transaction.
Registration establishes a usable folder and survives later issue-level review.
"""

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from uuid import uuid4

from backend.base.acquisition_intake import IntakeErrorCode as E, IntakeFailure
from backend.base.identification import PublicationAuthority
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
    reviews: list[dict] = field(default_factory=list)
    presentation: list[dict] = field(default_factory=list)
    volume_id: int | None = None
    stamps: dict[int, tuple] = field(default_factory=dict)
    reviewed: dict[int, tuple[int, ...]] = field(default_factory=dict)
    association_forced: dict[int, bool] = field(default_factory=dict)
    source_directories: dict[str, tuple[int, int]] = field(default_factory=dict)
    issue_catalog: tuple = ()


_sessions: dict[str, LocalSession] = {}
_lock = RLock()


def retain_preview(database: str, roots: tuple[str, ...], batch: OrganizationBatch, reviews=None, *, volume_id=None) -> dict:
    plans = [compact_plan(p) for p in batch.plans]
    with closing(sqlite3.connect(database)) as db:
        ids = sorted({i for p in plans for i in p['issue_ids']})
        labels = {r[0]: '#'+r[1]+(' '+r[2] if r[2] else '') for r in db.execute(
            'SELECT id,issue_number,title FROM issues WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(ids),))}
        catalog = tuple(db.execute('SELECT id,issue_number,title FROM issues WHERE volume_id=? ORDER BY id', (volume_id,))) if volume_id is not None else ()
    for index, (value, plan) in enumerate(zip(plans, batch.plans)):
        value['row_id'] = str(index)
        value['review_available'] = volume_id is not None
        selected = plan.identification.selected
        value['issue_labels'] = [labels[i] for i in value['issue_ids'] if i in labels]
        if selected:
            value['publication'] = selected.title
            value['authority'] = dict(provider=selected.provider_identity.provider, id=selected.provider_identity.provider_id)
    with _lock:
        for key in tuple(_sessions):
            if _sessions[key].expires < time.monotonic():
                del _sessions[key]
        # Eviction makes only the oldest receipt stale, never all volumes.
        while len(_sessions) >= 16 or (sum(len(s.batch.plans) for s in _sessions.values()) + len(batch.plans) > 4000 and _sessions):
            del _sessions[next(iter(_sessions))]
        if len(_sessions) >= 16 or sum(len(s.batch.plans) for s in _sessions.values()) + len(batch.plans) > 4000:
            raise IntakeFailure(E.CONFIGURATION)
        key = uuid4().hex
        stamps = {}
        if volume_id is not None:
            for index, plan in enumerate(batch.plans):
                try:
                    stat = os.stat(plan.source_path, follow_symlinks=False)
                except OSError:
                    plans[index].update(status='review_required', review_available=False,
                        identification_reasons=['local_file_unavailable'])
                    continue
                observed = plan.identification.candidate.file
                if (stat.st_size, stat.st_mtime_ns) != (observed.size, observed.mtime_ns):
                    plans[index].update(status='review_required', review_available=False,
                        identification_reasons=['local_file_changed'])
                    continue
                stamps[index] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        _sessions[key] = LocalSession(str(Path(database).absolute()), roots, batch, time.monotonic() + 600,
                                      reviews=reviews or [], presentation=plans, volume_id=volume_id, stamps=stamps, issue_catalog=catalog)
        return dict(id=key, expires_in=600, plans=plans + (reviews or []),
                    enumeration_complete=True)


def apply_preview(database: str, identifier: str) -> dict:
    with _lock:
        session = _sessions.get(identifier)
        if (session is None or session.database != str(Path(database).absolute())
                or session.expires < time.monotonic()):
            raise IntakeFailure(E.CONFIGURATION)
        replay = session.claimed
        # A failed or interrupted apply is never blindly replayed.
        session.claimed = True
    if session.volume_id is not None:
        from backend.features.local_issue_review import apply_associations
        return apply_associations(database, identifier)
    executor = OrganizationExecutor(database, session.roots)
    try:
        for index, plan in enumerate(session.batch.plans if not replay else ()):
            if (plan.status in (PlanStatus.READY, PlanStatus.NO_CHANGES)
                    and index not in session.jobs and index not in session.reviewed):
                session.jobs[index] = executor.create_job(plan, batch_id='local-preview:' + identifier)
        outcomes = []
        for index, job in tuple(session.jobs.items()):
            result = executor.store.get(job) if replay else executor.apply_job(job)
            plan = session.batch.plans[index]
            outcomes.append(dict(source=plan.source_path, target=plan.target_path,
                                 publication=plan.identification.selected.title,
                                 volume_id=plan.identification.selected.local_volume_id,
                                 job_id=job, state=result.state.value))
        from backend.features.import_folder_cleanup import cleanup_sources
        cleanup_sources(executor, session, outcomes)
        return dict(id=identifier, jobs=outcomes, replay=replay,
                    review=[p for i, p in enumerate(session.presentation) if i not in session.jobs and i not in session.reviewed]
                    + session.reviews)
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
        try:
            artifacts = observe_artifacts((folder,), row[1])
        except IntakeFailure as error:
            if error.code != E.UNSUPPORTED_ARTIFACT:
                raise
            artifacts = ()
        scope = DiscoveryScope(uuid4().hex, folder, kind=DiscoveryKind.LIBRARY_SCAN)
        policy = PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt', move=False, rename=False)
        batch = preview_local_artifacts(db, tuple(a.path for a in artifacts), scope, policy, volume_id=volume_id,
                                       authority=PublicationAuthority.MANAGED_VOLUME) if artifacts else OrganizationBatch(())
        # No missing-path cleanup, even following a complete enumeration.
        return retain_preview(database, (row[1],), batch, volume_id=volume_id)
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
        contexts = {}
        operation = uuid4().hex
        reviews = []
        for identity in dict.fromkeys(a[0] for a in authorized):
            group = [a for a in authorized if a[0] == identity]
            reason = None
            existing = None
            if len({a[2] for a in group}) != 1:
                reason = 'folder_assignment_required'
            else:
                _, path, root_id, root = group[0]
                try:
                    existing = ProviderIdentityDB.find_selected_volume(identity.provider, identity.provider_id)
                    if existing is not None:
                        row = db.execute('SELECT folder FROM volumes WHERE id=?', (existing,)).fetchone()
                        if row is None or not row[0] or not Path(row[0]).is_dir():
                            reason = 'existing_volume_folder_conflict'
                    else:
                        existing = Library.add_metadata(identity, root_id, True,
                            organizer_registration=True, import_destination=True)
                        commit()
                except ValueError:
                    reason = 'publication_registration_conflict'
                except Exception:
                    reason = 'publication_registration_unavailable'
            for _, path, root_id, root in group:
                if reason:
                    reviews.append(dict(status='review_required', source=path, target=None,
                        volume_id=existing, issue_ids=[], effects=[], diagnostics=[],
                        identification_reasons=[reason], authority=dict(provider=identity.provider, id=identity.provider_id)))
                else:
                    assert existing is not None
                    contexts[path] = (DiscoveryScope(operation, root), existing)
        batch = OrganizationBatch(())
        if contexts:
            first_scope = next(iter(contexts.values()))[0]
            batch = preview_local_artifacts(db, tuple(contexts), first_scope,
                PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt', rename=rename, move=True),
                path_contexts=contexts, authority=PublicationAuthority.IMPORT_SELECTION)
        value = retain_preview(database, tuple(r[1] for r in roots), batch, reviews)
        with _lock:
            session = _sessions[value['id']]
            for _, source, _, _ in authorized:
                parent = str(Path(source).parent)
                stat = os.stat(parent, follow_symlinks=False)
                session.source_directories[parent] = (stat.st_dev, stat.st_ino)
        return value
    finally:
        db.close()

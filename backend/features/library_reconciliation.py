"""Explicit local DB-only admission through the existing organizer pipeline.

Monitoring supplies observations, not bibliographic identity or mutation orders.
No legacy scanner, provider acquisition, missing-link removal or path guessing.
"""

import os
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

from backend.base.definitions import FileConstants
from backend.base.folder_monitor import PathStamp, ReevaluationResult
from backend.base.identification import MatchState
from backend.base.import_candidate import (DiscoveryKind, DiscoveryScope,
                                           EvidenceSource, Provenance)
from backend.base.organization_job import JobState
from backend.base.organization_plan import (EffectKind, PlanningPolicy,
                                            PlanStatus)
from backend.features.folder_monitor import stamp
from backend.features.organization_execution import OrganizationExecutor
from backend.features.organization_plan import observe_plan_paths
from backend.implementations.comicinfo_candidate import enrich_comicinfo
from backend.implementations.identification import MatchingSnapshot, identify
from backend.implementations.import_candidates import observe_import_candidate
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.organization_plan import (PlanningContext,
                                                       path_key, plan_one)
from backend.internals.import_identity import load_existing_import_identities
from backend.internals.organization_plan import load_planning_records
from backend.internals.organization_reservations import load_reservations


class LibraryReconciler:
    """A scoped context per ready-work batch; the executor revalidates on apply."""

    def __init__(self, database: str, db: sqlite3.Connection):
        self.database, self.db = database, db
        self.records = None
        self.matching = None
        self.context = None
        self.policy = PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt', move=False, rename=False)

    def __call__(self, root_id: int, observed: PathStamp) -> ReevaluationResult:
        def review(reason: str) -> ReevaluationResult:
            return ReevaluationResult('review', reason)

        root = self.db.execute('SELECT folder FROM root_folders WHERE id=?', (root_id,)).fetchone()
        if root is None:
            return review('root_no_longer_configured')
        root_path, path = Path(root[0]), Path(observed.path)
        root_observation = stamp(str(root_path))
        if not root_observation.directory or root_path not in path.parents or stamp(observed.path) != observed:
            return ReevaluationResult('pending', 'filesystem_changed')
        if observed.directory or path.name.lower() in FileConstants.METADATA_FILES:
            return review('general_metadata_requires_explicit_review')
        if load_reservations(self.db.cursor()).conflicts(observed.path):
            return ReevaluationResult('pending', 'organization_job_owns_path')
        if self.records is None:
            self.records = load_planning_records(tuple(PROVIDERS), self.db.cursor())
            vs, ins, rs, fs, ns = self.records
            self.matching = MatchingSnapshot.build((v.identity for v in vs), (i.identity for i in ins))
            self.context = PlanningContext.build(vs, ins, rs, fs, ns, self.policy)
        volumes, issues, roots, files, naming = self.records
        owners = [v for v in volumes if v.root_id == root_id and v.folder and Path(v.folder) in path.parents]
        if len(owners) != 1:
            return review('unique_existing_managed_volume_required')
        owner = owners[0]
        known = load_existing_import_identities([observed.path], tuple(PROVIDERS), self.db.cursor()).get(observed.path)
        candidate = observe_import_candidate(observed.path,
            DiscoveryScope('folder-monitor', str(root_path), False, DiscoveryKind.LIBRARY_SCAN), existing=known)
        candidate = replace(candidate, folder=replace(candidate.folder,
            local_volume_ids=(owner.identity.id,), provenance=Provenance(EvidenceSource.DATABASE, 'volumes.folder')))
        candidate = enrich_comicinfo(candidate)
        assert self.matching is not None and self.context is not None
        result = identify(candidate, self.matching)
        if result.state != MatchState.AUTOMATIC or not result.selected or result.selected.local_volume_id != owner.identity.id:
            return review('identification_' + result.state.value)
        # A delete/create pair cannot mint a duplicate or transfer old identity.
        # Existing coverage, including missing paths, requires explicit review.
        wanted = set(result.selected.local_issue_ids)
        if any(f.path != observed.path and any(l.issue_id in wanted for l in f.links) for f in files):
            return review('existing_issue_file_requires_review')
        policy = self.policy
        context = self.context
        provisional = plan_one(result, context)
        paths = tuple(p for p in (observed.path, provisional.target_path, provisional.target_folder, provisional.target_root) if p)
        observations = observe_plan_paths(paths, policy)
        context = replace(context,
            observations=MappingProxyType({path_key(o.path, policy): o for o in observations}),
            context_id=sha256(repr((context.context_id, observations)).encode()).hexdigest())
        plan = plan_one(result, context)
        if plan.status == PlanStatus.NO_CHANGES:
            stored = self.db.execute('SELECT size FROM active_files WHERE filepath=?', (observed.path,)).fetchone()
            if stored is not None and stored[0] != observed.size:
                return review('registered_file_size_changed')
            return ReevaluationResult('reconciled', 'canonical_associations_unchanged')
        if (plan.status != PlanStatus.READY or plan.target_path != observed.path or not plan.associations
                or plan.associations.removed or not plan.effects
                or any(e.kind not in (EffectKind.FILE_RECORD, EffectKind.ASSOCIATIONS) for e in plan.effects)):
            return review('organization_preview_requires_approval')
        # The executor owns mutations, receipts, concurrency and revalidation.
        # An event cannot authorize a filesystem effect, even if planner defaults
        # change in a later release.
        executor = OrganizationExecutor(self.database, (str(root_path),))
        try:
            job = executor.create_job(plan)
            outcome = executor.apply_job(job)
            self.records = None  # This batch's canonical file/link snapshot changed.
            return ReevaluationResult('reconciled' if outcome.state == JobState.COMPLETED else 'review',
                                      'db_only_job_' + outcome.state.value, job)
        finally:
            executor.close()

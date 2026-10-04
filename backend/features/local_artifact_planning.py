"""Shared read-only local-artifact preview; no source-specific matching rules.

Acquisition callers must establish stability before invoking this boundary.
Manual/scan callers supply their own authorization policy. Neither target hints
nor previewing a READY plan authorize execution here.
"""

import sqlite3
from dataclasses import replace
from types import MappingProxyType
from typing import Optional, Tuple

from backend.base.acquisition_intake import IntakeErrorCode as E, IntakeFailure
from backend.base.identification import (MatchReason, MatchState,
                                         PublicationAuthority)
from backend.base.import_candidate import (CoverageHypothesis, CoverageKind,
                                           DiscoveryScope, EvidenceSource,
                                           MatchHypothesis, Provenance)
from backend.base.organization_fingerprint import database_fingerprint
from backend.base.organization_plan import OrganizationBatch, PlanningPolicy
from backend.features.organization_plan import observe_plan_paths
from backend.implementations.acquisition_paths import MAX_ARTIFACTS, contained
from backend.implementations.comicinfo_candidate import enrich_comicinfo
from backend.implementations.identification import (MatchingSnapshot, identify,
                                                    identify_authorized)
from backend.implementations.import_candidates import observe_import_candidate
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.organization_plan import (PlanningContext,
                                                       path_key, plan_many)
from backend.internals.import_identity import load_existing_import_identities
from backend.internals.organization_plan import load_planning_records


def preview_local_artifacts(
    db: sqlite3.Connection, paths: Tuple[str, ...], scope: DiscoveryScope,
    policy: PlanningPolicy, *, volume_id: Optional[int] = None,
    issue_ids: Tuple[int, ...] = (),
    path_contexts: Optional[dict[str, tuple[DiscoveryScope, int]]] = None,
    authority: PublicationAuthority = PublicationAuthority.HYPOTHESIS,
) -> OrganizationBatch:
    """One shared snapshot and batch collision pass for the declared paths.

    Ordinary target hints require independent AUTOMATIC identification.
    Explicit import selections and managed-volume scans instead establish
    publication authority; conflicts and issue coverage still require evidence.
    Provider-qualified conflicting evidence is retained by Phase 4D. New files
    are never given fabricated DB identity or folder ownership.
    """
    if not scope.root or not 1 <= len(paths) <= MAX_ARTIFACTS or len(set(paths)) != len(paths):
        raise IntakeFailure(E.CONFIGURATION)
    contexts = path_contexts or {}
    if set(contexts) - set(paths):
        raise IntakeFailure(E.CONFIGURATION)
    ordered = tuple(sorted(contained(path, contexts[path][0].root or '' if path in contexts else scope.root)
                           for path in paths))
    selected_ids = ({volume_id} if volume_id is not None else {v for _, v in contexts.values()})
    records = load_planning_records(tuple(PROVIDERS), db.cursor(),
        volume_ids=selected_ids if authority != PublicationAuthority.HYPOTHESIS and selected_ids else None)
    volumes, issues, roots, files, naming = records
    matching = MatchingSnapshot.build((v.identity for v in volumes), (i.identity for i in issues))
    target = matching.volumes.get(volume_id) if volume_id is not None else None
    if ((volume_id is not None and target is None)
            or any(i not in matching.issues or matching.issues[i].volume_id != volume_id for i in issue_ids)):
        raise IntakeFailure(E.IDENTIFICATION)
    known = load_existing_import_identities(ordered, tuple(PROVIDERS), db.cursor())
    identified = []
    for path in ordered:
        local_scope, local_volume = contexts.get(path, (scope, volume_id))
        local_target = matching.volumes.get(local_volume) if local_volume is not None else target
        if local_volume is not None and local_target is None:
            raise IntakeFailure(E.IDENTIFICATION)
        # Scoped scans must retain filename evidence on subsequent scans too.
        # Generic observation may skip parsing an already-associated file, which
        # would otherwise make a proven split title disappear after first Apply.
        candidate = observe_import_candidate(path, local_scope,
            existing=known.get(path) if authority == PublicationAuthority.HYPOTHESIS else None)
        if authority != PublicationAuthority.HYPOTHESIS:
            candidate = replace(candidate, existing=known.get(path))
        if local_target is not None:
            provenance = Provenance(EvidenceSource.MANUAL, local_scope.run_id, authority.value)
            candidate = replace(candidate, alternatives=(MatchHypothesis(
                local_target.authority, provenance,
                CoverageHypothesis(CoverageKind.UNKNOWN, provenance), local_target.id),))
        candidate = enrich_comicinfo(candidate)
        result = (identify_authorized(candidate, matching, local_volume, authority)
                  if authority != PublicationAuthority.HYPOTHESIS and local_volume is not None
                  else identify(candidate, matching))
        if result.selected is not None and (
                (local_volume is not None and result.selected.local_volume_id != local_volume)
                or (issue_ids and not set(result.selected.local_issue_ids).issubset(issue_ids))):
            # An independently identified different publication is evidence for
            # review, not authorization to substitute the selected target.
            result = replace(result, state=MatchState.REVIEW, selected=None,
                             reasons=result.reasons + (MatchReason.EVIDENCE_CONFLICT,))
        identified.append(result)
    context = PlanningContext.build(volumes, issues, roots, files, naming, policy)
    owners = dict(context.folder_owners)
    owner_records = ()
    if authority != PublicationAuthority.HYPOTHESIS:
        owners = {}
        owner_records = tuple(db.execute("SELECT id,folder FROM volumes WHERE folder<>''"))
        for vid, folder in owner_records:
            owners.setdefault(path_key(folder,policy),[]).append(vid)
    context = replace(context,folder_owners=MappingProxyType({k:tuple(v) for k,v in owners.items()}))
    provisional = plan_many(identified, context)
    observed_paths = set(ordered)
    for plan in provisional.plans:
        observed_paths.update(p for p in (plan.target_path, plan.target_folder, plan.target_root) if p)
    observations = observe_plan_paths(observed_paths, policy)
    context = PlanningContext.build(volumes, issues, roots, files, naming, policy, observations)
    context = replace(context,folder_owners=MappingProxyType({k:tuple(v) for k,v in owners.items()}))
    batch = plan_many(identified, context)
    if authority != PublicationAuthority.HYPOTHESIS:
        # Executor fingerprints include all folder owners. Scope metadata loading
        # without dropping that independent ownership projection from the receipt.
        batch = replace(batch, plans=tuple(replace(plan, database_fingerprint=database_fingerprint(
            context.volumes[plan.identification.selected.local_volume_id],
            context.children[plan.identification.selected.local_volume_id],
            context.roots.items(), owner_records, context.naming))
            if plan.database_fingerprint else plan for plan in batch.plans))
    return batch

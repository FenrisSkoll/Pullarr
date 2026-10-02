"""Read-only maintenance preview adapter over the canonical organizer."""

import json
import os
import sqlite3
import stat
from collections import defaultdict
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from backend.base.import_candidate import DiscoveryKind, DiscoveryScope
from backend.base.library_health import canonical, fingerprint
from backend.base.maintenance_review import (Action, Capability,
                                             ReviewError, ReviewItem)
from backend.base.organization_plan import MetadataMode, PlanningPolicy
from backend.features.organization_plan import observe_plan_paths
from backend.implementations.comicinfo_candidate import enrich_comicinfo
from backend.implementations.identification import MatchingSnapshot, identify
from backend.implementations.import_candidates import observe_import_candidate
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.organization_filesystem import safe_path
from backend.implementations.organization_plan import (PlanningContext,
                                                       path_key, path_module,
                                                       plan_many, plan_one,
                                                       preview_plan)
from backend.internals.import_identity import load_existing_import_identities


def file_state(path):
    try:
        safe_path(path)
        value = os.lstat(path)
        return dict(state='file' if stat.S_ISREG(value.st_mode) else 'directory' if stat.S_ISDIR(value.st_mode) else 'unsupported',
                    stamp=[value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns])
    except FileNotFoundError:
        return dict(state='absent', stamp=None)
    except Exception:
        return dict(state='unavailable', stamp=None)


def batch_collisions(effects, windows=None):
    """One whole-selection index, including cross-action source dependencies.

    A dependency blocks even without a cycle. No winner/staging order is chosen.
    Diagnostics are group-based rather than a quadratic pair list.
    """
    if len(effects) > 250:
        raise ReviewError('preview_limit')
    windows = os.name == 'nt' if windows is None else windows
    policy = PlanningPolicy(windows=windows, case_sensitive=not windows)
    mod = path_module(policy)
    sources, targets = defaultdict(list), defaultdict(list)
    result = []
    for item, source, target in effects:
        sources[path_key(source, policy)].append(item)
        targets[path_key(target, policy)].append(item)
        if source != target and path_key(source, policy) == path_key(target, policy):
            result.append(dict(code='case_only_requires_staging', items=[item], path=target))
    for path, ids in sorted(sources.items()):
        if len(ids) > 1:
            result.append(dict(code='duplicate_source', items=sorted(ids), path=path))
    for path, ids in sorted(targets.items()):
        if len(ids) > 1:
            result.append(dict(code='shared_target', items=sorted(ids), path=path))
        involved = set(ids) | set(sources.get(path, ()))
        if sources.get(path) and len(involved) > 1:
            result.append(dict(code='source_target_dependency_or_cycle', items=sorted(involved), path=path))
        ancestor = mod.dirname(path)
        depth = 0
        while ancestor and ancestor != mod.dirname(ancestor):
            depth += 1
            if depth > 64:
                raise ReviewError('path_index_limit')
            parents = set(targets.get(ancestor, ())) | set(sources.get(ancestor, ()))
            if parents:
                result.append(dict(code='parent_child_collision', items=sorted(set(ids) | parents), path=path))
            ancestor = mod.dirname(ancestor)
    if len(result) > 2000:
        raise ReviewError('collision_limit')
    return result


def build_previews(database, report, items, snapshot, *, detached_plans=None, individual_plans=None):
    """One snapshot/identity load per revision, not one load per finding."""
    files = {f['id']: f for f in snapshot['files']}
    volumes = {v['id']: v for v in snapshot['volumes']}
    issues = {i['id']: i for i in snapshot['issues']}
    refs = defaultdict(list)
    files_by_path = defaultdict(list)
    for row in snapshot['volume_refs']:
        refs[row['volume_id']].append(row)
    for row in files.values():
        files_by_path[row['filepath']].append(row['id'])
    direct, general, coverage = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in snapshot['direct']:
        direct[row['file_id']].append(row)
    for row in snapshot['general']:
        general[row['file_id']].append(row)
    for row in snapshot['coverage']:
        coverage[row['file_id']].append(row)
    old_stamps = {}
    for finding in report.findings:
        if finding.path:
            old_stamps[finding.path] = json.loads(finding.evidence_json).get('stat')
    states = {}
    def observe(path):
        if path not in states:
            states[path] = file_state(path)
        return states[path]

    current = snapshot['digest'] == report.state_digest
    updated = []
    planned = []
    for item in items:
        f = item.finding
        if not item.selected or item.excluded:
            # Excluding an item is not a fresh observation. Keep the stale latch
            # even if the changed settings/metadata later happen to return.
            updated.append(item if item.capability == Capability.STALE else
                           replace(item, capability=Capability.NOT_APPLICABLE, blockers=()))
            continue
        evidence = json.loads(f.evidence_json)['evidence']
        paths = ([f.path] if f.path else []) + list(evidence.get('paths', ()))
        paths += [files[i]['filepath'] for i in evidence.get('file_ids', ()) if i in files]
        if len(paths) > 2000:
            raise ReviewError('path_index_limit')
        fresh = current
        for path in set(paths):
            state = observe(path)
            if path not in old_stamps or state['state'] == 'unavailable' or state['stamp'] != old_stamps[path]:
                fresh = False
        previous = json.loads(item.freshness_json)
        if previous and any(observe(path) != expected for path, expected in previous.get('paths', {}).items()):
            fresh = False
        if item.capability == Capability.STALE or not fresh:
            updated.append(replace(item, capability=Capability.STALE, blockers=('evidence_changed_reinspect',)))
            continue
        freshness = dict(snapshot=snapshot['digest'], paths={p: observe(p) for p in sorted(set(paths))})
        volume = volumes.get(f.volume_id)
        if volume:
            freshness['authority'] = dict(volume_id=f.volume_id, provider=volume['metadata_provider'],
                generation=volume['authority_generation'], references=refs[f.volume_id])
        item = replace(item, freshness_json=canonical(freshness), blockers=())
        preview = dict(preserved=['local IDs', 'all files', 'monitoring', 'provider identities', 'C2 coverage', 'history'],
                       apply_available=False)
        cap = Capability.LATER
        blockers = ()
        recovery = 'Intent only; no mutation, receipt or snapshot undo.'
        if item.action in (Action.NONE, Action.ACKNOWLEDGE, Action.LATER):
            cap = Capability.NOT_APPLICABLE
        elif item.action in (Action.DUPLICATE, Action.KEEP):
            if f.category != 'duplicate':
                cap, blockers = Capability.NOT_APPLICABLE, ('not_duplicate_evidence',)
            else:
                ids = set(evidence.get('file_ids', ()))
                ids.update(i for path in paths for i in files_by_path[path])
                preview['members'] = [dict(file=files[i], direct=direct[i], general=general[i], coverage=coverage[i],
                    issues=[issues[r['issue_id']] for r in direct[i] if r['issue_id'] in issues]) for i in sorted(ids) if i in files]
                preview['evidence'] = evidence
                preview['meaning'] = 'Overlapping content coverage does not establish duplicate publication ownership.'
                preview['history'] = 'No history deletion/relink authorization; exact execution-history audit required by 8G.'
                recovery = 'Keep both/review only. No deletion, quality winner or recoverable deletion contract.'
        elif item.action == Action.FOLDER:
            cap, blockers = Capability.BLOCKED, ('whole_volume_or_root_transition_unsupported',)
            preview['observation'] = evidence
            preview['custom_folder_preserved'] = True
            recovery = 'Current per-file jobs cannot certify/revert whole-volume or root migration.'
        elif item.action == Action.METADATA:
            cap = Capability.LATER if f.category in ('metadata', 'identity', 'comicinfo') else Capability.NOT_APPLICABLE
            blockers = ('requires_provider_review_8d',)
            preview['current_volume'] = volume
            preview['authority_class'] = 'Selected-provider fields only; locks/local controls and historical evidence preserved.'
            preview['unsupported'] = ['identity repair', 'provider switch', 'generic field overwrite']
        elif item.action == Action.ASSOCIATION:
            cap, blockers = Capability.BLOCKED, ('exact_additive_association_proposal_required',)
            preview['direct'] = direct[f.file_id]
            preview['coverage'] = coverage[f.file_id]
            recovery = 'No destructive reassociation; C2 coverage cannot become direct identity.'
        elif item.action in (Action.RENAME, Action.COMICINFO):
            permitted = f.category == 'policy' if item.action == Action.RENAME else f.category == 'comicinfo'
            if not permitted or f.file_id not in files or not f.path or not direct[f.file_id] or general[f.file_id]:
                cap, blockers = Capability.BLOCKED, ('exact_direct_file_required',)
            elif item.action == Action.COMICINFO and f.code not in ('comicinfo_absent', 'invalid_field', 'duplicate_field'):
                cap, blockers = Capability.BLOCKED, ('comicinfo_salvage_not_supported',)
            else:
                cap = Capability.PREVIEW
                planned.append((len(updated), item))
            recovery = ('Journaled rename may have a conditional inverse after execution; no apply in 8C.'
                        if item.action == Action.RENAME else
                        'Archive rewrite required. Forward journal recovery exists; lossless undo needs retained original bytes.')
        updated.append(replace(item, capability=cap, blockers=blockers, preview_json=canonical(preview), recovery=recovery))
    if len(planned) > 250:
        raise ReviewError('preview_limit')
    if not planned:
        return tuple(updated), []
    if snapshot['planning'] is None:
        for index, _ in planned:
            updated[index] = replace(updated[index], capability=Capability.UNAVAILABLE, blockers=('planning_unavailable',))
        return tuple(updated), []
    paths = tuple(sorted({i.finding.path for _, i in planned}))
    db = sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True)
    cursor = db.cursor()
    try:
        db.execute('PRAGMA query_only=ON')
        known = load_existing_import_identities(paths, tuple(PROVIDERS), cursor)
    finally:
        cursor.close()
        db.close()
    records = snapshot['planning']
    matching = MatchingSnapshot.build((v.identity for v in records[0]), (i.identity for i in records[1]))
    roots = dict(records[2])
    groups = defaultdict(list)
    for index, item in planned:
        f = item.finding
        volume = volumes.get(f.volume_id)
        if not volume or f.path not in known:
            updated[index] = replace(updated[index], capability=Capability.BLOCKED, blockers=('exact_identity_unavailable',))
            continue
        from backend.implementations.organization_plan import contained
        host_policy = PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt')
        if (not contained(roots.get(volume['root_folder'], ''), f.path, host_policy)
                or not contained(volume['folder'] or '', f.path, host_policy)):
            updated[index] = replace(updated[index], capability=Capability.BLOCKED, blockers=('source_scope_invalid',))
            continue
        scope = DiscoveryScope(report.id, roots.get(volume['root_folder']), kind=DiscoveryKind.LIBRARY_SCAN)
        candidate = observe_import_candidate(f.path, scope, existing=known[f.path])
        if item.action == Action.COMICINFO:
            candidate = enrich_comicinfo(candidate)
        result = identify(candidate, matching)
        groups[item.action].append((index, result))
    effects = []
    for action, group in groups.items():
        policy = PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt', move=False,
            rename=action == Action.RENAME, associate=False,
            metadata=MetadataMode.REQUIRED if action == Action.COMICINFO else MetadataMode.OFF)
        context = PlanningContext.build(*records, policy)
        provisional = plan_many((r for _, r in group), context)
        paths = {p for plan in provisional.plans for p in (plan.source_path, plan.target_path, plan.target_folder, plan.target_root) if p}
        observations = observe_plan_paths(paths, policy)
        context = PlanningContext.build(*records, policy, observations)
        batch = plan_many((r for _, r in group), context)
        # Match by immutable candidate identity; duplicates retain separate items.
        plans = {p.identification.candidate.candidate_id: p for p in batch.plans}
        for index, result in group:
            plan = plans[result.candidate.candidate_id]
            item = updated[index]
            if individual_plans is not None:
                individual_plans[item.finding.id] = plan_one(result, context)
            if detached_plans is not None:
                detached_plans[item.finding.id] = plan
            view = preview_plan(plan)
            view['expected_preconditions'] = [dict(name=p.name, expected=p.expected,
                revalidate=p.revalidate_at_apply) for p in plan.preconditions]
            view['metadata']['source_digest'] = plan.metadata.source_digest
            view['metadata']['proposed_digest'] = sha256(plan.metadata.xml).hexdigest() if plan.metadata.xml else None
            view['plan_digest'] = fingerprint((view, plan.database_fingerprint))
            view['database_fingerprint'] = plan.database_fingerprint
            blocked = tuple(d.code.value for d in plan.diagnostics if d.severity.value in ('blocking', 'review')
                            or d.code.value in ('cross_filesystem_relocation', 'cross_filesystem_status_unknown'))
            if plan.status.value not in ('ready', 'no_changes') and not blocked:
                blocked = ('planner_not_ready',)
            fresh = json.loads(item.freshness_json)
            for path in (plan.source_path, plan.target_path, plan.target_folder, plan.target_root):
                if path:
                    fresh['paths'][path] = observe(path)
            updated[index] = replace(item, preview_json=canonical(view), freshness_json=canonical(fresh),
                capability=Capability.BLOCKED if blocked else Capability.PREVIEW, blockers=blocked)
            if plan.target_path:
                effects.append((item.finding.id, plan.source_path, plan.target_path))
    collisions = batch_collisions(effects)
    by_item = defaultdict(set)
    for collision in collisions:
        for identifier in collision['items']:
            by_item[identifier].add(collision['code'])
    return tuple(replace(i, capability=Capability.BLOCKED,
                         blockers=tuple(sorted(set(i.blockers) | by_item[i.finding.id])))
                 if i.finding.id in by_item else i for i in updated), collisions

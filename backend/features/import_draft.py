"""Read-only Library Import preflight; execution delegates to the organizer.

No volume, directory, file or association is created until apply.  The short
lived draft is an admission receipt; the existing OrganizationJobs are the
durable recovery authority once apply begins.
"""

import os
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4

from backend.base.definitions import SpecialVersion
from backend.base.folder_policy import (FolderPolicy, FolderPublication,
                                        FolderStatus)
from backend.base.identification import LocalMatchIssue, LocalMatchVolume
from backend.base.import_candidate import ProviderReference, ResourceKind
from backend.base.rename_policy import NamingContext, RenameIssue, RenameStatus
from backend.features.import_issue_catalog import issue_catalog
from backend.features.library_import import normalize_import_matches
from backend.features.managed_permissions import (apply_managed_permissions,
                                                  managed_directory_problem,
                                                  worker_permission_context)
from backend.implementations.acquisition_paths import (contained,
                                                       observe_artifacts)
from backend.implementations.folder_policy import FolderContext, decide_folder
from backend.implementations.organization_filesystem import execution_gate
from backend.implementations.rename_policy import (build_rename_catalog,
                                                   decide_rename)
from backend.internals.organization_plan import load_planning_records


@dataclass
class Draft:
    database: str
    matches: list[dict]
    rename: bool
    expires: float
    plans: list[dict]
    stamps: dict[str, tuple]
    delegate: str | None = None
    claimed: bool = False


_drafts: dict[str, Draft] = {}
_lock = RLock()


def _stamp(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('stale_import_source')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _rename(catalog, chosen, source, folder, naming):
    provider, provider_id = catalog['provider'], catalog['provider_id']
    authority = ProviderReference(provider, ResourceKind.VOLUME, provider_id)
    volume_id = catalog['volume_id'] or -1
    volume = LocalMatchVolume(volume_id, authority, catalog['title'], catalog['year'],
        catalog['volume_number'], catalog['publisher'], SpecialVersion(catalog['special_version']))
    all_issues = []
    selected = []
    for index, issue in enumerate(catalog['issues'], 1):
        local = issue['local_id'] or -index
        identity = LocalMatchIssue(local, volume_id, issue['number'], None,
            (ProviderReference(provider, ResourceKind.ISSUE, issue['id']),), title=issue['title'])
        all_issues.append(identity)
        if issue['id'] in chosen:
            selected.append(RenameIssue(identity, issue['title'], issue['date'],
                int(issue['id']) if provider == 'comicvine' else None))
    decision = decide_rename(NamingContext(volume, tuple(selected),
        build_rename_catalog(volume_id, all_issues, naming.issue_padding), naming,
        Path(source).name, int(provider_id) if provider == 'comicvine' else None,
        folder, os.name == 'nt', os.name != 'nt'))
    return decision.target_filename if decision.status in (RenameStatus.CALCULATED, RenameStatus.UNCHANGED) else None


def stage_import(database: str, matches: object, rename: bool) -> dict:
    normalized = normalize_import_matches(matches)
    if not 1 <= len(normalized) <= 100 or len({path for _, path in normalized}) != len(normalized):
        raise ValueError('invalid_import_batch')
    with closing(sqlite3.connect(database)) as db:
        _, _, roots, _, naming = load_planning_records(('comicvine', 'metron', 'gcd'), db.cursor())
        owners = tuple(db.execute("SELECT id,folder FROM volumes WHERE folder<>''"))
        folder_context = FolderContext.build(roots, owners, naming, os.name == 'nt', os.name != 'nt')
        grouped = {}
        for identity, path in normalized:
            grouped.setdefault((identity.provider, identity.provider_id), []).append(path)
        catalogues = {}
        for key, paths in grouped.items():
            try:
                catalogues[key] = issue_catalog(database, *key, paths)
            except Exception as error:
                if str(error) in {'unsafe_catalog_path', 'invalid_catalog_request',
                                  'unsupported_catalog_file'}:
                    raise
                catalogues[key] = None
        plans, stamps, targets = [], {}, set()
        for entry, (identity, path) in zip(matches, normalized):
            matching_roots = [(rid, root) for rid, root in roots if Path(root) in Path(path).parents]
            if len(matching_roots) != 1:
                raise ValueError('unsafe_import_source')
            root_id, root = matching_roots[0]
            source = contained(path, root)
            observed = observe_artifacts((source,), root)
            if len(observed) != 1 or observed[0].path != source:
                raise ValueError('unsupported_import_source')
            stamps[source] = _stamp(source)
            submitted_stamp = entry.get('stamp')
            if submitted_stamp is not None and (not isinstance(submitted_stamp, list)
                    or submitted_stamp != [str(value) for value in stamps[source]]):
                plans.append(dict(source=source, target=None, status='review_required',
                    identification_reasons=['stale_scan'], issue_ids=[]))
                continue
            if os.stat(source, follow_symlinks=False).st_nlink > 1:
                plans.append(dict(source=source, target=None, status='review_required',
                    identification_reasons=['shared_inode_requires_seed_policy'], issue_ids=[]))
                continue
            catalogue = catalogues[(identity.provider, identity.provider_id)]
            if catalogue is None:
                plans.append(dict(source=source, target=None, status='review_required',
                    identification_reasons=['catalog_unavailable'], issue_ids=[]))
                continue
            if type(entry.get('overlap_override', False)) is not bool:
                raise ValueError('invalid_overlap_override')
            chosen = entry.get('issue_ids')
            if (not isinstance(chosen, list) or not chosen or len(chosen) > 100
                    or any(not isinstance(i, str) for i in chosen) or len(set(chosen)) != len(chosen)):
                plans.append(dict(source=source, target=None, status='review_required',
                    identification_reasons=['issue_selection_required'], issue_ids=[]))
                continue
            available = {issue['id']: issue for issue in catalogue['issues']}
            if any(issue not in available for issue in chosen):
                plans.append(dict(source=source, target=None, status='review_required',
                    identification_reasons=['issue_not_in_selected_publication'], issue_ids=chosen))
                continue
            proposal = catalogue['proposals'].get(source, {})
            if (proposal.get('reason', '').startswith('Embedded publication identity conflicts')
                    or proposal.get('reason', '').startswith('Embedded issue identity is conflicting')
                    or proposal.get('issue_ids') and set(proposal['issue_ids']) != set(chosen)
                    and (proposal.get('confidence') == 'exact' or proposal.get('reason', '').startswith(
                        'Embedded exact issue identity'))):
                plans.append(dict(source=source, target=None, status='blocked',
                    identification_reasons=['embedded_identity_conflict'], issue_ids=chosen))
                continue
            existing_file = db.execute('SELECT id FROM files WHERE filepath=?', (source,)).fetchone()
            if existing_file:
                linked = {r[0] for r in db.execute('''SELECT e.provider_id FROM issues_files b
                    JOIN issues i ON i.id=b.issue_id
                    JOIN issue_external_ids e ON e.issue_id=i.id AND e.provider=?
                    WHERE b.file_id=? AND i.volume_id=?''',
                    (identity.provider, existing_file[0], catalogue['volume_id']))}
                plans.append(dict(source=source, target=source,
                    status='already_imported' if linked == set(chosen) else 'blocked',
                    identification_reasons=[] if linked == set(chosen) else ['existing_file_ownership'],
                    issue_ids=chosen))
                continue
            if any(available[i]['owned'] for i in chosen) and not entry.get('overlap_override', False):
                plans.append(dict(source=source, target=None, status='review_required',
                    identification_reasons=['issue_already_owned_confirm_overlap'], issue_ids=chosen))
                continue
            if entry.get('catalog_revision') and entry['catalog_revision'] != catalogue['revision']:
                plans.append(dict(source=source, target=None, status='review_required',
                    identification_reasons=['stale_catalogue'], issue_ids=chosen))
                continue
            volume_id = catalogue['volume_id']
            if volume_id:
                row = db.execute('SELECT root_folder,folder FROM volumes WHERE id=?', (volume_id,)).fetchone()
                if row is None or row[0] != root_id or not row[1]:
                    plans.append(dict(source=source, target=None, status='review_required',
                        identification_reasons=['existing_volume_folder_conflict'], issue_ids=chosen))
                    continue
                folder = contained(row[1], root)
            else:
                publication = FolderPublication(catalogue['title'], catalogue['year'],
                    catalogue['volume_number'], catalogue['publisher'],
                    ProviderReference(identity.provider, ResourceKind.VOLUME, identity.provider_id),
                    special_version=SpecialVersion(catalogue['special_version']),
                    comicvine_id=int(identity.provider_id) if identity.provider == 'comicvine' else None)
                decision = decide_folder(publication, folder_context,
                                         FolderPolicy(default_root_id=root_id))
                if decision.status != FolderStatus.CALCULATED or not decision.target_folder:
                    plans.append(dict(source=source, target=None, status='review_required',
                        identification_reasons=[d.code.value for d in decision.diagnostics], issue_ids=chosen))
                    continue
                folder = contained(decision.target_folder, root)
            if os.path.isdir(folder):
                permission_problem = managed_directory_problem(folder)
                if permission_problem:
                    try:
                        empty_destination = not os.listdir(folder)
                    except OSError:
                        empty_destination = False
                    plans.append(dict(source=source, target=folder, status='review_required',
                        volume_id=volume_id, issue_ids=chosen,
                        identification_reasons=[permission_problem],
                        repair_available=bool(volume_id and empty_destination)))
                    continue
            basename = _rename(catalogue, chosen, source, folder, naming) if rename else Path(source).name
            if not basename:
                plans.append(dict(source=source, target=None, status='review_required',
                    identification_reasons=['rename_requires_review'], issue_ids=chosen))
                continue
            target = contained(str(Path(folder) / basename), root)
            key = os.path.normcase(target).casefold()
            parent = Path(target).parent
            folded_collision = (parent.is_dir() and any(
                name.casefold() == Path(target).name.casefold() and name != Path(target).name
                for name in os.listdir(parent)))
            if (key in targets or folded_collision or source != target and os.path.lexists(target)
                    or any(owned != volume_id and os.path.normcase(folder) == os.path.normcase(other)
                           for owned, other in owners)):
                plans.append(dict(source=source, target=target, status='blocked',
                    identification_reasons=['filesystem_target_occupied'], issue_ids=chosen))
                continue
            targets.add(key)
            plans.append(dict(source=source, target=target, status='ready',
                volume_id=volume_id, publication=catalogue['title'],
                authority=dict(provider=identity.provider, id=identity.provider_id),
                issue_ids=chosen, issue_labels=[available[i]['number'] for i in chosen],
                issue_owned=[available[i]['owned'] for i in chosen],
                operation='associate' if source == target else 'move',
                registration='reuse' if volume_id else 'new',
                catalog_revision=catalogue['revision']))
        cover_sources = {}
        for entry, plan in zip(matches, plans):
            if plan['status'] != 'ready':
                continue
            for issue_id in plan['issue_ids']:
                key = (plan['authority']['provider'], plan['authority']['id'], issue_id)
                cover_sources.setdefault(key, []).append((entry, plan))
        for rows in cover_sources.values():
            if len(rows) > 1:
                for entry, plan in rows:
                    if not entry.get('overlap_override', False):
                        plan.update(status='review_required',
                            identification_reasons=['duplicate_issue_selection'])
                    else:
                        plan.setdefault('warnings', []).append('duplicate_issue_selection_reviewed')
    identifier = uuid4().hex
    with _lock:
        for key in tuple(_drafts):
            if _drafts[key].expires < time.monotonic():
                del _drafts[key]
        if len(_drafts) >= 16:
            del _drafts[next(iter(_drafts))]
        _drafts[identifier] = Draft(str(Path(database).absolute()), list(matches), rename,
                                   time.monotonic() + 600, plans, stamps)
    return dict(id=identifier, version=2, plans=plans,
                counts={state:sum(p['status'] == state for p in plans)
                        for state in ('ready', 'review_required', 'blocked', 'already_imported')},
                expires_in=600, enumeration_complete=True,
                worker=worker_permission_context())


def apply_draft(database: str, identifier: str) -> dict | None:
    with _lock:
        draft = _drafts.get(identifier)
        if draft is None:
            return None
        if draft.database != str(Path(database).absolute()) or draft.expires < time.monotonic():
            raise ValueError('stale_import_draft')
        if draft.delegate:
            delegate = draft.delegate
        elif draft.claimed:
            raise ValueError('import_apply_in_progress')
        else:
            draft.claimed = True
            delegate = None
    from backend.features.local_organization import (apply_preview,
                                                     import_preview)
    if delegate:
        result = apply_preview(database, delegate)
        result['id'] = identifier
        result['review'] += [p for p in draft.plans if p['status'] != 'ready']
        return result
    ready = [entry for entry, plan in zip(draft.matches, draft.plans) if plan['status'] == 'ready']
    if not ready:
        with _lock:
            draft.claimed = False
        return dict(id=identifier, jobs=[], review=draft.plans)
    try:
        grouped = {}
        for plan in draft.plans:
            if plan['status'] != 'ready':
                continue
            if _stamp(plan['source']) != draft.stamps[plan['source']]:
                raise ValueError('stale_import_source')
            if plan['source'] != plan['target'] and os.path.lexists(plan['target']):
                raise ValueError('occupied_import_target')
            authority = plan['authority']
            grouped.setdefault((authority['provider'], authority['id']), []).append(plan)
        for (provider, provider_id), plans in grouped.items():
            current = issue_catalog(database, provider, provider_id,
                                    [plan['source'] for plan in plans])
            if any(plan['catalog_revision'] != current['revision'] for plan in plans):
                raise ValueError('stale_import_catalogue')
        preview = import_preview(database, ready, draft.rename)
    except Exception:
        with _lock:
            draft.claimed = False
        raise
    session_id = preview['id']
    with _lock:
        draft.delegate = session_id
    from backend.features.local_organization import (_lock as session_lock,
                                                     _sessions)
    with session_lock:
        session = _sessions[session_id]
        from dataclasses import replace

        from backend.base.organization_plan import PlanStatus
        planned = {p['source']: p['target'] for p in draft.plans if p['status'] == 'ready'}
        for index, plan in enumerate(session.batch.plans):
            if plan.target_path != planned.get(plan.source_path):
                session.presentation[index].update(status='review_required',
                    identification_reasons=['stale_destination_policy'])
        session.batch = replace(session.batch, plans=tuple(
            replace(plan, status=PlanStatus.REVIEW) if plan.target_path != planned.get(plan.source_path)
            else plan for plan in session.batch.plans))
    result = apply_preview(database, session_id)
    result['id'] = identifier
    result['review'] += [p for p in draft.plans if p['status'] != 'ready']
    return result


def repair_empty_import_destination(database: str, volume_id: int,
                                    folder: str, source: str) -> dict:
    """Explicit one-folder repair after checking canonical ownership and emptiness."""
    if os.name == 'nt':
        raise ValueError('repair_unsupported_platform')
    if (type(volume_id) is not int or volume_id <= 0
            or not isinstance(folder, str) or not isinstance(source, str)):  # type: ignore[redundant-expr]
        raise ValueError('invalid_repair_request')
    with execution_gate(database), closing(sqlite3.connect(database)) as db:
        row = db.execute('''SELECT v.title,v.year,v.volume_number,v.publisher,
            v.metadata_provider,e.provider_id,v.folder,r.id,r.folder,v.special_version,v.comicvine_id
            FROM volumes v JOIN volume_external_ids e ON e.volume_id=v.id AND e.provider=v.metadata_provider
            JOIN root_folders r ON r.id=v.root_folder WHERE v.id=?''', (volume_id,)).fetchone()
        if row is None or row[6] != folder:
            raise ValueError('repair_folder_not_owned')
        title, year, number, publisher, provider, provider_id, _, root_id, root, special, cv_id = row
        contained(folder, root)
        contained(source, root)
        if len(observe_artifacts((source,), root)) != 1:
            raise ValueError('repair_source_unavailable')
        if not os.path.isdir(folder) or os.path.islink(folder) or os.listdir(folder):
            raise ValueError('repair_folder_not_empty')
        if db.execute('SELECT 1 FROM files WHERE filepath=? LIMIT 1', (folder,)).fetchone():
            raise ValueError('repair_folder_has_file_record')
        if db.execute('SELECT 1 FROM volumes WHERE id<>? AND folder=? LIMIT 1',
                      (volume_id, folder)).fetchone():
            raise ValueError('repair_folder_conflict')
        _, _, roots, _, naming = load_planning_records(('comicvine', 'metron', 'gcd'), db.cursor())
        context = FolderContext.build(roots, (), naming, os.name == 'nt', os.name != 'nt')
        publication = FolderPublication(title, year, number, publisher,
            ProviderReference(provider, ResourceKind.VOLUME, provider_id),
            special_version=SpecialVersion(special), comicvine_id=cv_id)
        canonical = decide_folder(publication, context, FolderPolicy(default_root_id=root_id))
        if canonical.target_folder != folder:
            raise ValueError('repair_folder_not_canonical')
        apply_managed_permissions(folder, directory=True, repair=True)
        problem = managed_directory_problem(folder)
        if problem:
            raise ValueError(problem)
        return dict(folder=folder, repaired=True)

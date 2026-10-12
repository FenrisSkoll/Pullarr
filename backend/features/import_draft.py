"""Durable Library Import review; organizer jobs own filesystem execution."""

import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from backend.base.definitions import SpecialVersion
from backend.base.folder_policy import (FolderPolicy, FolderPublication,
                                        FolderStatus)
from backend.base.identification import LocalMatchIssue, LocalMatchVolume
from backend.base.import_candidate import ProviderReference, ResourceKind
from backend.base.quality import QualityError
from backend.base.rename_policy import NamingContext, RenameIssue, RenameStatus
from backend.features.import_issue_catalog import issue_catalog
from backend.features.library_import import normalize_import_matches
from backend.features.managed_permissions import (apply_managed_permissions,
                                                  managed_directory_problem,
                                                  worker_permission_context)
from backend.implementations.acquisition_paths import (contained,
                                                       observe_artifacts)
from backend.implementations.file_quality import analyze
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
    revision: int
    plans: list[dict]
    stamps: dict[str, tuple]
    delegate: str | None = None
    claimed: bool = False


_drafts: dict[str, Draft] = {}
_lock = RLock()
_applying: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(database: str, identifier: str) -> Draft | None:
    if type(identifier) is not str or len(identifier) != 32 or not all(
            character in '0123456789abcdef' for character in identifier):
        return None
    with closing(sqlite3.connect(database)) as db:
        header = db.execute('''SELECT version,state,revision,rename,delegate
            FROM library_import_drafts WHERE id=?''', (identifier,)).fetchone()
        if header is None:
            return None
        if header[0] != 3 or header[1] == 'discarded':
            raise ValueError('unsupported_import_draft')
        rows = db.execute('''SELECT selection,plan,stamp FROM library_import_draft_rows
            WHERE draft_id=? ORDER BY ordinal''', (identifier,)).fetchall()
    matches = [json.loads(row[0]) for row in rows]
    plans = [json.loads(row[1]) for row in rows]
    stamps = {plan['source']: tuple(json.loads(row[2])) for plan, row in zip(plans, rows)}
    return Draft(str(Path(database).absolute()), matches, bool(header[3]), header[2],
                 plans, stamps, header[4], header[1] == 'applying')


def get_draft(database: str, identifier: str, *, offset: int = 0, limit: int = 100) -> dict | None:
    if not 0 <= offset <= 1000000 or not 1 <= limit <= 100:
        raise ValueError('invalid_draft_page')
    if type(identifier) is not str or len(identifier) != 32 or not all(
            character in '0123456789abcdef' for character in identifier):
        return None
    with closing(sqlite3.connect(database)) as db:
        header = db.execute('''SELECT state,created_at,updated_at,result,scope,revision,rename,version
            FROM library_import_drafts
            WHERE id=?''', (identifier,)).fetchone()
        if header is None:
            return None
        if header[7] != 3 or header[0] == 'discarded':
            raise ValueError('unsupported_import_draft')
        total = db.execute('''SELECT COUNT(*) FROM library_import_draft_rows
            WHERE draft_id=?''', (identifier,)).fetchone()[0]
        saved_rows = db.execute('''SELECT row_id,selection,plan,receipt,stamp
            FROM library_import_draft_rows
            WHERE draft_id=? ORDER BY ordinal LIMIT ? OFFSET ?''',
            (identifier, limit, offset)).fetchall()
        jobs = dict(db.execute('''SELECT json_extract(intent,'$.source'),id
            FROM organization_jobs WHERE batch_id=?
            AND json_extract(intent,'$.library_import')=1
            ORDER BY created_at,id LIMIT 1001''', ('local-preview:' + identifier,)))
    from backend.features.import_completion import verify_import_job
    page_rows = []
    for row_id, raw_selection, raw_plan, raw_receipt, raw_stamp in saved_rows:
        selection, plan = json.loads(raw_selection), json.loads(raw_plan)
        prior = json.loads(raw_receipt) if raw_receipt else None
        job_id = prior.get('job_id') if prior else jobs.get(plan['source'])
        current = verify_import_job(database, job_id, plan) if job_id else prior
        stale_reason = None
        if current is None and plan['status'] not in ('excluded','already_imported'):
            try:
                if _stamp(plan['source']) != tuple(json.loads(raw_stamp)):
                    stale_reason = 'source_changed_revalidation_needed'
            except (OSError, ValueError):
                stale_reason = 'source_missing_revalidation_needed'
        page_rows.append(dict(row_id=row_id, selection=selection, plan=plan,
                              receipt=current, stale_reason=stale_reason))
    visible_state = header[0]
    if visible_state == 'completed' and any(row['receipt'] and
            row['receipt']['state'] != 'completed' for row in page_rows):
        visible_state = 'recovery_required'
    if visible_state in ('draft','ready','needs_review') and any(
            row['stale_reason'] for row in page_rows):
        visible_state = 'needs_review'
    return dict(id=identifier, version=3, revision=header[5], state=visible_state,
                created_at=header[1], updated_at=header[2], rename=bool(header[6]),
                rows=page_rows,
                total=total, next_offset=offset + limit if offset + limit < total else None,
                result=json.loads(header[3]) if header[3] else None,
                scope=json.loads(header[4]))


def list_drafts(database: str, *, offset: int = 0, limit: int = 50) -> dict:
    if not 0 <= offset <= 1000000 or not 1 <= limit <= 100:
        raise ValueError('invalid_draft_page')
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute('''SELECT d.id,d.state,d.revision,d.rename,d.updated_at,
            (SELECT COUNT(*) FROM library_import_draft_rows r WHERE r.draft_id=d.id)
            FROM library_import_drafts d WHERE d.state<>'discarded'
            ORDER BY d.updated_at DESC,d.id LIMIT ? OFFSET ?''', (limit + 1, offset)).fetchall()
    return dict(items=[dict(id=r[0], state=r[1], revision=r[2], rename=bool(r[3]),
                            updated_at=r[4], rows=r[5]) for r in rows[:limit]],
                next_offset=offset + limit if len(rows) > limit else None)


def require_applying_draft(database: str, identifier: str | None, matches: object,
                           rename: bool) -> None:
    """Private mutation capability, checked against durable reviewed selections."""
    draft = _load(database, identifier or '')
    if (draft is None or not draft.claimed or draft.rename != rename or
            list(matches) != [entry for entry, plan in zip(draft.matches, draft.plans)
                              if plan['status'] == 'ready']):
        raise ValueError('legacy_import_requires_review')


def _review_row(db: sqlite3.Connection, entry: dict) -> tuple[str, dict, dict, tuple]:
    if type(entry) is not dict or not isinstance(entry.get('filepath'), str):
        raise ValueError('invalid_import_review_row')
    source = entry['filepath']
    roots = tuple(db.execute('SELECT id,folder FROM root_folders ORDER BY id'))
    matching = [(root_id, root) for root_id, root in roots if Path(root) in Path(source).parents]
    if len(matching) != 1:
        raise ValueError('unsafe_import_source')
    root_id, root = matching[0]
    source = contained(source, root)
    observed = observe_artifacts((source,), root)
    if len(observed) != 1 or observed[0].path != source:
        raise ValueError('unsupported_import_source')
    stamp = _stamp(source)
    supplied = entry.get('stamp')
    if supplied is not None and supplied != [str(value) for value in stamp]:
        raise ValueError('stale_import_source')
    choice = {key:entry[key] for key in ('filepath','stamp','provider','provider_id',
        'issue_ids','catalog_revision','issue_override','overlap_override','selected',
        'issue_labels','issue_reason','manual_issues','catalog_completeness',
        'reported_issue_count','loaded_issue_count','selected_issues') if key in entry}
    if 'provider' in choice or 'provider_id' in choice:
        if 'provider' not in choice or 'provider_id' not in choice:
            raise ValueError('invalid_import_review_row')
        identity, _ = normalize_import_matches([choice])[0]
        choice['provider'], choice['provider_id'] = identity.provider, identity.provider_id
    issues = choice.get('issue_ids', [])
    if (not isinstance(issues, list) or len(issues) > 100 or
            any(not isinstance(i, str) or not i for i in issues) or
            len(set(issues)) != len(issues)):
        raise ValueError('invalid_import_review_row')
    if type(choice.get('selected', True)) is not bool:
        raise ValueError('invalid_import_review_row')
    for key in ('issue_override','overlap_override','manual_issues'):
        if type(choice.get(key, False)) is not bool:
            raise ValueError('invalid_import_review_row')
    if len(json.dumps(choice, default=str).encode()) > 16384:
        raise ValueError('invalid_import_review_row')
    display = entry.get('display')
    if display is not None:
        if not isinstance(display, dict) or len(json.dumps(display).encode()) > 8192:
            raise ValueError('invalid_import_review_row')
        choice['display'] = display
    choice['filepath'] = source
    plan = dict(source=source, target=None, status='review_required', root_id=root_id,
                issue_ids=issues, identification_reasons=[
                    'issue_selection_required' if choice.get('provider') else 'publication_selection_required'])
    return source, choice, plan, stamp


def save_review_draft(database: str, rows: list[dict], *, identifier: str | None = None,
                      revision: int | None = None, scope: dict | None = None) -> dict:
    """Save up to one scan page of choices with an optimistic revision.

    This is observational. The saved rows are never trusted for execution:
    stage_import and apply_draft independently validate them again.
    """
    if type(rows) is not list or not 1 <= len(rows) <= 100:
        raise ValueError('invalid_import_review_batch')
    if scope is not None and (type(scope) is not dict or set(scope) -
            {'folder_filter','folder_limit','page_size'} or
            len(json.dumps(scope).encode()) > 2048):
        raise ValueError('invalid_import_review_scope')
    with closing(sqlite3.connect(database)) as lookup:
        prepared = [_review_row(lookup, entry) for entry in rows]
    if len({entry[0] for entry in prepared}) != len(prepared):
        raise ValueError('invalid_import_review_batch')
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        if identifier is None:
            identifier = uuid4().hex
            now = _now()
            db.execute('''INSERT INTO library_import_drafts
                (id,version,state,revision,rename,created_at,updated_at,scope)
                VALUES(?,3,'draft',1,0,?,?,?)''',
                (identifier, now, now, json.dumps(scope or {'kind':'selected_files'})))
            current = 0
        else:
            header = db.execute('SELECT state,revision FROM library_import_drafts WHERE id=?',
                                (identifier,)).fetchone()
            if (header is None or header[0] not in ('draft','needs_review','ready') or
                    type(revision) is not int or revision != header[1]):
                raise ValueError('stale_import_revision')
            if scope is not None:
                db.execute('UPDATE library_import_drafts SET scope=? WHERE id=?',
                           (json.dumps(scope), identifier))
            current = header[1]
        existing = {r[0]:(r[1],r[2]) for r in db.execute('''SELECT source,ordinal,row_id
            FROM library_import_draft_rows WHERE draft_id=?''', (identifier,))}
        next_ordinal = max((r[0] for r in existing.values()), default=-1) + 1
        for source, choice, plan, stamp in prepared:
            if _stamp(source) != stamp:
                raise ValueError('stale_import_source')
            ordinal, row_id = existing.get(source, (next_ordinal, uuid4().hex))
            if source not in existing:
                next_ordinal += 1
            db.execute('''INSERT INTO library_import_draft_rows
                (draft_id,ordinal,row_id,source,selection,plan,stamp)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(draft_id,ordinal) DO UPDATE SET
                selection=excluded.selection,plan=excluded.plan,stamp=excluded.stamp''',
                (identifier, ordinal, row_id, source, json.dumps(choice), json.dumps(plan),
                 json.dumps(stamp)))
        db.execute('''UPDATE library_import_drafts SET state='draft',revision=?,updated_at=?,
            delegate=NULL,result=NULL WHERE id=?''', (current + 1, _now(), identifier))
        db.commit()
    return dict(id=identifier, version=3, revision=current + 1, state='draft',
                saved=len(rows), total=next_ordinal)


def discard_draft(database: str, identifier: str, revision: int) -> dict:
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT state,revision FROM library_import_drafts WHERE id=?',
                         (identifier,)).fetchone()
        if row is None or type(revision) is not int or row[1] != revision:
            raise ValueError('stale_import_revision')
        if (row[0] in ('applying','completed','partially_completed','recovery_required') or
                db.execute('SELECT 1 FROM organization_jobs WHERE batch_id=? LIMIT 1',
                           ('local-preview:' + identifier,)).fetchone()):
            raise ValueError('import_draft_has_journal')
        db.execute('''UPDATE library_import_drafts SET state='discarded',
            revision=revision+1,updated_at=? WHERE id=?''', (_now(),identifier))
        db.commit()
    return dict(id=identifier, state='discarded', revision=revision + 1)


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


def stage_import(database: str, matches: object, rename: bool, *,
                 identifier: str | None = None, revision: int | None = None) -> dict:
    normalized = normalize_import_matches(matches)
    if not 1 <= len(normalized) <= 1000 or len({path for _, path in normalized}) != len(normalized):
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
                catalog_revision=catalogue['revision'],
                catalog_completeness=catalogue['completeness'],
                reported_issue_count=catalogue['reported_issue_count'],
                loaded_issue_count=catalogue['loaded_issue_count'],
                selected_issues=[{key:available[i].get(key) for key in
                    ('id','number','title','date')} for i in chosen]))
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
    identifier = identifier or uuid4().hex
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        now = _now()
        header = db.execute('SELECT state,revision FROM library_import_drafts WHERE id=?',
                            (identifier,)).fetchone()
        if header:
            if (header[0] not in ('draft','ready','needs_review') or
                    type(revision) is not int or revision != header[1]):
                raise ValueError('stale_import_revision')
            saved = {r[0]:(r[1],r[2],json.loads(r[3]),tuple(json.loads(r[4])))
                     for r in db.execute('''SELECT source,ordinal,row_id,selection,stamp
                         FROM library_import_draft_rows WHERE draft_id=?''', (identifier,))}
            if any(p['source'] not in saved or saved[p['source']][3] != stamps[p['source']]
                   for p in plans):
                raise ValueError('stale_import_review')
            planned = {p['source']:(entry,p) for entry,p in zip(matches,plans)}
            ordered = []
            for source,(ordinal,row_id,choice,stamp) in sorted(saved.items(), key=lambda item:item[1][0]):
                if source in planned:
                    ordered.append((row_id,*planned[source],stamp))
                else:
                    ordered.append((row_id,choice,dict(source=source,target=None,
                        status='excluded',issue_ids=choice.get('issue_ids',[]),
                        identification_reasons=['not_selected_for_import']),stamp))
            matches = [item[1] for item in ordered]
            plans = [item[2] for item in ordered]
            db.execute('DELETE FROM library_import_draft_rows WHERE draft_id=?', (identifier,))
            next_revision = header[1] + 1
            state = 'ready' if any(p['status'] == 'ready' for p in plans) else 'needs_review'
            db.execute('''UPDATE library_import_drafts SET state=?,revision=?,rename=?,
                updated_at=?,delegate=NULL,result=NULL WHERE id=?''',
                (state,next_revision,int(rename),now,identifier))
        else:
            ordered = [(uuid4().hex,entry,plan,stamps[plan['source']])
                       for entry,plan in zip(matches,plans)]
            next_revision = 1
            state = 'ready' if any(p['status'] == 'ready' for p in plans) else 'needs_review'
            db.execute('''INSERT INTO library_import_drafts
                (id,version,state,revision,rename,created_at,updated_at,scope)
                VALUES(?,3,?,1,?,?,?,?)''',
                (identifier, state, int(rename), now, now,
                 json.dumps({'kind':'selected_files'})))
        db.executemany('''INSERT INTO library_import_draft_rows
            (draft_id,ordinal,row_id,source,selection,plan,stamp) VALUES(?,?,?,?,?,?,?)''',
            ((identifier, index, row_id, plan['source'],
              json.dumps(entry, separators=(',', ':')),
              json.dumps(plan, separators=(',', ':')),
              json.dumps(stamp))
             for index, (row_id,entry,plan,stamp) in enumerate(ordered)))
        db.commit()
    return dict(id=identifier, version=3, revision=next_revision, state=state, plans=plans,
                counts={state:sum(p['status'] == state for p in plans)
                        for state in ('ready', 'review_required', 'blocked', 'already_imported')},
                enumeration_complete=True,
                worker=worker_permission_context())


def _set_draft_state(database: str, identifier: str, state: str, result: dict | None = None) -> None:
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute('''UPDATE library_import_drafts SET state=?,revision=revision+1,
            updated_at=?,result=? WHERE id=?''',
            (state, _now(), json.dumps(result, separators=(',', ':')) if result else None,
             identifier))
        db.commit()


def _receipt_result(database: str, identifier: str, draft: Draft,
                    jobs: list[dict] | None = None, reviews: list[dict] | None = None) -> dict:
    from backend.features.import_completion import verify_import_job
    with closing(sqlite3.connect(database)) as db:
        state = db.execute('SELECT state FROM library_import_drafts WHERE id=?',
                           (identifier,)).fetchone()[0]
        rows = db.execute('''SELECT id,intent FROM organization_jobs
            WHERE batch_id=? ORDER BY created_at,id''', ('local-preview:' + identifier,)).fetchall()
        row_ids = dict(db.execute('''SELECT source,row_id FROM library_import_draft_rows
            WHERE draft_id=?''', (identifier,)))
    by_source = {}
    for job_id, raw in rows:
        intent = json.loads(raw)
        if intent.get('library_import'):
            by_source.setdefault(intent['source'], job_id)
    presented = []
    job_outcomes = {item['source']: item for item in jobs or []}
    for plan in draft.plans:
        if plan['status'] != 'ready':
            continue
        job_id = by_source.get(plan['source'])
        if not job_id and jobs is not None:
            continue
        if job_id:
            receipt = verify_import_job(database, job_id, plan)
        else:
            prior = job_outcomes.get(plan['source'])
            receipt = prior or dict(source=plan['source'], target=plan['target'],
                                    state='running' if state == 'applying' else 'recovery_required',
                                    error=None if state == 'applying' else 'journal_job_missing')
        receipt['row_id'] = row_ids.get(plan['source'])
        presented.append(receipt)
    review_rows = [p for p in draft.plans if p['status'] != 'ready'] + (reviews or [])
    counts = {state: sum(row['state'] == state for row in presented)
              for state in ('completed', 'recovery_required', 'failed', 'running', 'pending')}
    counts['needs_review'] = sum(plan['status'] == 'review_required' for plan in review_rows)
    counts['blocked'] = sum(plan['status'] == 'blocked' for plan in review_rows)
    counts['excluded'] = sum(plan['status'] == 'excluded' for plan in review_rows)
    counts['recoverable'] = counts['recovery_required']
    counts['still_running'] = counts['running'] + counts['pending']
    return dict(id=identifier, jobs=presented,
                review=review_rows,
                counts=counts)


def _persist_receipts(database: str, identifier: str, result: dict) -> None:
    by_source = {item['source']: item for item in result['jobs']}
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        for source, receipt in by_source.items():
            db.execute('''UPDATE library_import_draft_rows SET receipt=?
                WHERE draft_id=? AND source=?''',
                (json.dumps(receipt, separators=(',', ':')), identifier, source))
        db.commit()


def apply_draft(database: str, identifier: str, revision: int | None = None) -> dict | None:
    draft = _load(database, identifier)
    if draft is None:
        return None
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ValueError('invalid_import_revision')
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT state,revision FROM library_import_drafts WHERE id=?',
                         (identifier,)).fetchone()
        if row is None:
            return None
        state, current_revision = row
        if state in ('completed', 'partially_completed', 'recovery_required', 'applying'):
            db.commit()
            return _receipt_result(database, identifier, draft)
        if revision is not None and revision != current_revision:
            raise ValueError('stale_import_revision')
        ready = [entry for entry, plan in zip(draft.matches, draft.plans)
                 if plan['status'] == 'ready']
        if not ready:
            db.commit()
            return dict(id=identifier, jobs=[],
                        review=draft.plans, counts={'needs_review':len(draft.plans)})
        db.execute('''UPDATE library_import_drafts SET state='applying',
            revision=revision+1,updated_at=? WHERE id=?''', (_now(), identifier))
        db.commit()
    with _lock:
        _applying.add(identifier)
    grouped = {}
    for plan in draft.plans:
        if plan['status'] != 'ready':
            continue
        try:
            if _stamp(plan['source']) != draft.stamps[plan['source']]:
                raise ValueError('stale_import_source')
            if plan['source'] != plan['target'] and os.path.lexists(plan['target']):
                raise ValueError('occupied_import_target')
            suffix = Path(plan['source']).suffix.casefold()
            if suffix in ('.cbr','.rar'):
                with open(plan['source'], 'rb') as stream:
                    magic = stream.read(8)
                if not magic.startswith((b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01\x00')):
                    raise ValueError('archive_invalid')
            facts = analyze(plan['source'], structural_only=True)
            if suffix in ('.cbz','.zip') and facts['integrity'] != 'valid':
                raise ValueError('archive_invalid')
            if _stamp(plan['source']) != draft.stamps[plan['source']]:
                raise ValueError('stale_import_source')
            plan['validated_archive_sha256'] = facts['sha256']
        except (OSError, ValueError, QualityError) as error:
            plan.update(status='blocked', identification_reasons=[str(error)])
            continue
        authority = plan['authority']
        grouped.setdefault((authority['provider'], authority['id']), []).append(plan)
    for (provider, provider_id), plans in grouped.items():
        try:
            current = issue_catalog(database, provider, provider_id,
                                    [plan['source'] for plan in plans])
            for plan in plans:
                if plan['catalog_revision'] != current['revision']:
                    plan.update(status='review_required',
                                identification_reasons=['stale_import_catalogue'])
        except Exception:
            for plan in plans:
                plan.update(status='review_required',
                            identification_reasons=['catalog_unavailable'])
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        db.executemany('''UPDATE library_import_draft_rows SET plan=?
            WHERE draft_id=? AND source=?''',
            ((json.dumps(plan), identifier, plan['source']) for plan in draft.plans))
        db.commit()
    ready = [entry for entry, plan in zip(draft.matches, draft.plans)
             if plan['status'] == 'ready']
    if not ready:
        result = _receipt_result(database, identifier, draft, [], [])
        _set_draft_state(database, identifier, 'needs_review', {'counts':result['counts']})
        with _lock:
            _applying.discard(identifier)
        return result
    from backend.features.local_organization import (_lock as session_lock,
                                                     _sessions, apply_preview,
                                                     import_preview)
    try:
        preview = import_preview(database, ready, draft.rename, draft_id=identifier)
        with session_lock:
            session = _sessions[preview['id']]
            from dataclasses import replace

            from backend.base.organization_plan import PlanStatus
            planned = {p['source']: p['target'] for p in draft.plans if p['status'] == 'ready'}
            for index, plan in enumerate(session.batch.plans):
                if plan.target_path != planned.get(plan.source_path):
                    session.presentation[index].update(status='review_required',
                        identification_reasons=['stale_destination_policy'])
            session.batch = replace(session.batch, plans=tuple(
                replace(plan, status=PlanStatus.REVIEW)
                if plan.target_path != planned.get(plan.source_path) else plan
                for plan in session.batch.plans))
        result = apply_preview(database, preview['id'])
    except Exception:
        # Registration may already exist. Keep the durable draft visible for
        # guarded journal or empty-folder recovery; never call it completed.
        _set_draft_state(database, identifier, 'recovery_required')
        with _lock:
            _applying.discard(identifier)
        raise
    final = _receipt_result(database, identifier, draft, result['jobs'], result['review'])
    _persist_receipts(database, identifier, final)
    complete = final['counts']['completed']
    state = ('completed' if complete == len(ready) and not final['review']
             else 'partially_completed' if complete else 'needs_review' if final['review']
             else 'recovery_required')
    _set_draft_state(database, identifier, state, {'counts': final['counts']})
    with _lock:
        _applying.discard(identifier)
    return final


def resume_draft(database: str, identifier: str, revision: int) -> dict:
    """Resume pending journal jobs; leave interrupted effects for reviewed recovery."""
    draft = _load(database, identifier)
    if draft is None:
        raise ValueError('import_draft_not_found')
    if type(revision) is not int or revision != draft.revision:
        raise ValueError('stale_import_revision')
    with _lock:
        if identifier in _applying:
            return _receipt_result(database, identifier, draft)
    with closing(sqlite3.connect(database)) as db:
        state = db.execute('SELECT state FROM library_import_drafts WHERE id=?',
                           (identifier,)).fetchone()[0]
        job_ids = [r[0] for r in db.execute('''SELECT id FROM organization_jobs
            WHERE batch_id=? ORDER BY created_at,id''', ('local-preview:' + identifier,))]
        roots = tuple(r[0] for r in db.execute('SELECT folder FROM root_folders ORDER BY id'))
    if state not in ('applying','recovery_required','partially_completed','completed'):
        raise ValueError('import_draft_not_applying')
    if job_ids:
        from backend.base.organization_job import JobState
        from backend.features.organization_execution import \
            OrganizationExecutor
        executor = OrganizationExecutor(database, roots)
        try:
            for job_id in job_ids:
                if executor.store.get(job_id).state == JobState.PENDING:
                    executor.apply_job(job_id)
        finally:
            executor.close()
        result = _receipt_result(database, identifier, draft)
        _persist_receipts(database, identifier, result)
        ready_count = sum(p['status'] == 'ready' for p in draft.plans)
        completed = result['counts']['completed']
        final_state = ('completed' if completed == ready_count and not result['review']
                       else 'partially_completed' if completed else 'recovery_required')
        _set_draft_state(database, identifier, final_state, {'counts':result['counts']})
        return result
    # No journal was created before interruption. Revalidate the original
    # proposal through the normal apply entry point; existing metadata/folders
    # are never by themselves counted as an imported file.
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        changed = db.execute('''UPDATE library_import_drafts SET state='ready',
            revision=revision+1,updated_at=? WHERE id=? AND revision=?''',
            (_now(), identifier, revision))
        if changed.rowcount != 1:
            raise ValueError('stale_import_revision')
        db.commit()
    return apply_draft(database, identifier, revision + 1) or {}


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

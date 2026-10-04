"""Scoped, explicit issue association review. GET is observational; POST is DB-only.

The existing scan writer guard supplies provider/reservation exclusion. A single
SQLite transaction is the durable boundary: no file effects or new journal needed.
"""
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from backend.base.identification import MatchReason, MatchState
from backend.base.import_candidate import InspectionState
from backend.base.organization_plan import PlanCode, Severity
from backend.features.local_organization import _lock, _sessions
from backend.features.organization_plan import observe_plan_paths
from backend.implementations.acquisition_paths import contained
from backend.implementations.identification import MatchingSnapshot
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.organization_plan import PlanningContext, plan_one
from backend.internals.import_identity import load_existing_import_identities
from backend.internals.organization_plan import load_planning_records
from backend.internals.scan_mutation import require_unreserved, scan_mutation


class LocalReviewError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value):
    value = re.sub(r'https?://\S+', '[link omitted]', str(value if value is not None else ''), flags=re.I)
    return ''.join(c for c in value if c.isprintable())[:300]


def _stamp(path):
    stat = os.stat(path, follow_symlinks=False)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _session(database, volume_id, identifier, index):
    session = _sessions.get(identifier)
    if (session is None or session.database != str(Path(database).absolute())
            or session.expires < time.monotonic() or session.volume_id != volume_id
            or not 0 <= index < len(session.batch.plans)):
        raise LocalReviewError('stale_preview')
    return session


def _fresh(db, session, index):
    original = session.batch.plans[index]
    if tuple(db.execute('SELECT id,issue_number,title FROM issues WHERE volume_id=? ORDER BY id', (session.volume_id,))) != session.issue_catalog:
        raise LocalReviewError('stale_preview')
    row = db.execute('''SELECT v.folder,r.folder FROM volumes v JOIN root_folders r
        ON r.id=v.root_folder WHERE v.id=?''', (session.volume_id,)).fetchone()
    if row is None or not row[0] or not row[1]:
        raise LocalReviewError('stale_preview')
    folder = contained(row[0], row[1])
    if (os.path.normcase(folder) != os.path.normcase(original.identification.candidate.scope.root or '')
            or row[1] not in session.roots):
        raise LocalReviewError('stale_preview')
    path = contained(original.source_path, folder)
    for owner, other in db.execute("SELECT id,folder FROM volumes WHERE id<>? AND folder<>''",(session.volume_id,)):
        if os.path.normcase(os.path.realpath(other)) == os.path.normcase(folder):
            raise LocalReviewError('publication_or_file_conflict')
    if index not in session.stamps or _stamp(path) != session.stamps[index]:
        raise LocalReviewError('stale_preview')
    # Revalidate the retained evidence, never rediscover its publication. The
    # unchanged file stamp binds the parsed filename/ComicInfo receipt to bytes.
    try:
        volumes, issues, _, _, _ = load_planning_records(tuple(PROVIDERS), db.cursor(), volume_ids=(session.volume_id,))
        snapshot = MatchingSnapshot.build((v.identity for v in volumes), (i.identity for i in issues))
        existing = load_existing_import_identities((path,), tuple(PROVIDERS), db.cursor()).get(path)
    except ValueError:
        raise LocalReviewError('stale_preview') from None
    current = replace(original, identification=replace(original.identification,
        candidate=replace(original.identification.candidate, existing=existing)))
    def association_key(candidate):
        existing = candidate.existing
        return (existing.file_id, tuple((a.volume_id, a.issue_id, a.forced, a.selected_volume, a.selected_issue)
                                        for a in existing.associations)) if existing else None
    if (snapshot.snapshot_id != original.identification.snapshot_id
            or current.identification.selected is None
            or original.identification.selected is None
            or session.volume_id not in snapshot.volumes
            or snapshot.volumes[session.volume_id].authority != original.identification.selected.provider_identity
            or (index not in session.reviewed and association_key(current.identification.candidate)
                != association_key(original.identification.candidate))):
        raise LocalReviewError('stale_preview')
    if index in session.reviewed:
        existing = current.identification.candidate.existing
        if (not existing or tuple(sorted(a.issue_id for a in existing.associations if a.issue_id is not None)) != session.reviewed[index]
                or any(a.volume_id != session.volume_id or a.forced != session.association_forced.get(index, True) or a.issue_id is None for a in existing.associations)):
            raise LocalReviewError('stale_preview')
    if _stamp(path) != session.stamps[index]:
        raise LocalReviewError('stale_preview')
    return current


def _blocked(plan, volume_id):
    result = plan.identification
    candidate = result.candidate
    if (candidate.archive_state == InspectionState.FAILED
            or candidate.file.stat_state != InspectionState.PRESENT
            or MatchReason.DIAGNOSTIC in result.reasons
            or MatchReason.INVALID_LOCAL in result.reasons
            or MatchReason.IDENTITY_CONFLICT in result.reasons
            or MatchReason.UNKNOWN_IDENTITY in result.reasons
            or candidate.existing and any(a.volume_id != volume_id for a in candidate.existing.associations)):
        return 'publication_or_file_conflict'
    return None


def _admit_selection(db, plan, ids):
    """An explicit issue choice does not waive folder/path/ownership admission."""
    volumes, issues, roots, files, naming = load_planning_records(tuple(PROVIDERS), db.cursor(), volume_ids=(plan.identification.selected.local_volume_id,))
    paths = {plan.source_path, str(Path(plan.source_path).parent)}
    volume_id = plan.identification.selected.local_volume_id
    paths.update(v.folder for v in volumes if v.identity.id == volume_id)
    paths.update(path for _, path in roots)
    context = PlanningContext.build(volumes, issues, roots, files, naming, plan.policy,
                                    observe_plan_paths(paths, plan.policy))
    selected = replace(plan.identification.selected, local_issue_ids=ids, rejections=(), review_reasons=())
    admitted = plan_one(replace(plan.identification, state=MatchState.AUTOMATIC, selected=selected,
                               reasons=(MatchReason.FORCED,)), context)
    if (admitted.source_path != admitted.target_path
            or any(d.severity in (Severity.REVIEW, Severity.BLOCKING) and d.code != PlanCode.ASSOCIATIONS
                   for d in admitted.diagnostics)):
        raise LocalReviewError('publication_or_file_conflict')


def _details(db, session, index, plan, query):
    candidate = plan.identification.candidate
    document = candidate.comicinfo.document
    filename = candidate.filename
    evidence = []
    if filename:
        evidence.extend([dict(label='Filename publication', value=_text(filename.series)),
                         dict(label='Filename issue', value=_text(filename.legacy_issue_number))])
    if document:
        for name in ('Series', 'Number', 'Title', 'Volume', 'Year'):
            for item in document.values(name)[:4]:
                evidence.append(dict(label='ComicInfo '+name, value=_text(item.text)))
    for claim in candidate.claims[:10]:
        ref = claim.reference
        evidence.append(dict(label='Embedded '+ref.kind.value+' identity',
                             value=_text(ref.provider+':'+ref.provider_id)))
    reasons = []
    fields = {'Series/filename':'ComicInfo series differs from the filename publication.',
              'Number/filename':'ComicInfo issue number differs from the filename issue.',
              'Year/filename':'ComicInfo year differs from the filename year.',
              'Series/local_association':'ComicInfo series differs from the existing publication association.',
              'Number/local_association':'ComicInfo issue number differs from the existing issue association.'}
    for diagnostic in candidate.conflicts:
        for key, message in fields.items():
            if diagnostic.provenance.locator.endswith('/'+key):
                reasons.append(message)
    messages = {MatchReason.TITLE_CONFLICT:'The local publication title differs from this managed volume.',
                MatchReason.IDENTITY_CONFLICT:'An embedded provider identity contradicts this publication.',
                MatchReason.UNKNOWN_IDENTITY:'The embedded identity cannot be verified within this publication.',
                MatchReason.ISSUE_AMBIGUOUS:'More than one issue has this number.',
                MatchReason.ISSUE_MISSING:'The issue could not be determined.',
                MatchReason.NUMBER_UNAVAILABLE:'The issue label or range cannot be resolved safely.',
                MatchReason.INVALID_LOCAL:'The existing association or embedded issue belongs to another publication.'}
    reasons.extend(messages[r] for r in plan.identification.reasons if r in messages)
    if not reasons and MatchReason.EVIDENCE_CONFLICT in plan.identification.reasons:
        reasons.append('Embedded issue identity or metadata disagrees with local issue evidence. Compare the values below.')
    rows = db.execute('''SELECT id,issue_number,title FROM issues WHERE volume_id=?
        AND (?='' OR instr(lower(issue_number || ' ' || coalesce(title,'')),lower(?))>0)
        ORDER BY calculated_issue_number,id LIMIT 201''', (session.volume_id, query, query)).fetchall()
    existing = [dict(issue_id=a.issue_id, label=_text(a.issue_number), forced=a.forced)
                for a in candidate.existing.associations] if candidate.existing else []
    return dict(id=str(index), volume_id=session.volume_id, filename=_text(Path(plan.source_path).name),
        publication=_text(plan.identification.selected.title), evidence=evidence,
        reasons=list(dict.fromkeys(reasons)) or ['Choose the intended issue within this managed volume.'],
        existing=existing, embedded_issue_ids=_embedded_issues(db, plan),
        blocked=_blocked(plan, session.volume_id), more=len(rows)>200,
        issues=[dict(id=r[0], label=_text('#'+r[1]+(' '+r[2] if r[2] else ''))) for r in rows[:200]])


def review_issue(database, volume_id, identifier, index, *, issue_ids=None, query='', override=False):
    if type(override) is not bool or not isinstance(query, str) or len(query)>100:
        raise LocalReviewError('invalid_review')
    if issue_ids is not None and (not isinstance(issue_ids, list) or not 1 <= len(issue_ids) <= 100
            or any(type(i) is not int or i<=0 for i in issue_ids) or len(set(issue_ids))!=len(issue_ids)):
        raise LocalReviewError('invalid_issue_selection')
    with _lock, closing(sqlite3.connect(database, timeout=30)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        session = _session(database, volume_id, identifier, index)
        if issue_ids is None:
            return _details(db, session, index, _fresh(db, session, index), query)
        ids = tuple(sorted(issue_ids))
        if index in session.jobs or index in session.reviewed and session.reviewed[index] != ids:
            raise LocalReviewError('stale_preview')
        with scan_mutation(db.cursor(), volume_id, prune_unmatched=False):
            plan = _fresh(db, session, index)
            require_unreserved(db.cursor(), plan.source_path)
            if _blocked(plan, volume_id):
                raise LocalReviewError('publication_or_file_conflict')
            rows = db.execute('SELECT id,issue_number,title FROM issues WHERE volume_id=? AND id IN '
                              '(SELECT value FROM json_each(?))',
                              (volume_id, json.dumps(ids))).fetchall()
            if len(rows)!=len(ids):
                raise LocalReviewError('invalid_issue_selection')
            changes = _overrides(db, plan, ids)
            if changes and not override and index not in session.reviewed:
                raise LocalReviewError('override_required')
            _admit_selection(db, plan, ids)
            # This is the same explicit forced association stored by Manual Match,
            # without its subsequent broad scan or missing-file cleanup.
            db.execute('INSERT INTO files(filepath,size) VALUES(?,?) ON CONFLICT(filepath) DO UPDATE SET size=excluded.size',
                       (plan.source_path, plan.identification.candidate.file.size))
            file_id = db.execute('SELECT id FROM files WHERE filepath=?', (plan.source_path,)).fetchone()[0]
            db.execute('DELETE FROM volume_files WHERE file_id=?', (file_id,))
            db.execute('DELETE FROM issues_files WHERE file_id=?', (file_id,))
            db.executemany('INSERT INTO issues_files(file_id,issue_id,forced) VALUES(?,?,1)',
                           ((file_id, issue_id) for issue_id in ids))
            if _stamp(plan.source_path) != session.stamps[index]:
                raise LocalReviewError('stale_preview')
        session.reviewed[index] = ids
        session.presentation[index] = {**session.presentation[index], 'status':'associated',
            'issue_ids':list(ids), 'issue_labels':['#'+r[1]+(' '+r[2] if r[2] else '') for r in rows],
            'identification_reasons':[], 'review_available':False}
        return dict(id=identifier, plans=session.presentation, enumeration_complete=True)


def apply_associations(database, identifier):
    """One guarded DB-only transaction; no archive IO, quality scan or jobs."""
    from backend.base.organization_plan import PlanStatus
    with _lock, closing(sqlite3.connect(database, timeout=30)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        retained = _sessions.get(identifier)
        if retained is None or retained.volume_id is None:
            raise LocalReviewError('stale_preview')
        session = _session(database, retained.volume_id, identifier, 0) if retained.batch.plans else retained
        outcomes, completed = [], []
        with scan_mutation(db.cursor(), session.volume_id, prune_unmatched=False):
            for index, original in enumerate(session.batch.plans):
                if original.status not in (PlanStatus.READY, PlanStatus.NO_CHANGES) or index in session.reviewed or index not in session.stamps:
                    continue
                plan = _fresh(db, session, index)
                require_unreserved(db.cursor(), plan.source_path)
                ids = tuple(sorted(plan.identification.selected.local_issue_ids))
                if _blocked(plan, session.volume_id) or not ids:
                    raise LocalReviewError('publication_or_file_conflict')
                _admit_selection(db, plan, ids)
                existing = plan.identification.candidate.existing
                if existing and any(a.issue_id is None for a in existing.associations):
                    raise LocalReviewError('publication_or_file_conflict')
                previous = tuple(sorted(a.issue_id for a in existing.associations)) if existing else ()
                if previous and previous != ids:
                    raise LocalReviewError('publication_or_file_conflict')
                if not previous:
                    db.execute('INSERT INTO files(filepath,size) VALUES(?,?) ON CONFLICT(filepath) DO UPDATE SET size=excluded.size',
                        (plan.source_path, plan.identification.candidate.file.size))
                    fid = db.execute('SELECT id FROM files WHERE filepath=?',(plan.source_path,)).fetchone()[0]
                    db.executemany('INSERT OR IGNORE INTO issues_files(file_id,issue_id,forced) VALUES(?,?,0)',((fid,i) for i in ids))
                if _stamp(plan.source_path) != session.stamps[index]:
                    raise LocalReviewError('stale_preview')
                completed.append((index,ids,bool(existing and all(a.forced for a in existing.associations))))
        for index, ids, forced in completed:
            session.reviewed[index] = ids
            session.association_forced[index] = forced
            session.presentation[index] = {**session.presentation[index], 'status':'associated','review_available':False}
        for index in session.reviewed:
            if session.batch.plans[index].status in (PlanStatus.READY, PlanStatus.NO_CHANGES):
                outcomes.append(dict(source=session.batch.plans[index].source_path,job_id=None,state='completed'))
        return dict(id=identifier,jobs=outcomes,replay=not completed,
            review=[p for i,p in enumerate(session.presentation) if i not in session.reviewed])


def _embedded_issues(db, plan):
    ids = set()
    for claim in plan.identification.candidate.claims:
        ref = claim.reference
        if ref.kind.value == 'issue':
            ids.update(r[0] for r in db.execute('SELECT issue_id FROM issue_external_ids WHERE provider=? AND provider_id=?',
                                              (ref.provider,ref.provider_id)))
    return sorted(ids)


def _overrides(db, plan, ids):
    existing = plan.identification.candidate.existing
    previous = {a.issue_id for a in existing.associations if a.issue_id is not None} if existing else set()
    embedded = set(_embedded_issues(db, plan))
    return bool(previous and previous != set(ids) or embedded and embedded != set(ids))

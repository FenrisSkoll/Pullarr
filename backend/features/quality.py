"""Application-owned quality analysis on TaskHandler; no acquisition side effects."""

import json
from pathlib import Path
from threading import RLock
from time import time
from uuid import uuid4

from flask import current_app, has_app_context

from backend.base.definitions import Task
from backend.base.quality import ClaimedQuality, QualityError, integer
from backend.implementations.file_quality import analyze
from backend.internals.collections import transaction
from backend.internals.db import get_db
from backend.internals.quality import QualityStore, canonical


def search_quality_context(volume_id, issue_id, cursor=None):
    """Snapshot current policy into the existing scorer, never replace it."""
    if cursor is None and not has_app_context():
        return ''
    c = cursor if cursor is not None else get_db()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='quality_profiles'").fetchone():
        return ''
    store = QualityStore(c)
    effective = store.effective([volume_id])[volume_id]
    profile = effective['profile'] or store.profile(store.default()['profile_id'])
    current, upgrade = None, False
    if issue_id is not None:
        state = store.issue_states([issue_id])
        if state and state[0]['direct_owned']:
            current = state[0]['files'][0]['claims']
            upgrade = state[0]['upgrade_eligible']
    return canonical(dict(profile=profile, current=current, upgrade=upgrade, conflict=effective['conflict']))


class QualityAnalysisTask(Task):
    action = 'quality_analysis'
    display_title = 'Analyze file quality'
    volume_id = None
    issue_id = None

    def __init__(self, owner, identifier):
        self.owner, self.identifier = owner, identifier
        self.message = 'Queued quality analysis'
        self._stop = False

    @property
    def stop(self):
        return self._stop

    @stop.setter
    def stop(self, value):
        self._stop = value
        if value:
            with self.owner.lock:
                item = self.owner.handles.get(self.identifier)
                if item and item['state'] == 'queued':
                    item['state'] = 'cancelled'

    def run(self):
        self.owner.execute(self)


class QualityAnalysis:
    def __init__(self, *, enqueue=None, clock=time):
        self.enqueue, self.clock = enqueue, clock
        self.lock = RLock()
        self.handles = {}

    def submit(self, file_ids):
        if not isinstance(file_ids, list) or not 1 <= len(file_ids) <= 50 or len(set(file_ids)) != len(file_ids):
            raise QualityError('bounded')
        for fid in file_ids:
            integer(fid, 1)
        c = get_db()
        count = c.execute('SELECT COUNT(*) FROM active_files WHERE id IN (SELECT value FROM json_each(?))', (canonical(file_ids),)).fetchone()[0]
        if count != len(file_ids):
            raise QualityError('not_found')
        with self.lock:
            for key, item in tuple(self.handles.items()):
                if item['expires'] < self.clock() and item['state'] not in ('queued', 'running'):
                    del self.handles[key]
                elif item['file_ids'] == sorted(file_ids) and item['state'] in ('queued', 'running'):
                    return self.status(key)
            if len(self.handles) >= 16:
                raise QualityError('capacity')
            identifier = uuid4().hex
            self.handles[identifier] = dict(id=identifier, file_ids=sorted(file_ids), state='queued',
                completed=0, total=len(file_ids), results=[], expires=self.clock()+1800)
            task = QualityAnalysisTask(self, identifier)
            if self.enqueue:
                self.enqueue(task)
            else:
                from backend.features.tasks import TaskHandler
                TaskHandler().add(task)
            return self.status(identifier)

    def status(self, identifier):
        with self.lock:
            item = self.handles.get(identifier)
            if item is None or item['expires'] < self.clock() and item['state'] not in ('queued', 'running'):
                raise QualityError('task_expired')
            return json.loads(canonical({k:v for k,v in item.items() if k not in ('expires', 'file_ids')}))

    def execute(self, task):
        item = self.handles[task.identifier]
        item['state'] = 'running'
        c = get_db()
        for fid in item['file_ids']:
            if task.stop:
                item['state'] = 'cancelled'
                return
            try:
                row = c.execute('SELECT filepath FROM active_files WHERE id=?', (fid,)).fetchone()
                if row is None:
                    raise QualityError('file_changed')
                path = row[0]
                roots = [Path(r[0]) for r in c.execute('SELECT folder FROM root_folders').fetchall()]
                if not any(root in Path(path).parents for root in roots):
                    raise QualityError('unsafe_file')
                facts = analyze(path, cancelled=lambda: task.stop,
                    progress=lambda done,total: setattr(task, 'message', f'Analyzing file {item["completed"]+1}/{item["total"]}: page {done}/{total}'))
                with transaction(c, write=True):
                    latest = c.execute('SELECT filepath FROM active_files WHERE id=?', (fid,)).fetchone()
                    if latest is None or latest[0] != path:
                        raise QualityError('file_changed')
                    assessment = QualityStore(c).assessment(fid, facts)
                    # Baseline is explicitly today's observation, not a fictional grab.
                    if not c.execute('SELECT 1 FROM acquisition_provenance WHERE file_id=?', (fid,)).fetchone():
                        links = c.execute('SELECT i.id,i.volume_id FROM issues_files f JOIN issues i ON i.id=f.issue_id WHERE f.file_id=?', (fid,)).fetchall()
                        c.execute('''INSERT INTO acquisition_provenance
                            (id,issue_id,volume_id,reason,state,release_title,source,claims,profile_snapshot,decision,file_id,assessment_id,created_at,updated_at)
                            VALUES(?,?,?,'legacy','imported','','unknown',?,'{}','{}',?,?,?,?)''',
                            (uuid4().hex, links[0][0] if len(links)==1 else None, links[0][1] if links else None,
                             canonical(ClaimedQuality(origin='legacy_unknown').preview()), fid, assessment, self.clock(), self.clock()))
                item['results'].append(dict(file_id=fid, assessment_id=assessment, state='complete'))
            except QualityError as error:
                if str(error) == 'cancelled':
                    item['state'] = 'cancelled'
                    return
                item['results'].append(dict(file_id=fid, state='failed', reason=str(error)))
            except Exception:
                item['results'].append(dict(file_id=fid, state='failed', reason='analysis_failed'))
            item['completed'] += 1
        item['state'] = 'partial' if any(r['state'] == 'failed' for r in item['results']) else 'complete'


def record_import(executor, job, intent):
    """Only genuinely new file registration, never rename/move/metadata tagging."""
    db = executor.store.db
    if intent.get('inverse') or intent.get('database_before', {}).get('file') is not None:
        return
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_provenance'").fetchone():
        return
    file = db.execute('SELECT id FROM active_files WHERE filepath=?', (intent['target'],)).fetchone()
    if file is None or db.execute("SELECT 1 FROM acquisition_provenance WHERE file_id=? AND state='imported'", (file[0],)).fetchone():
        return
    link = db.execute('''SELECT p.id FROM acquisition_artifacts a JOIN acquisition_intakes i ON i.id=a.intake_id
        JOIN acquisition_provenance p ON p.client_kind=i.kind AND p.client_job=i.download_id
        WHERE a.organization_job_id=? LIMIT 1''', (job.id,)).fetchone()
    assessment = None
    try:
        facts = analyze(intent['target'])
        assessment = QualityStore(db.cursor()).assessment(file[0], facts)
    except QualityError:
        # Migration-safe ordinary imports can have unsupported raster evidence.
        # Unknown facts never pass the separate mandatory upgrade gate.
        pass
    with executor.store.transaction():
        if link:
            parent = QualityStore(db.cursor()).detail(link[0])
            if parent['issue_id'] is None:
                issues = [r[0] for r in db.execute('SELECT issue_id FROM issues_files WHERE file_id=?', (file[0],))]
                decision = dict(parent['decision'], acquisition_request=parent['id'], issue_ids=issues)
                db.execute('''INSERT INTO acquisition_provenance
                    (id,volume_id,issue_id,reason,state,release_title,source,claims,profile_id,profile_revision,
                     profile_snapshot,decision,file_id,assessment_id,created_at,updated_at)
                    VALUES(?,?,?,?,'imported',?,?,?,?,?,?,?,?,?,?,?)''',
                    (uuid4().hex,parent['volume_id'],issues[0] if len(issues)==1 else None,parent['reason'],
                     parent['release_title'],parent['source'],canonical(parent['claims']),parent['profile_id'],parent['profile_revision'],
                     canonical(parent['profile_snapshot']),canonical(decision),file[0],assessment,time(),time()))
                # A pack is not complete merely because its first file imported.
                db.execute("UPDATE acquisition_provenance SET state='verifying',updated_at=? WHERE id=?", (time(),parent['id']))
            else:
                db.execute("UPDATE acquisition_provenance SET state='imported',file_id=?,assessment_id=?,updated_at=? WHERE id=?",
                           (file[0],assessment,time(),link[0]))
        else:
            issues = [r[0] for r in db.execute('SELECT issue_id FROM issues_files WHERE file_id=?', (file[0],))]
            db.execute('''INSERT INTO acquisition_provenance
                (id,volume_id,issue_id,reason,state,release_title,source,claims,profile_snapshot,decision,file_id,assessment_id,created_at,updated_at)
                VALUES(?,?,?,'manual_import','imported',?,'unknown',?,'{}','{}',?,?,?,?)''',
                (uuid4().hex,intent['volume_id'],issues[0] if len(issues)==1 else None,Path(intent['source']).name[:1000],
                 canonical(ClaimedQuality(origin='manual_import_unknown').preview()),file[0],assessment,time(),time()))


def record_manual_imports(volume_id, paths, existing_ids):
    """Legacy Library Import admission, not organizer rename/move or rescan."""
    if not has_app_context() or 'quality_analysis' not in current_app.extensions:
        return
    c=get_db()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_provenance'").fetchone():
        return
    for offset in range(0,len(paths),100):
        files=c.execute('''SELECT f.id,f.filepath FROM active_files f
            WHERE f.filepath IN (SELECT value FROM json_each(?))''',(canonical(paths[offset:offset+100]),)).fetchall()
        with transaction(c,write=True):
            for fid,path in files:
                if fid in existing_ids or c.execute('SELECT 1 FROM acquisition_provenance WHERE file_id=?',(fid,)).fetchone():
                    continue
                links=c.execute('SELECT issue_id FROM issues_files WHERE file_id=?',(fid,)).fetchall()
                c.execute('''INSERT INTO acquisition_provenance
                    (id,volume_id,issue_id,reason,state,release_title,source,claims,profile_snapshot,decision,file_id,created_at,updated_at)
                    VALUES(?,?,?,'manual_import','imported',?,'unknown',?,'{}','{}',?,?,?)''',
                    (uuid4().hex,volume_id,links[0][0] if len(links)==1 else None,Path(path).name[:1000],
                     canonical(ClaimedQuality(origin='manual_import_unknown').preview()),fid,time(),time()))


def import_baseline(paths):
    if not has_app_context() or 'quality_analysis' not in current_app.extensions:
        return set()
    result=set()
    for offset in range(0,len(paths),100):
        result.update(r[0] for r in get_db().execute('SELECT id FROM files WHERE filepath IN (SELECT value FROM json_each(?))',
                                                   (canonical(paths[offset:offset+100]),)))
    return result

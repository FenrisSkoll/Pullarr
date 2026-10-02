"""Explicit bounded archive reviews. TaskHandler queues; OrganizationJobs mutate."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event, RLock
from time import monotonic
from uuid import uuid4
from zipfile import BadZipFile

import rarfile

from backend.base.acquisition_intake import IntakeFailure
from backend.base.comicinfo import ComicInfoError
from backend.base.definitions import Task
from backend.base.organization_job import OrganizationError
from backend.base.quality import QualityError
from backend.features.organization_archive import (register_archive, review,
                                                   sibling_names,
                                                   target_registered)
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.archive_normalization import (ArchiveFailure,
                                                           inspect)
from backend.internals.organization_jobs import canonical, digest

MAX_SCAN = 1000
MAX_BATCH = 100
MAX_HANDLES = 16
TTL = 1800


class ArchiveTask(Task):
    action = 'maintenance_archive'
    display_title = 'Archive maintenance'
    volume_id = None
    issue_id = None

    def __init__(self, owner, identifier, operation, payload):
        self.owner, self.identifier, self.operation, self.payload = owner, identifier, operation, payload
        self.cancelled = Event()
        self.message = 'Queued archive maintenance'

    @property
    def stop(self):
        return self.cancelled.is_set()

    @stop.setter
    def stop(self, value):
        if value:
            self.cancelled.set()

    def run(self):
        self.owner.run(self)


class ArchiveMaintenance:
    def __init__(self, database, *, enqueue=None, clock=monotonic):
        self.database, self.enqueue, self.clock = database, enqueue, clock
        self.lock, self.handles = RLock(), {}

    def connection(self):
        db = sqlite3.connect(Path(self.database).absolute().as_uri()+'?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        return closing(db)

    def files(self, *, volume_id=None, issue_id=None, after=0, limit=50):
        with self.connection() as db:
            rows = db.execute('''SELECT f.id,f.filepath,f.size,v.title,v.id volume_id FROM active_files f
                JOIN volumes v ON v.id=(SELECT MIN(volume_id) FROM (
                  SELECT i.volume_id FROM issues_files b JOIN issues i ON i.id=b.issue_id WHERE b.file_id=f.id
                  UNION SELECT volume_id FROM volume_files WHERE file_id=f.id))
                WHERE f.id>? AND (? IS NULL OR v.id=?)
                AND (? IS NULL OR EXISTS(SELECT 1 FROM issues_files WHERE file_id=f.id AND issue_id=?))
                ORDER BY f.id LIMIT ?''', (after, volume_id, volume_id, issue_id, issue_id, limit+1)).fetchall()
        return dict(items=[dict(file_id=r['id'], filename=Path(r['filepath']).name, size=r['size'],
                          title=r['title'], volume_id=r['volume_id']) for r in rows[:limit]],
                    next_after=rows[limit-1]['id'] if len(rows)>limit else None)

    def executor(self):
        with self.connection() as db:
            roots = tuple(r[0] for r in db.execute('SELECT folder FROM root_folders ORDER BY id LIMIT 1001'))
        if not 1 <= len(roots) <= 1000:
            raise ArchiveFailure('configured_root_required')
        return OrganizationExecutor(self.database, roots)

    def _expire(self):
        for key in tuple(self.handles):
            value = self.handles[key]
            if value['expires'] <= self.clock() and value['state'] not in ('queued', 'running'):
                del self.handles[key]

    def submit(self, operation, payload):
        if operation not in ('scan', 'preview', 'apply'):
            raise ArchiveFailure('invalid_request')
        with self.lock:
            self._expire()
            active = next((k for k, v in self.handles.items() if v['state'] in ('queued','running')), None)
            if active:
                raise ArchiveFailure('archive_task_active')
            if len(self.handles) >= MAX_HANDLES:
                raise ArchiveFailure('capacity')
            identifier = uuid4().hex
            self.handles[identifier] = dict(state='queued', operation=operation, items=[], done=0,
                expires=self.clock()+TTL, reason=None)
            task = ArchiveTask(self, identifier, operation, payload)
            self.handles[identifier]['task'] = task
        try:
            if self.enqueue is None:
                from backend.features.tasks import TaskHandler
                TaskHandler().add(task)
            else:
                self.enqueue(task)
        except Exception:
            with self.lock:
                self.handles.pop(identifier, None)
            raise ArchiveFailure('task_unavailable') from None
        return dict(id=identifier, state='queued')

    def status(self, identifier, *, offset=0, limit=50, status='all'):
        with self.lock:
            self._expire()
            value = self.handles.get(identifier)
            if value is None:
                raise ArchiveFailure('review_expired')
            items = value['items']
            if status != 'all':
                items = [r for r in items if r.get('status') == status or status == 'shared' and r.get('shared_source')]
            return dict(id=identifier, state=value['state'], operation=value['operation'], reason=value['reason'],
                done=value['done'], total=len(items), items=items[offset:offset+limit],
                next_offset=offset+limit if len(items)>offset+limit else None)

    def cancel(self, identifier):
        with self.lock:
            if identifier not in self.handles:
                raise ArchiveFailure('review_expired')
            value = self.handles[identifier]
            value['task'].stop = True
            if value['state'] == 'queued':
                value['state'] = 'cancelled'
        return dict(state='cancellation_requested')

    def _preview(self, executor, fid, cancelled):
        authority, confirmation = review(executor, fid)
        path = authority['ownership']['file']['filepath']
        facts = inspect(path, cancelled=cancelled)
        if facts['old'] != authority['old']:
            raise ArchiveFailure('stale_preview')
        if Path(path).suffix.casefold() not in ('.cbr', '.rar', '.cbz', '.zip'):
            raise ArchiveFailure('unsupported_container')
        target = Path(path).with_suffix('.cbz')
        siblings = sibling_names(path)
        collision = (target != Path(path) and any(name.casefold() == target.name.casefold() for name in siblings)
                     or target_registered(executor.store.db, fid, str(target)))
        workspace = any(name.startswith('.pullarr-archive-') for name in siblings)
        value = dict(file_id=fid, filename=Path(path).name, target=target.name,
            container=facts['container'], size=authority['old']['size'], pages=facts['pages'],
            metadata=facts['metadata'], shared_source=authority['sharing']['shared'],
            seeded_source=bool(authority['sharing']['seeds']), status='blocked' if collision or workspace else facts['status'],
            reason='archive_workspace_review_required' if workspace else 'target_occupied' if collision else None, confirmation=confirmation,
            operation='archive-convert-cbr-cbz/v1' if facts['container']=='cbr' else 'archive-repack-cbz/v1',
            page_payloads='preserve_exact_bytes', metadata_action='preserve_bytes',
            order='preserve_source_member_order', source_retention='shared_source_unchanged',
            apply_available=not collision and not workspace)
        return value

    def run(self, task):
        with self.lock:
            value = self.handles[task.identifier]
            if value['state'] != 'queued':
                return
            value['state'] = 'running'
        executor = None
        try:
            executor = self.executor()
            selected = task.payload['selected']
            if not 1 <= len(selected) <= (MAX_SCAN if task.operation=='scan' else MAX_BATCH):
                raise ArchiveFailure('selection_bound')
            for selection in selected:
                if task.stop:
                    raise ArchiveFailure('cancelled')
                fid = selection if task.operation!='apply' else selection['file_id']
                recorded = executor.store.db.execute('SELECT filepath FROM active_files WHERE id=?',(fid,)).fetchone()
                filename = Path(recorded[0]).name if recorded else 'Unavailable file'
                try:
                    if task.operation == 'apply':
                        identifier = register_archive(executor, fid, selection['confirmation'],
                            'archive:'+task.payload['review_id']+':'+str(fid), cancelled=task.cancelled.is_set)
                        # Cancellation after registration leaves a recoverable pending job.
                        job = executor.store.get(identifier) if task.stop else executor.apply_job(identifier)
                        result = dict(file_id=fid, job_id=identifier, status=job.state.value,
                                      reason=job.error, history_domain='organization')
                    else:
                        result = self._preview(executor, fid, task.cancelled.is_set)
                except (ArchiveFailure, IntakeFailure, OrganizationError, ComicInfoError, QualityError, rarfile.Error, BadZipFile, OSError, ValueError, SyntaxError) as error:
                    result = dict(file_id=fid, status='review_required', apply_available=False,
                        reason=str(error) if isinstance(error, ArchiveFailure) else 'archive_unreadable_or_unsafe')
                with self.lock:
                    result.setdefault('filename',filename)
                    value['items'].append(result)
                    value['done'] += 1
            if task.operation == 'preview':
                value['confirmation'] = digest(canonical(value['items']))
            value['state'] = 'cancelled' if task.stop else 'complete'
        except ArchiveFailure as error:
            value['state'], value['reason'] = 'cancelled' if task.stop else 'failed', str(error)
        except Exception:
            from backend.base.logging import LOGGER
            LOGGER.error('Archive maintenance task failed unexpectedly')
            value['state'], value['reason'] = 'failed', 'internal_error'
        finally:
            if executor:
                executor.close()

    def apply(self, identifier, selected):
        with self.lock:
            self._expire()
            previous = self.handles.get(identifier)
            if previous is None or previous['state'] != 'complete' or previous['operation'] != 'preview':
                raise ArchiveFailure('review_expired')
            allowed = {r['file_id']: r for r in previous['items'] if r.get('apply_available')}
            if not selected or len(selected)>MAX_BATCH or any(fid not in allowed for fid in selected):
                raise ArchiveFailure('invalid_selection')
            return self.submit('apply', dict(review_id=identifier, selected=[
                dict(file_id=fid, confirmation=allowed[fid]['confirmation']) for fid in selected]))

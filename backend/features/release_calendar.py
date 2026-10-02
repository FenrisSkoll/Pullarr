"""One application-owned Calendar coordinator, using the existing TaskHandler."""

import json
from asyncio import run
from threading import RLock
from time import time
from uuid import uuid4

from backend.base.definitions import Task
from backend.base.release_calendar import MAX_SYNC_SUBJECTS, CalendarError
from backend.implementations.metadata.release_dates import acquire_dates
from backend.internals.collections import transaction
from backend.internals.db import get_db
from backend.internals.release_calendar import CalendarStore


class CalendarTask(Task):
    action = 'calendar_sync'
    display_title = 'Refresh release Calendar'
    volume_id = None
    issue_id = None

    def __init__(self, owner, identity, provider):
        self.owner, self.identity, self.provider = owner, identity, provider
        self._stop = False
        self.message = 'Queued Calendar observation'

    @property
    def stop(self):
        return self._stop

    @stop.setter
    def stop(self, value):
        self._stop = value
        if value:
            # TaskHandler removes queued tasks without calling run(). Persist
            # that terminal state now, rather than retaining a permanent guard.
            self.owner.cancel_queued(self.identity)

    def run(self):
        self.owner.execute(self)


class ReleaseCalendar:
    def __init__(self, *, enqueue=None, acquire=acquire_dates, clock=time):
        self.enqueue, self.acquire, self.clock = enqueue, acquire, clock
        self.lock = RLock()
        self.active: set[str] = set()

    def submit(self, provider='all'):
        if provider not in ('all', 'comicvine', 'metron', 'gcd'):
            raise CalendarError('invalid_request')
        with self.lock:
            if self.active:
                raise CalendarError('sync_active')
            identity = uuid4().hex
            cursor = get_db()
            with transaction(cursor, True):
                cursor.execute('INSERT INTO calendar_sync_state(id,started_at,state) VALUES(?,?,?)', (identity, self.clock(), 'queued'))
            self.active.add(identity)
            task = CalendarTask(self, identity, provider)
            try:
                if self.enqueue is None:
                    from backend.features.tasks import TaskHandler
                    TaskHandler().add(task)
                else:
                    self.enqueue(task)
            except Exception:
                self.active.discard(identity)
                with transaction(cursor, True):
                    cursor.execute("UPDATE calendar_sync_state SET state='interrupted',completed_at=? WHERE id=?", (self.clock(), identity))
                raise CalendarError('task_unavailable') from None
            return dict(id=identity, state='queued')

    def status(self, identity):
        value = CalendarStore(get_db()).sync_status(identity)
        with self.lock:
            if value['state'] in ('queued', 'running') and identity not in self.active:
                value.update(state='interrupted', reason='application_restarted')
        return value

    def cancel_queued(self, identity):
        try:
            cursor = get_db()
        except RuntimeError:
            # Process shutdown can signal Task.stop outside Flask context.
            # The worker observes the flag; a restart projects unfinished
            # receipts as interrupted rather than attempting an unsafe write.
            return
        with self.lock, transaction(cursor, True):
            cursor.execute("UPDATE calendar_sync_state SET state='cancelled',completed_at=? WHERE id=? AND state='queued'",
                (self.clock(), identity))
            if cursor.rowcount:
                self.active.discard(identity)

    def execute(self, task):
        cursor, receipts = get_db(), []
        store = CalendarStore(cursor, clock=self.clock)
        state = 'cancelled' if task.stop else 'complete'
        try:
            if task.stop:
                return
            subjects = [s for s in store.subjects() if task.provider in ('all', s['provider'])]
            bounded = len(subjects) > MAX_SYNC_SUBJECTS
            # Fail closed before provider IO; no silent first-50-only refresh.
            if bounded:
                raise CalendarError('bounded')
            with transaction(cursor, True):
                cursor.execute("UPDATE calendar_sync_state SET state='running',total=? WHERE id=?", (len(subjects), task.identity))
            for subject in subjects:
                if task.stop:
                    state = 'cancelled'
                    break
                task.message = 'Observing known ' + subject['provider'] + ' publication'
                try:
                    acquired = run(self.acquire(subject['provider'], subject['provider_id'], self.clock()))
                    store.persist(subject, acquired)
                    receipt = dict(provider=subject['provider'], state='complete')
                except Exception as error:
                    # Controlled category only, no raw provider response/URL/exception text.
                    reason = getattr(error, 'reason', None)
                    if isinstance(error, CalendarError):
                        reason = str(error)
                    if reason not in ('credentials', 'disabled', 'rate_limited', 'budget', 'bounded', 'stale_authority', 'unsupported_provider'):
                        reason = 'provider_unavailable'
                    receipt = dict(provider=subject['provider'], state='failed', reason=reason)
                    state = 'partial'
                receipts.append(receipt)
                with transaction(cursor, True):
                    cursor.execute('UPDATE calendar_sync_state SET processed=?,providers=? WHERE id=?', (len(receipts), json.dumps(receipts), task.identity))
        except CalendarError as error:
            state = 'bounded' if str(error) == 'bounded' else 'partial'
        except Exception:
            state = 'partial'
            receipts.append(dict(state='failed', reason='observation_unavailable'))
        finally:
            with transaction(cursor, True):
                cursor.execute('UPDATE calendar_sync_state SET state=?,completed_at=?,providers=? WHERE id=?', (state, self.clock(), json.dumps(receipts), task.identity))
            with self.lock:
                self.active.discard(task.identity)

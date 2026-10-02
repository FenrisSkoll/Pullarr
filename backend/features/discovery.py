"""Application-owned observation tasks. Polling never invokes acquisition."""

import json
from threading import RLock
from time import time
from uuid import uuid4

from backend.base.definitions import Task
from backend.base.discovery import (FEED, MAX_PAGES, ORIGIN, DiscoveryError,
                                    canonical, parse_feed, parse_listing)
from backend.features.discovery_matching import project
from backend.implementations.discovery_source import DiscoveryHTTP
from backend.internals.db import get_db
from backend.internals.discovery import DiscoveryStore


class DiscoveryTask(Task):
    action = 'discover_refresh'
    display_title = 'Refresh Discover observations'
    volume_id = None
    issue_id = None

    def __init__(self, owner, identifier):
        self.owner, self.identifier = owner, identifier
        self.message, self.stop = 'Queued Discover work', False

    def run(self):
        self.owner.execute(self)


class Discover:
    def __init__(self, *, transport=None, enqueue=None, clock=time):
        self.transport = transport or DiscoveryHTTP()
        self.enqueue, self.clock = enqueue, clock
        self.lock, self.handles = RLock(), {}
        self.reviews = {}

    def tick(self):
        row = get_db().execute("SELECT enabled,automatic,next_poll FROM discovery_sources WHERE key='getcomics'").fetchone()
        if row and row[0] and row[1] and row[2] <= self.clock():
            try:
                self.submit()
            except DiscoveryError as error:
                # Optional observation work must not prevent the shared runtime
                # from running ordinary Wanted acquisition on this cadence.
                if str(error) not in ('capacity', 'source_disabled', 'rate_limited'):
                    raise

    def submit(self, *, operation='poll', post_id=None, confirmation=None):
        with self.lock:
            for key, item in tuple(self.handles.items()):
                if item['expires'] < self.clock() and item['state'] not in ('queued', 'running'):
                    del self.handles[key]
                elif item['operation'] == operation and item['post_id'] == post_id and item['state'] in ('queued', 'running'):
                    return self.status(key)
            if len(self.handles) >= 16:
                raise DiscoveryError('capacity')
            if operation == 'poll':
                source = get_db().execute("SELECT enabled,next_poll,error FROM discovery_sources WHERE key='getcomics'").fetchone()
                if not source[0]:
                    raise DiscoveryError('source_disabled')
                if source[2] == 'rate_limited' and source[1] > self.clock():
                    raise DiscoveryError('rate_limited')
            elif operation not in ('preview', 'acquire') or post_id is None:
                raise DiscoveryError('invalid_request')
            identifier = uuid4().hex
            self.handles[identifier] = dict(id=identifier, operation=operation, post_id=post_id, confirmation=confirmation,
                state='queued', result=None, expires=self.clock()+1800)
            task = DiscoveryTask(self, identifier)
            if self.enqueue:
                self.enqueue(task)
            else:
                from backend.features.tasks import TaskHandler
                TaskHandler().add(task)
            return self.status(identifier)

    def status(self, identifier):
        with self.lock:
            item = self.handles.get(identifier)
            if not item or item['expires'] < self.clock() and item['state'] not in ('queued', 'running'):
                raise DiscoveryError('task_expired')
            return json.loads(canonical({k: v for k, v in item.items() if k not in ('expires', 'confirmation')}))

    def execute(self, task):
        item = self.handles[task.identifier]
        if task.stop:
            item['state'] = 'cancelled'
            return
        item['state'] = 'running'
        try:
            if item['operation'] == 'poll':
                item['result'] = self.poll(cancelled=lambda: task.stop)
            else:
                from backend.features.discovery_acquisition import acquisition
                item['result'] = acquisition(self, item['post_id'], item['confirmation'] if item['operation'] == 'acquire' else None)
            item['state'] = 'complete'
        except DiscoveryError as error:
            item.update(state='failed', result=dict(reason=str(error)))
        except Exception:
            from backend.base.logging import LOGGER
            LOGGER.error('Discover task failed unexpectedly')
            item.update(state='failed', result=dict(reason='internal_error'))

    def poll(self, cancelled=lambda: False):
        store = DiscoveryStore(get_db(), self.clock)
        source = dict(get_db().execute("SELECT * FROM discovery_sources WHERE key='getcomics'").fetchone())
        existing = bool(store.status()['retained'])
        values, fallback, validators, pages = [], None, None, 0
        try:
            try:
                response = self.transport.get(FEED, source['etag'], source['last_modified'])
                if response['unchanged']:
                    receipt = dict(status='unchanged', observed=0, new=0, changed=0, duplicates=0, pages=0, gap=bool(source['gap']))
                    store.receipt(receipt, transport=source['transport'], gap=bool(source['gap']))
                    return receipt
                values = parse_feed(response['data'])
                validators = response
            except DiscoveryError as error:
                # Never retry a rate-limit or access policy via another surface.
                if str(error) not in ('invalid_feed', 'parser_contract_changed', 'source_unavailable', 'invalid_content_type'):
                    raise
                fallback = str(error)
            overlap = store.known(values) if existing and values else False
            needs_pages = fallback is not None or (existing and not overlap)
            if needs_pages:
                for page in range(1, MAX_PAGES+1):
                    if cancelled():
                        raise DiscoveryError('cancelled')
                    response = self.transport.get(ORIGIN+'/' if page == 1 else ORIGIN+f'/page/{page}/')
                    batch = parse_listing(response['data'])
                    pages += 1
                    overlap = overlap or (existing and store.known(batch))
                    values.extend(batch)
                    if overlap or not existing:
                        break
            if cancelled():
                raise DiscoveryError('cancelled')
            gap = bool(existing and not overlap)
            counts = store.ingest(values)
            receipt = dict(status='possible_gap' if gap else 'observed', **counts, pages=pages,
                gap=gap, fallback_reason=fallback, coverage='bounded_recent_window')
            store.receipt(receipt, transport='html' if fallback else 'feed', validators=validators, gap=gap)
            return receipt
        except DiscoveryError as error:
            # Keep successfully parsed observations, even if catch-up then failed.
            counts = store.ingest(values) if values else dict(observed=0,new=0,changed=0,duplicates=0)
            receipt = dict(status='failed', reason=str(error), **counts, pages=pages, gap=bool(existing or source['gap']))
            store.receipt(receipt, error=str(error), gap=receipt['gap'], retry_after=error.retry_after)
            raise

    def page(self, *, state='all', quality='', **filters):
        from backend.base.quality import CLASSES
        if state not in ('all','missing','upgrade','satisfied','in_library','unmatched','ambiguous','bundle','non_release','blocked') or quality and quality not in CLASSES:
            raise DiscoveryError('invalid_request')
        result = DiscoveryStore(get_db()).page(**filters)
        result['items'] = project(get_db(), result['items'])
        result['scanned'] = len(result['items'])
        # Cursor represents the scanned source window, not a false full-match count.
        result['items'] = [p for p in result['items'] if (state == 'all' or state == p['interest'] or state == p['match']
            or state == 'in_library' and p['match'] == 'matched') and (not quality or p['claims']['quality_class'] == quality)]
        return result

    def detail(self, identifier):
        post = project(get_db(), [DiscoveryStore(get_db()).post(identifier)])[0]
        row = get_db().execute('SELECT * FROM discovery_details WHERE post_id=?', (identifier,)).fetchone()
        post['detail'] = dict(fetched_at=row['fetched_at'], stale=row['revision'] != post['revision'], facts=json.loads(row['facts'])) if row else None
        return post

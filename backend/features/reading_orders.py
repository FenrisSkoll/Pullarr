"""One application-owned review/task coordinator; no new acquisition engine."""

import json
from copy import deepcopy
from threading import RLock
from time import time
from uuid import uuid4

from backend.base.collections import integer, reference, text
from backend.base.definitions import MonitorScheme, Task
from backend.base.reading_orders import (ReadingOrderError, digest,
                                         page_bounds, parse_cbl)
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.reading_order_sources import (CBLFetcher,
                                                           MetronLists)
from backend.internals.collections import rows
from backend.internals.db import get_db
from backend.internals.reading_orders import (PROJECTION, ReadingOrderStore,
                                              transaction)


class ReadingOrderTask(Task):
    action = 'reading_order_source'
    display_title = 'Reading Order source review'
    volume_id = None
    issue_id = None

    def __init__(self, owner, identifier):
        self.owner, self.identifier = owner, identifier
        self.message = 'Queued Reading Order operation'
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


class ReadingOrders:
    def __init__(self, *, fetcher=None, provider=None, enqueue=None, clock=time):
        self.fetcher = fetcher or CBLFetcher()
        self.provider = provider or MetronLists()
        self.enqueue, self.clock = enqueue, clock
        self.lock = RLock()
        self.handles: dict = {}

    def retain(self, kind, **values):
        with self.lock:
            for key, item in list(self.handles.items()):
                if item['expires'] < self.clock() and item['state'] not in ('queued', 'running'):
                    del self.handles[key]
            if len(self.handles) >= 16:
                raise ReadingOrderError('capacity')
            identifier = uuid4().hex
            item = dict(id=identifier, kind=kind, state='complete', revision=0, expires=self.clock()+1800, **values)
            self.handles[identifier] = item
            return item

    def get(self, identifier):
        item = self.handles.get(identifier)
        if item is None or item['expires'] < self.clock():
            raise ReadingOrderError('review_expired')
        return item

    def review(self, model, source=None):
        resolved = ReadingOrderStore(get_db()).match(model)
        item = self.retain('import', model=model, resolved=resolved, source=source)
        item['digest'] = digest(dict(model=model, resolved=resolved, revision=0))
        return self.delivery(item['id'])

    def upload(self, raw):
        return self.review(parse_cbl(raw))

    def delivery(self, identifier, offset=0, limit=50):
        offset, limit = page_bounds(offset, limit)
        with self.lock:
            item = self.get(identifier)
            result = {k: item[k] for k in ('id', 'kind', 'state', 'revision', 'digest', 'reason', 'result') if k in item}
            if item['kind'] == 'import':
                result.update(title=item['model']['title'], description=item['model']['description'], warnings=item['model']['warnings'],
                    items=deepcopy(item['resolved'][offset:offset+limit]), total=len(item['resolved']), offset=offset, has_next=offset+limit<len(item['resolved']))
            elif 'results' in item:
                result.update(items=deepcopy(item['results'][offset:offset+limit]), total=len(item['results']), offset=offset, has_next=offset+limit<len(item['results']))
            return deepcopy(result)

    def resolve_review(self, identifier, revision, position, issue_id):
        integer(revision, 0)
        integer(position, 0, 1999)
        with self.lock:
            item = self.get(identifier)
            if item['kind'] != 'import' or item['revision'] != revision or item.get('result') or position >= len(item['resolved']):
                raise ReadingOrderError('revision_conflict')
            ReadingOrderStore(get_db()).snapshot_issue(issue_id)
            row = item['resolved'][position]
            row.update(issue_id=issue_id, match='manual')
            item['revision'] += 1
            item['digest'] = digest(dict(model=item['model'], resolved=item['resolved'], revision=item['revision']))
            return self.delivery(identifier, position//50*50)

    def accept(self, identifier, revision, expected_digest, confirmed):
        if confirmed is not True:
            raise ReadingOrderError('confirmation_required')
        with self.lock:
            item = self.get(identifier)
            if item['kind'] != 'import' or item['revision'] != revision or item['digest'] != expected_digest:
                raise ReadingOrderError('revision_conflict')
            if item.get('result'):
                return item['result']
            store = ReadingOrderStore(get_db())
            # Re-evaluate exact links at confirmation; manual decisions remain
            # explicit and must still point at a live canonical issue.
            fresh = store.match(item['model'])
            for old, new in zip(item['resolved'], fresh):
                if old['match'] == 'manual':
                    store.snapshot_issue(old['issue_id'])
                    new.update(issue_id=old['issue_id'], match='manual')
                elif old['issue_id'] != new['issue_id'] or old['match'] != new['match']:
                    raise ReadingOrderError('stale')
            with transaction(get_db()):
                result = store.accept_model(item['model'], fresh, dict(kind='metron_list' if item['source'] else 'cbl_import',
                    version=1, digest=expected_digest, accepted_at=self.clock()))
                if item['source']:
                    result = store.attach(result['id'], result['revision'], 'metron_list', item['source'])
                    get_db().execute('UPDATE reading_order_sources SET accepted=?,digest=? WHERE order_id=?',
                        (json.dumps(item['model']), digest(item['model']), result['id']))
            item['result'] = result
            return result

    def submit(self, kind, arguments):
        if kind not in ('refresh', 'provider_search', 'provider_fetch', 'add'):
            raise ReadingOrderError('invalid_request')
        with self.lock:
            for item in self.handles.values():
                if item['kind'] == kind and item.get('arguments') == arguments and item['state'] in ('queued', 'running'):
                    return self.delivery(item['id'])
            item = self.retain(kind, arguments=arguments)
            item['state'] = 'queued'
            task = ReadingOrderTask(self, item['id'])
            try:
                if self.enqueue:
                    self.enqueue(task)
                else:
                    from backend.features.tasks import TaskHandler
                    TaskHandler().add(task)
            except Exception:
                item.update(state='failed', reason='task_unavailable')
                raise ReadingOrderError('task_unavailable') from None
            return self.delivery(item['id'])

    def refresh(self, source_id):
        source = ReadingOrderStore(get_db()).source(source_id)
        if not source['enabled']:
            raise ReadingOrderError('detach_required')
        return self.submit('refresh', dict(source_id=source_id, revision=source['revision']))

    def search(self, provider, query):
        if provider != 'metron':
            raise ReadingOrderError('unsupported_source_order')
        return self.submit('provider_search', dict(query=text(query, 200)))

    def fetch_provider(self, handle, result_id):
        with self.lock:
            item = self.get(handle)
            if item['kind'] != 'provider_search' or item['state'] != 'complete' or not any(v['id'] == result_id for v in item.get('results', [])):
                raise ReadingOrderError('invalid_request')
        reference('metron', result_id)
        return self.submit('provider_fetch', dict(identity=result_id))

    def add(self, order_id, entry_id, provider, root_id, confirmed):
        if confirmed is not True:
            raise ReadingOrderError('confirmation_required')
        integer(root_id)
        refs = rows(get_db(), '''SELECT r.* FROM reading_order_entry_refs r JOIN reading_order_entries e ON e.id=r.entry_id
            WHERE e.order_id=? AND e.id=? AND r.provider=? AND r.volume_ref IS NOT NULL''', (integer(order_id), integer(entry_id), provider))
        identities = {r['volume_ref'] for r in refs}
        if len(identities) != 1:
            raise ReadingOrderError('requires_exact_publication')
        reference(provider, next(iter(identities)))
        return self.submit('add', dict(provider=provider, identity=next(iter(identities)), root_id=root_id))

    def execute(self, task):
        with self.lock:
            item = self.handles[task.identifier]
            if task.stop:
                item['state'] = 'cancelled'
                return
            item['state'] = 'running'
        args = item['arguments']
        try:
            if item['kind'] == 'refresh':
                store = ReadingOrderStore(get_db())
                source = store.source(args['source_id'])
                acquired = self.fetcher.fetch(source['locator'], source['etag'], source['last_modified']) if source['kind'] == 'cbl_url' else self.provider.fetch(source['locator'])
                result = store.observe(source['id'], args['revision'], acquired)
            elif item['kind'] == 'provider_search':
                item['results'] = self.provider.search(args['query'])
                result = dict(count=len(item['results']))
            elif item['kind'] == 'provider_fetch':
                acquired = self.provider.fetch(args['identity'])
                result = self.review(acquired['model'], args['identity'])
            else:
                from backend.implementations.volumes import Library
                existing = [r[0] for r in get_db().execute('SELECT volume_id FROM volume_external_ids WHERE provider=? AND provider_id=?', (args['provider'], args['identity']))]
                if len(existing) > 1:
                    raise ReadingOrderError('ambiguous')
                if existing:
                    result = dict(volume_id=existing[0], already_local=True)
                else:
                    volume = Library.add_metadata(ProviderVolumeIdentity(args['provider'], args['identity']), args['root_id'],
                        monitored=False, monitor_scheme=MonitorScheme.NONE, monitor_new_issues=False, auto_search=False)
                    result = dict(volume_id=volume)
            with self.lock:
                item.update(state='complete', result=result)
        except Exception as error:
            reason = str(error) if isinstance(error, ReadingOrderError) else getattr(error, 'reason', 'source_unavailable')
            if reason not in ('bounded', 'unsafe_xml', 'invalid_xml', 'unsupported_cbl', 'blocked_destination', 'source_timeout',
                'source_http_error', 'source_unavailable', 'redirect_limit', 'unsupported_encoding', 'source_busy', 'revision_conflict',
                'unsupported_source_order', 'credentials', 'rate_limited', 'budget', 'ambiguous'):
                reason = 'source_unavailable'
            if item['kind'] == 'refresh':
                with transaction(get_db()):
                    get_db().execute('UPDATE reading_order_sources SET error=?,checked_at=? WHERE id=?', (reason, self.clock(), args['source_id']))
            with self.lock:
                item.update(state='failed', reason=reason)

    def wanted_preview(self, order_id, revision, entries):
        integer(revision, 0)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 50 or len(set(entries)) != len(entries):
            raise ReadingOrderError('bounded')
        for value in entries:
            integer(value)
        store = ReadingOrderStore(get_db())
        store.guard(order_id, revision, False)
        selected = rows(get_db(), PROJECTION + 'SELECT * FROM projection WHERE id IN (SELECT value FROM json_each(?)) ORDER BY position', (json.dumps([order_id]), json.dumps(entries)))
        if len(selected) != len(entries):
            raise ReadingOrderError('invalid_request')
        results = []
        for row in selected:
            bucket = ('already_owned' if row['exact_owned'] else 'unresolved' if row['status'] in ('unresolved', 'ambiguous')
                else 'requires_add' if not row['canonical_id'] else 'content_represented' if row['content_elsewhere']
                else 'already_searching' if row['acquisition_active'] or row['searching'] or row['existing_acquisition'] else 'volume_unmonitored' if not row['volume_monitored']
                else 'already_wanted' if row['issue_monitored'] else 'ready')
            results.append(dict(entry_id=row['id'], issue_id=row['canonical_id'], volume_id=row['volume_id'],
                title=row['local_series'] or json.loads(row['source'])['series'], number=row['issue_number'], bucket=bucket))
        item = self.retain('wanted', results=results, order_id=order_id, order_revision=revision, entries=entries)
        item['digest'] = digest(results)
        return self.delivery(item['id'])

    def wanted_apply(self, handle, expected_digest, selected, confirmed):
        if confirmed is not True or not isinstance(selected, list) or not 1 <= len(selected) <= 50:
            raise ReadingOrderError('confirmation_required')
        with self.lock:
            item = self.get(handle)
            if item['kind'] != 'wanted' or item['digest'] != expected_digest:
                raise ReadingOrderError('revision_conflict')
            if item.get('result'):
                if item.get('selected') != selected:
                    raise ReadingOrderError('revision_conflict')
                return item['result']
            ready = {r['entry_id']: r for r in item['results'] if r['bucket'] == 'ready'}
            if len(set(selected)) != len(selected) or any(type(e) is not int or e not in ready for e in selected):
                raise ReadingOrderError('invalid_request')
            with transaction(get_db()):
                fresh = self.wanted_preview(item['order_id'], item['order_revision'], item['entries'])
                self.handles.pop(fresh['id'])
                if fresh['digest'] != expected_digest:
                    raise ReadingOrderError('stale')
                from backend.implementations.volumes import Issue
                for identity in {ready[e]['issue_id'] for e in selected}:
                    Issue(identity, check_existence=True).update({'monitored': True}, from_public=True)
            item['selected'] = selected
            item['result'] = dict(monitored_issues=sorted({ready[e]['issue_id'] for e in selected}),
                effect='issue_monitoring_enabled_existing_wanted', immediate_search=False)
            return item['result']

"""App-owned bounded provider assistance. Local state never needs provider IO."""

import json
from asyncio import run
from copy import deepcopy
from threading import RLock
from time import monotonic
from uuid import uuid4

from backend.base.collections import CollectionError, choice, integer, text
from backend.base.definitions import MonitorScheme, Task
from backend.features.metadata_search import aggregated_search
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.internals.collections import CollectionStore, transaction
from backend.internals.db import get_db


class CollectionTask(Task):
    action = 'collection_assistance'
    display_title = 'Collection provider assistance'
    volume_id = None
    issue_id = None

    def __init__(self, owner, identifier, operation, arguments):
        self.owner, self.identifier = owner, identifier
        self.operation, self.arguments = operation, arguments
        self.stop = False
        self.message = 'Queued explicit Collection request'

    def run(self):
        self.owner.execute(self.identifier, self.operation, self.arguments, self.stop)


class Collections:
    """One graph per application; search handles are not durable decisions."""

    def __init__(self, *, enqueue=None, clock=monotonic):
        self.enqueue, self.clock = enqueue, clock
        self.lock = RLock()
        self.handles: dict[str, dict] = {}

    def _expire(self):
        for key in tuple(self.handles):
            if self.handles[key]['expires'] <= self.clock():
                del self.handles[key]

    def submit_search(self, node_id, query, provider='all', suggestions=False):
        integer(node_id); text(query, 500)
        choice(provider, ('all', 'comicvine', 'metron', 'gcd'))
        if type(suggestions) is not bool:
            raise CollectionError('invalid_request')
        CollectionStore(get_db())._one('collection_nodes', node_id)
        return self._submit('search', dict(node_id=node_id, query=query, provider=provider, suggestions=suggestions))

    def submit_add(self, publication_id, root_id, provider, confirmed):
        integer(publication_id); integer(root_id)
        choice(provider, ('comicvine', 'metron', 'gcd'))
        if confirmed is not True:
            raise CollectionError('confirmation_required')
        store = CollectionStore(get_db())
        publication = store._resolve([publication_id])
        if not publication:
            raise CollectionError('not_found')
        # Exact response-loss retry is possible after this transient owner dies.
        if publication[0]['status'] == 'in_library':
            return dict(state='complete', publication_id=publication_id, volume_id=publication[0]['local_volume_id'])
        if publication[0]['status'] == 'ambiguous':
            raise CollectionError('publication_identity_conflict')
        if not any(r['provider'] == provider for r in publication[0]['refs']):
            raise CollectionError('invalid_reference')
        return self._submit('add', dict(publication_id=publication_id, root_id=root_id, provider=provider))

    def _submit(self, operation, arguments):
        with self.lock:
            self._expire()
            for key, item in self.handles.items():
                if operation == 'add' and item['operation'] == operation and item['arguments'] == arguments and item['state'] in ('queued', 'running'):
                    return self.delivery(key)
            if len(self.handles) >= 16:
                raise CollectionError('capacity')
            key = uuid4().hex
            self.handles[key] = dict(expires=self.clock() + 900, operation=operation, arguments=arguments,
                                     state='queued', task_id=None, results=[], providers=[], reason=None)
        task = CollectionTask(self, key, operation, arguments)
        try:
            if self.enqueue is None:
                from backend.features.tasks import TaskHandler
                task_id = TaskHandler().add(task)
            else:
                task_id = self.enqueue(task)
            with self.lock:
                self.handles[key]['task_id'] = task_id
        except Exception:
            with self.lock:
                self.handles.pop(key, None)
            raise CollectionError('task_unavailable') from None
        return self.delivery(key)

    def execute(self, key, operation, arguments, stopped=False):
        with self.lock:
            self._expire()
            item = self.handles.get(key)
            if item is None or item['state'] != 'queued':
                return
            item['state'] = 'cancelled' if stopped else 'running'
        if stopped:
            return
        try:
            if operation == 'search':
                receipts = run(aggregated_search(arguments['query'], selected_provider=None if arguments['provider'] == 'all' else arguments['provider']))
                results = []
                for receipt in receipts:
                    for value in receipt.results:
                        results.append(dict(key=value.provider + ':' + value.provider_id, provider=value.provider,
                            provider_id=value.provider_id, title=text(value.title, 500), year=value.year,
                            publisher=text(value.publisher or '', 500, True), kind='unknown'))
                if len(results) > 750 or len(json.dumps(results).encode()) > 2 * 1024 * 1024:
                    raise CollectionError('bounded')
                if arguments['suggestions']:
                    CollectionStore(get_db()).propose(arguments['node_id'], results, arguments['query'])
                outcome = dict(results=results, providers=[dict(provider=r.provider, state=r.status, reason=r.reason,
                    count=len(r.results)) for r in receipts])
            else:
                outcome = self._add(**arguments)
            with self.lock:
                item.update(outcome, state='complete')
        except (CollectionError, MetadataProviderError) as error:
            reason = str(error) if isinstance(error, CollectionError) else error.reason
            with self.lock:
                item.update(state='failed', reason=reason if reason in {'bounded', 'not_found', 'publication_identity_conflict',
                    'credentials', 'disabled', 'rate_limited', 'budget', 'unavailable'} else 'provider_unavailable')
        except Exception:
            # No exception repr/provider response/URL in transport or logs.
            with self.lock:
                item.update(state='failed', reason='operation_unavailable')

    def _add(self, publication_id, root_id, provider):
        from backend.implementations.volumes import Library
        store = CollectionStore(get_db())
        publication = store._resolve([publication_id])[0]
        if publication['status'] == 'ambiguous':
            raise CollectionError('publication_identity_conflict')
        volume = publication['local_volume_id']
        if volume is None:
            ref = next(r for r in publication['refs'] if r['provider'] == provider)
            volume = Library.add_metadata(ProviderVolumeIdentity(provider, ref['provider_id']), root_id,
                monitored=False, monitor_scheme=MonitorScheme.NONE, monitor_new_issues=False, auto_search=False)
        store.link_added(publication_id, volume)
        return dict(publication_id=publication_id, volume_id=volume)

    def delivery(self, key, offset=0, limit=50):
        integer(offset, 0, 750); integer(limit, 1, 100)
        with self.lock:
            self._expire()
            if key not in self.handles:
                raise CollectionError('search_expired')
            item = self.handles[key]
            return deepcopy(dict(id=key, operation=item['operation'], state=item['state'], reason=item['reason'],
                task_id=item['task_id'], providers=item['providers'], items=item['results'][offset:offset + limit],
                total=len(item['results']), has_next=offset + limit < len(item['results']),
                publication_id=item.get('publication_id'), volume_id=item.get('volume_id')))

    def propose_result(self, key, result_key):
        with self.lock:
            self._expire()
            item = self.handles.get(key)
            if item is None:
                raise CollectionError('search_expired')
            if item['operation'] != 'search' or item['state'] != 'complete':
                raise CollectionError('invalid_request')
            selected = [r for r in item['results'] if r['key'] == result_key]
            if len(selected) != 1:
                raise CollectionError('invalid_reference')
            return CollectionStore(get_db()).propose(item['arguments']['node_id'], selected, item['arguments']['query'])

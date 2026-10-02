"""Authenticated transport, exact confirmation and existing issue monitoring."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TReadingOrders as fixtures
from flask import Blueprint, Flask

from backend.features.reading_orders import ReadingOrders
from frontend.api import auth, error_handler, return_api
from frontend.reading_orders_api import register

CBL = fixtures.CBL


class ReadingOrderAPITests(TestCase):
    def setUp(self):
        self.fixture = fixtures.ReadingOrdersTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tasks = []
        self.owner = ReadingOrders(enqueue=self.tasks.append)
        self.addCleanup(self.owner.fetcher.dns.shutdown, wait=False, cancel_futures=True)
        self.app = Flask(__name__)
        self.app.extensions['reading_orders'] = self.owner
        api = Blueprint('reading_orders_test', __name__)
        register(api, auth, error_handler, return_api)
        self.app.register_blueprint(api, url_prefix='/api')
        self.client = self.app.test_client()
        for target, options in (
            ('frontend.api.Settings', dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='fixture-ro-key')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer', dict(return_value=None)),
            ('frontend.reading_orders_api.get_db', dict(side_effect=self.fixture.db.cursor)),
            ('backend.features.reading_orders.get_db', dict(side_effect=self.fixture.db.cursor)),
            ('backend.implementations.volumes.get_db', dict(side_effect=self.fixture.db.cursor)),
        ):
            p = patch(target, **options); p.start(); self.addCleanup(p.stop)

    def request(self, method, path='', body=None, status=200, auth=True, **kwargs):
        response = self.client.open('/api/reading-orders'+path+('&' if '?' in path else '?')+('api_key=fixture-ro-key' if auth else ''),
            method=method, **({'json': body} if body is not None else {}), **kwargs)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True)[:1000])
        return response.get_json()['result'] if response.is_json else response.data

    def test_every_route_auth_before_parsing_and_no_mutating_get(self):
        for rule in self.app.url_map.iter_rules():
            if not rule.rule.startswith('/api/reading-orders'):
                continue
            path = rule.rule.removeprefix('/api/reading-orders')
            for key in ('order_id', 'source_id'):
                path = path.replace('<int:'+key+'>', '1')
            path = path.replace('<string:handle>', 'a'*32)
            for method in rule.methods - {'HEAD', 'OPTIONS'}:
                with self.subTest(path=path, method=method):
                    self.request(method, path, status=401, auth=False, data='malformed')
            if 'POST' in rule.methods and 'GET' not in rule.methods:
                self.request('GET', path, status=405)

    def test_import_exact_accept_retry_export(self):
        review = self.request('POST', '/import', data=CBL, content_type='application/xml')
        path = '/reviews/'+review['id']+'/accept'
        identity = dict(revision=review['revision'], expected_digest=review['digest'], confirmed=True)
        self.request('POST', path, dict(identity, expected_digest='0'*64), status=409)
        first = self.request('POST', path, identity)
        self.assertEqual(first, self.request('POST', path, identity))
        self.assertEqual(self.fixture.db.execute('SELECT count(*) FROM reading_orders').fetchone()[0], 1)
        self.assertIn(b'<ReadingList>', self.request('GET', f"/{first['id']}/export"))

    def test_strict_json_queries_bounds_and_safe_error(self):
        for payload in ('{"title":"A","title":"B","description":""}', '{}', '{bad'):
            self.request('POST', '', data=payload, content_type='application/json', status=400)
        self.request('POST', '', dict(title='X', description='', paths=['C:/secret']), status=400)
        self.request('POST', '', data='x'*65537, content_type='application/json', status=400)
        for q in ('limit=101', 'limit=0', 'offset=-1', 'limit=1&limit=2', 'unknown=x'):
            self.request('GET', '?'+q, status=400)
        self.request('POST', '/1/subscriptions', dict(revision=0, url='file:///secret'), status=400)
        with patch('frontend.reading_orders_api.ReadingOrderStore.page', side_effect=RuntimeError('secret URL XML')):
            self.assertEqual(self.request('GET', '', status=500), {'reason': 'internal_error'})

    def test_wanted_review_explicit_issue_only_effect(self):
        order = self.fixture.imported()
        self.fixture.db.execute('UPDATE issues SET monitored=0 WHERE id=1'); self.fixture.db.commit()
        ids = [r['id'] for r in self.fixture.store.entries(order['id'])['items']]
        review = self.request('POST', f"/{order['id']}/wanted-preview", dict(revision=order['revision'], entries=ids))
        self.assertEqual([r['bucket'] for r in review['items']], ['ready', 'unresolved', 'ready', 'requires_add'])
        identity = dict(expected_digest=review['digest'], selected=[ids[0], ids[2]], confirmed=True)
        applied = self.request('POST', '/wanted/'+review['id']+'/apply', identity)
        self.assertEqual(applied['monitored_issues'], [1])
        self.assertFalse(applied['immediate_search'])
        self.assertEqual(applied, self.request('POST', '/wanted/'+review['id']+'/apply', identity))
        self.assertEqual(self.fixture.db.execute('SELECT monitored FROM volumes WHERE id=1').fetchone()[0], 1)
        self.assertEqual(self.fixture.db.execute('SELECT count(*) FROM wanted_searches').fetchone()[0], 0)

    def test_subscription_task_pending_survives_owner_replacement(self):
        order = self.fixture.imported()
        order = self.request('POST', f"/{order['id']}/subscriptions", dict(revision=order['revision'], url='https://example.com/a.cbl'))
        source = order['source']['id']
        from backend.base.reading_orders import parse_cbl
        with patch.object(self.owner.fetcher, 'fetch', return_value=dict(model=parse_cbl(CBL), digest='a'*64)):
            task = self.request('POST', f'/sources/{source}/refresh', {})
            self.assertEqual(task['state'], 'queued')
            self.tasks[-1].run()
        self.assertEqual(self.request('GET', '/tasks/'+task['id'])['state'], 'complete')
        pending = self.request('GET', f'/sources/{source}/pending')
        self.assertTrue(pending['pending'])
        replacement = ReadingOrders(enqueue=self.tasks.append)
        self.addCleanup(replacement.fetcher.dns.shutdown, wait=False, cancel_futures=True)
        self.app.extensions['reading_orders'] = replacement
        self.assertEqual(pending, self.request('GET', f'/sources/{source}/pending'))
        self.request('GET', '/tasks/'+task['id'], status=410)

    def test_queued_cancellation(self):
        task = self.request('POST', '/providers/search', dict(provider='metron', query='fixture'))
        self.tasks[-1].stop = True
        self.assertEqual(self.request('GET', '/tasks/'+task['id'])['state'], 'cancelled')

"""Real authenticated transport with deterministic provider/task boundaries."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TCollections as fixtures
from flask import Blueprint, Flask

from backend.features.collections import Collections
from backend.features.metadata_search import ProviderSearchReceipt
from backend.implementations.metadata.models import VolumeSearchResult
from frontend.api import auth, error_handler, return_api
from frontend.collections_api import register


class CollectionsAPITests(TestCase):
    def setUp(self):
        self.fixture = fixtures.CollectionsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.tasks = []
        self.owner = Collections(enqueue=lambda t: self.tasks.append(t) or len(self.tasks))
        self.app = Flask(__name__)
        self.app.extensions['collections'] = self.owner
        api = Blueprint('collections_test', __name__)
        register(api, auth, error_handler, return_api)
        self.app.register_blueprint(api, url_prefix='/api')
        self.client = self.app.test_client()
        for target, opts in (
            ('frontend.api.Settings', dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='fixture-collection-key')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer', dict(return_value=None)),
            ('frontend.collections_api.get_db', dict(side_effect=self.db.cursor)),
            ('backend.features.collections.get_db', dict(side_effect=self.db.cursor)),
        ):
            p = patch(target, **opts); p.start(); self.addCleanup(p.stop)

    def request(self, method, path, body=None, status=200, authorized=True, **kwargs):
        response = self.client.open('/api/collections' + path + ('&' if '?' in path else '?') +
            ('api_key=fixture-collection-key' if authorized else ''), method=method,
            **({'json': body} if body is not None else {}), **kwargs)
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()['result']

    def test_every_route_auth_before_body_and_no_mutating_get(self):
        routes = [('GET', ''), ('POST', ''), ('GET', '/1'), ('POST', '/1/nodes'),
            ('POST', '/1/nodes/1/delete'), ('GET', '/1/publications'), ('POST', '/nodes/1/local'),
            ('POST', '/nodes/1/membership'), ('POST', '/nodes/1/kind'), ('POST', '/nodes/1/search'),
            ('GET', '/tasks/' + 'a' * 32), ('POST', '/tasks/' + 'a' * 32 + '/propose'),
            ('GET', '/nodes/1/suggestions'), ('POST', '/suggestions/' + 'a' * 64 + '/decision'),
            ('POST', '/publications/1/add'), ('GET', '/discovery'), ('GET', '/discovery/nodes')]
        for method, path in routes:
            with self.subTest(path=path, method=method):
                self.request(method, path, authorized=False, status=401)
                if method == 'POST':
                    self.request(method, path, {'unexpected': 'value'}, status=400)
                    self.request(method, path, data='x' * 65537, content_type='application/json', status=409)
        self.assertFalse(self.tasks)

    def test_strict_payload_query_and_safe_errors(self):
        for path in ('/0', '/999999999999999999999999999999999999'):
            self.request('GET', path, status=400)
        for path in ('?limit=101', '?limit=-1', '?limit=0', '?unknown=x', '?limit=1&limit=2', '?limit=true'):
            self.request('GET', path, status=400)
        self.request('POST', '', data='{"title":"A","title":"B"}', content_type='application/json', status=400)
        with patch('frontend.collections_api.CollectionStore', side_effect=RuntimeError('secret url/path')):
            result = self.request('GET', '', status=500)
        self.assertEqual(result, {'reason': 'internal_error'})

    def test_search_projection_decision_and_restart(self):
        hostile = '<img src=x onerror="window.hostile=true">'
        async def search(*args, **kwargs):
            return [ProviderSearchReceipt(p, p, 'complete', 250, [VolumeSearchResult(p, '999', hostile, 2020, 1,
                'https://secret.invalid', 'raw description', 'https://secret.invalid', [], 'Publisher', 1, False, None)]) for p in ('comicvine', 'metron', 'gcd')]
        handle = self.request('POST', '/nodes/1/search', dict(query='Batman', provider='all', suggestions=True))
        with patch('backend.features.collections.aggregated_search', search):
            self.tasks[-1].run()
        result = self.request('GET', '/tasks/' + handle['id'])
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(len(result['items']), 3)
        self.assertNotIn('secret', str(result))
        self.assertEqual(result['items'][0]['title'], hostile)
        suggestions = self.request('GET', '/nodes/1/suggestions')['items']
        self.assertEqual(len(suggestions), 3)
        first = suggestions[0]
        self.request('POST', '/suggestions/' + first['id'] + '/decision', dict(revision=0, collection_revision=0, decision='accepted'))
        self.app.extensions['collections'] = Collections()
        self.request('GET', '/tasks/' + handle['id'], status=410)
        self.assertEqual(len(self.request('GET', '/1/publications')['items']), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM volumes').fetchone()[0], 1)

    def test_exact_add_retry_before_transient_task(self):
        self.request('POST', '/nodes/1/local', dict(revision=0, volume_id=1))
        result = self.request('POST', '/publications/1/add', dict(root_id=1, provider='comicvine', confirmed=True))
        self.assertEqual(result['volume_id'], 1)
        self.assertFalse(self.tasks)

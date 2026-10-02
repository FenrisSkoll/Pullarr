"""Real review/apply services through authenticated transport; no provider HTTP."""

import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TProviderSwitchApply as fixtures
from flask import Blueprint, Flask

from backend.features.provider_switch_review import ProviderSwitchReviews
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.switch_target import admit
from frontend.api import auth, error_handler, return_api
from frontend.provider_switch_api import register


class SwitchAPITests(TestCase):
    def setUp(self):
        self.fixture = fixtures.SwitchApplyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.source()
        self.service = self.fixture.service
        async def acquire(reference):
            return admit(fixtures.remote(reference.provider, count=3), reference)
        self.service.acquire = acquire
        self.app = Flask(__name__)
        self.app.extensions['provider_switch_reviews'] = self.service
        blueprint = Blueprint('switch_tests', __name__)
        register(blueprint, auth, error_handler, return_api)
        self.app.register_blueprint(blueprint, url_prefix='/api')
        self.client = self.app.test_client()
        for target, options in (
            ('frontend.provider_switch_api.get_db', dict(side_effect=self.fixture.db.cursor)),
            ('frontend.api.Settings', dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='fixture-auth-key')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer', dict(return_value=None)),
        ):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method, path, body=None, status=200, authorized=True):
        separator = '&' if '?' in path else '?'
        response = self.client.open('/api' + path + (separator + 'api_key=fixture-auth-key' if authorized else ''), method=method, json=body)
        self.assertEqual(response.status_code, status, response.get_json())
        return (response.get_json(silent=True) or {'result': {}})['result']

    def create(self):
        return self.request('POST', '/provider-switch/reviews', {'volume_id': 1, 'provider': 'metron', 'provider_id': '700'})

    def revise(self, review):
        return self.request('PUT', '/provider-switch/reviews/' + review['session_id'], {'revision': review['revision'],
            'mappings': [{'local_issue_id': 1, 'target_provider_id': '701'}, {'local_issue_id': 2, 'target_provider_id': '702'}]})

    def intent(self, review):
        return dict(revision=review['revision'], mapping_digest=review['preview']['mapping_digest'],
                    confirmed=True, source_authority=review['source_authority'])

    def test_authenticated_review_revision_apply_history_and_restart_retry(self):
        before = self.fixture.unchanged_files()
        original = self.create()
        self.assertFalse(original['preview']['apply_available'])
        review = self.revise(original)
        self.assertTrue(review['preview']['apply_available'])
        self.assertEqual(review['revision'], 2)
        path = '/provider-switch/reviews/' + review['session_id']
        self.assertEqual(self.request('GET', path)['revision'], 2)
        self.request('PUT', path, {'revision': 1, 'mappings': []}, status=409)
        result = self.request('POST', path + '/apply', self.intent(review))
        self.assertFalse(result['already_applied'])
        self.assertEqual(result['added_count'], 1)
        self.app.extensions['provider_switch_reviews'] = ProviderSwitchReviews(task_observer=lambda _: ())
        replay = self.request('POST', path + '/apply', self.intent(review))
        self.assertTrue(replay['already_applied'])
        self.assertEqual(result['id'], replay['id'])
        history = self.request('GET', '/volumes/1/provider-switch/history?limit=1')
        self.assertEqual(len(history), 1)
        detail = self.request('GET', '/provider-switch/receipts/' + result['id'] + '?detail=1&limit=2')
        self.assertEqual(len(detail['issues']), 2)
        tail = self.request('GET', '/provider-switch/receipts/' + result['id'] + '?detail=1&offset=2&limit=2')
        self.assertEqual(len(tail['issues']), 1)
        self.assertEqual(before, self.fixture.unchanged_files())
        self.fixture.assert_integrity()

    def test_all_routes_require_authentication(self):
        review = self.create()
        path = '/provider-switch/reviews/' + review['session_id']
        for method, endpoint in [('POST', '/provider-switch/reviews'), ('GET', path), ('PUT', path), ('DELETE', path),
                                 ('POST', path + '/apply'), ('GET', '/volumes/1/provider-switch/history'),
                                 ('GET', '/provider-switch/receipts/' + 'a' * 32)]:
            with self.subTest(method=method, endpoint=endpoint):
                self.request(method, endpoint, status=401, authorized=False)

    def test_read_only_creation_revision_and_gets(self):
        before = list(self.fixture.db.iterdump())
        review = self.revise(self.create())
        self.request('GET', '/provider-switch/reviews/' + review['session_id'])
        self.request('GET', '/volumes/1/provider-switch/history')
        self.assertEqual(before, list(self.fixture.db.iterdump()))

    def test_expiry_cancellation_worker_loss_and_staleness(self):
        review = self.create()
        path = '/provider-switch/reviews/' + review['session_id']
        self.fixture.clock = 901
        self.assertEqual(self.request('GET', path, status=410)['reason'], 'review_unavailable')
        review = self.create()
        path = '/provider-switch/reviews/' + review['session_id']
        self.request('DELETE', path)
        self.request('GET', path, status=410)
        review = self.create()
        path = '/provider-switch/reviews/' + review['session_id']
        self.app.extensions['provider_switch_reviews'] = ProviderSwitchReviews(task_observer=lambda _: ())
        self.request('GET', path, status=410)
        self.app.extensions['provider_switch_reviews'] = self.service
        self.fixture.db.execute("UPDATE volumes SET title='Edited' WHERE id=1")
        self.fixture.db.commit()
        self.assertEqual(self.request('GET', path, status=409)['reason'], 'stale_review')

    def test_invalid_input_bounds_and_apply_contract(self):
        for body in (None, {}, {'volume_id': True, 'provider': 'metron', 'provider_id': '700'},
                     {'volume_id': 1, 'provider': '../module', 'provider_id': '700'},
                     {'volume_id': 1, 'provider': 'metron', 'provider_id': 700}):
            self.request('POST', '/provider-switch/reviews', body, status=400)
        self.request('GET', '/provider-switch/reviews/invalid', status=400)
        self.request('GET', '/volumes/1/provider-switch/history?limit=1000', status=400)
        review = self.revise(self.create())
        path = '/provider-switch/reviews/' + review['session_id']
        intent = self.intent(review)
        self.request('POST', path + '/apply', dict(intent, confirmed=False), status=400)
        self.request('POST', path + '/apply', dict(intent, mapping_digest='0' * 64), status=409)
        self.request('POST', path + '/apply', dict(intent, target_metadata={}), status=400)
        self.request('GET', path + '/apply', status=405)
        self.assertEqual(self.fixture.db.execute('SELECT authority_generation FROM volumes').fetchone()[0], 0)

    def test_blocked_unresolved_and_mapping_conflicts(self):
        review = self.create()
        path = '/provider-switch/reviews/' + review['session_id']
        self.assertEqual(self.request('POST', path + '/apply', self.intent(review), status=409)['reason'], 'review_blocked')
        self.request('PUT', path, {'revision': 1, 'mappings': [
            {'local_issue_id': 1, 'target_provider_id': '701'}, {'local_issue_id': 2, 'target_provider_id': '701'}]}, status=409)
        self.request('PUT', path, {'revision': 1, 'mappings': [
            {'local_issue_id': 1, 'target_provider_id': 'foreign'}]}, status=409)

    def test_safe_provider_failures_and_unknown_errors(self):
        secret = 'secret-response-header-token'
        for reason, status, expected in [('credentials', 503, 'provider_auth_required'), ('disabled', 503, 'provider_disabled'),
                ('rate_limited', 429, 'provider_rate_limited'), ('incomplete', 409, 'target_incomplete'),
                (secret, 502, 'provider_unavailable')]:
            async def fail(reference):
                raise MetadataProviderError(secret, reason)
            self.service.acquire = fail
            result = self.request('POST', '/provider-switch/reviews', {'volume_id': 1, 'provider': 'metron', 'provider_id': '700'}, status=status)
            self.assertEqual(result['reason'], expected)
            self.assertNotIn(secret, json.dumps(result))
        async def unexpected(reference):
            raise RuntimeError(secret)
        self.service.acquire = unexpected
        with self.assertLogs('Kapowarr', level='ERROR') as logs:
            self.request('POST', '/provider-switch/reviews', {'volume_id': 1, 'provider': 'metron', 'provider_id': '700'}, status=500)
        self.assertNotIn(secret, '\n'.join(logs.output))

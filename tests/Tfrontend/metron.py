"""HTTP-facing Metron lifecycle with the actual adapter and fixture transport."""

from unittest import TestCase

from fixtures.comicvine_search import FAKE_APP_KEY
from Tbackend.implementations.metron_lifecycle import MetronHarness

from backend.implementations.volumes import refresh_and_scan
from backend.internals.server import Server


class MetronAPI(MetronHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        self.start_patch('backend.implementations.root_folders.get_db', side_effect=self.db.cursor)
        for name in ('WebSocket', 'SimpleQueue', 'MPWebSocketQueue'):
            self.start_patch('backend.internals.server.' + name)
        self.app = Server._create_app()
        self.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = self.app.test_client()

    def test_search_add_read_refresh_uses_metron_and_truthful_identity(self):
        args = {'api_key': FAKE_APP_KEY, 'metadata': 'true'}
        response = self.client.get('/api/volumes/search', query_string=dict(
            args, provider='metron', query='Example'))
        self.assertEqual(response.status_code, 200)
        result = response.get_json()['result'][0]
        self.assertIsNone(result['comicvine_id'])
        self.assertEqual(result['metadata_source'], {'provider': 'metron', 'id': '700'})
        response = self.client.post('/api/volumes', query_string=args, json={
            'provider': result['metadata_source']['provider'],
            'provider_id': result['metadata_source']['id'],
            'root_folder_id': 1, 'auto_search': False})
        self.assertEqual(response.status_code, 201)
        volume = response.get_json()['result']
        self.assertIsNone(volume['comicvine_id'])
        self.assertEqual(volume['external_ids'], {'metron': '700', 'gcd': '800'})
        self.assertEqual(volume['issues'][0]['external_ids']['comicvine'], '901')
        self.series['name'] = 'API refreshed'
        refresh_and_scan(volume['id'])
        response = self.client.get('/api/volumes/' + str(volume['id']), query_string=args)
        self.assertEqual(response.get_json()['result']['title'], 'API refreshed')
        self.session.get.assert_not_called()

    def test_missing_token_is_explicit_and_never_calls_comicvine(self):
        self.settings.metron_api_token = ''
        response = self.client.get('/api/volumes/search', query_string={
            'api_key': FAKE_APP_KEY, 'provider': 'metron', 'query': 'Example'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'MetadataProviderError')
        self.http.get.assert_not_called()
        self.session.get.assert_not_called()

    def test_credential_test_never_returns_token(self):
        response = self.client.post('/api/settings/metron/test', query_string={
            'api_key': FAKE_APP_KEY}, json={'metron_api_token': 'fake-unit-token'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('fake-unit-token', response.get_data(as_text=True))
        self.assertTrue(response.get_json()['result']['valid'])

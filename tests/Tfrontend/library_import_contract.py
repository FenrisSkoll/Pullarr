"""Legacy import transport and manual ComicVine search, before Phase 2B."""

from unittest import TestCase

from fixtures.comicvine_fetch import envelope
from fixtures.comicvine_search import FAKE_APP_KEY, volume_response
from fixtures.library_import import ImportHarness

from backend.internals.server import Server


class ImportAPIHarness(ImportHarness):
    def setUp(self):
        super().setUp()
        self.start_patch('frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        for name in ('WebSocket', 'SimpleQueue', 'MPWebSocketQueue'):
            self.start_patch('backend.internals.server.' + name)
        self.app = Server._create_app()
        self.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = self.app.test_client()

    def post_import(self, data, rename=False):
        return self.client.post('/api/libraryimport', json=data, query_string={
            'api_key': FAKE_APP_KEY, 'rename_files': str(rename).lower()})


class ImportAPIContract(ImportAPIHarness, TestCase):
    def test_legacy_payload_remote_integer_id_and_success_shape(self):
        path = self.comic_file()
        response = self.post_import([{'filepath': path, 'id': 2127}])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json(), {'error': None, 'result': {}})
        self.assertEqual(self.bindings(), [(path, 1, 301)])

    def test_invalid_proposal_rejected_before_writes(self):
        for data in ({}, ['invalid'], [{'id': 2127}], [{'filepath': 'missing'}]):
            self.assertEqual(self.post_import(data).status_code, 400)
        self.assertEqual(self.rows('volumes'), [])

    def test_manual_comicvine_search_result_can_be_imported(self):
        path = self.comic_file()
        self.response.json.side_effect = None
        self.response.json.return_value = envelope([volume_response()])
        response = self.client.get('/api/volumes/search', query_string={
            'api_key': FAKE_APP_KEY, 'query': 'Example Hero'})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()['result'][0]
        self.prepare_fetch()
        # Keep normal-series classification so existing filename matching applies.
        from fixtures.comicvine_fetch import issue_response
        self.prepare_fetch(issues=[issue_response(), issue_response(id=302, issue_number='2')])
        response = self.post_import([{'filepath': path, 'id': result['comicvine_id']}])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.bindings()), 1)

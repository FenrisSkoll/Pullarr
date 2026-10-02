"""Opt-in API compatibility for deterministic non-CV objects."""

from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fixtures.comicvine_fetch import LibraryAddHarness
from fixtures.comicvine_search import FAKE_APP_KEY
from fixtures.neutral_provider import NeutralProvider

from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.volumes import refresh_and_scan
from backend.internals.server import Server


class NeutralMetadataAPI(LibraryAddHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch(
            'frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        self.start_patch(
            'backend.implementations.root_folders.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.internals.server.WebSocket')
        self.start_patch('backend.internals.server.SimpleQueue')
        self.start_patch('backend.internals.server.MPWebSocketQueue')
        self.start_patch('backend.implementations.volumes.commit',
                         side_effect=self.db.commit)
        self.app = Server._create_app()
        self.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = self.app.test_client()
        self.provider = NeutralProvider()
        p = patch.dict(PROVIDERS, {'test_provider': lambda: self.provider})
        p.start()
        self.addCleanup(p.stop)

    def request(self, method, path, data=None, qualified=True):
        args = {'api_key': FAKE_APP_KEY}
        if qualified:
            args['metadata'] = 'true'
        return self.client.open('/api' + path, method=method, json=data, query_string=args)

    def create(self):
        response = self.request('POST', '/volumes', {
            'provider': 'test_provider', 'provider_id': 'V:alpha',
            'root_folder_id': 1, 'auto_search': False}, qualified=False)
        self.assertEqual(response.status_code, 201)
        return response.get_json()['result']

    def test_provider_request_serializes_real_identity_without_cv_fabrication(self):
        data = self.create()
        self.assertIsNone(data['comicvine_id'])
        self.assertEqual(data['metadata_source'], {
                         'provider': 'test_provider', 'id': 'V:alpha'})
        self.assertEqual(data['external_ids'], {'test_provider': 'V:alpha'})
        self.assertTrue(all(i['comicvine_id'] is None for i in data['issues']))
        self.assertEqual(data['issues'][0]['metadata_source']['id'], 'I:one')

    def test_legacy_routes_remain_cv_only_and_optin_routes_include_neutral(self):
        self.create()
        self.assertEqual(self.request('GET', '/volumes',
                         qualified=False).get_json()['result'], [])
        self.assertEqual(self.request('GET', '/volumes/1',
                         qualified=False).status_code, 400)
        self.assertEqual(self.request('GET', '/issues/1',
                         qualified=False).status_code, 400)
        self.assertEqual(
            len(self.request('GET', '/volumes').get_json()['result']), 1)
        self.assertEqual(self.request('GET', '/volumes/1').get_json()
                         ['result']['title'], 'Neutral Series')

    def test_edit_monitor_refresh_issue_view_and_rename_preview(self):
        self.create()
        self.assertEqual(self.request('PUT', '/volumes/1',
                         {'monitored': False}).status_code, 200)
        self.assertEqual(self.request('PUT', '/issues/1',
                         {'monitored': False}).status_code, 200)
        issue = self.request('GET', '/issues/1').get_json()['result']
        self.assertFalse(issue['monitored'])
        self.assertIsNone(issue['comicvine_id'])
        self.provider.title = 'Updated'
        refresh_and_scan(1)
        self.assertEqual(self.request(
            'GET', '/volumes/1').get_json()['result']['title'], 'Updated')
        self.assertEqual(self.request(
            'GET', '/volumes/1/rename').status_code, 200)

    def test_conflicting_request_and_unknown_provider_fail_before_insert(self):
        for values in ({'provider': 'test_provider', 'provider_id': 'V:alpha', 'comicvine_id': 123},
                       {'provider': 'unregistered', 'provider_id': '123'},
                       {'provider': 'test_provider', 'provider_id': 123}):
            response = self.request(
                'POST', '/volumes', dict(values, root_folder_id=1, auto_search=False))
            self.assertEqual(response.status_code, 400)
        self.assert_empty_library()

    def test_invalid_query_fails_before_add_and_delete_cascades_identities(self):
        response = self.client.post('/api/volumes', query_string={
            'api_key': FAKE_APP_KEY, 'metadata': 'invalid'}, json={
            'provider': 'test_provider', 'provider_id': 'V:alpha',
            'root_folder_id': 1, 'auto_search': False})
        self.assertEqual(response.status_code, 400)
        self.assert_empty_library()
        self.create()
        with patch('backend.features.tasks.TaskHandler.task_for_volume_running', return_value=False), patch(
                'backend.features.download_queue.DownloadHandler') as downloads:
            downloads.return_value.download_for_volume_queued.return_value = False
            response = self.client.delete('/api/volumes/1', query_string={
                'api_key': FAKE_APP_KEY, 'metadata': 'true', 'delete_folder': 'false'})
            self.assertEqual(response.status_code, 200)
        for table in ('volumes', 'issues', 'volume_external_ids', 'issue_external_ids'):
            self.assertEqual(self.db.execute(
                'SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_builtin_management_does_not_depend_on_cv_ids(self):
        root = Path(__file__).resolve(
        ).parents[2] / 'frontend' / 'static' / 'js'
        source = (root / 'view_volume.js').read_text(encoding='utf-8')
        self.assertNotIn('comicvine_id', source)
        for action in ('showRename', 'deleteVolume', 'toggleMonitored', 'showIssueInfo'):
            self.assertIn(action, source)
        general = (root / 'general.js').read_text(encoding='utf-8')
        self.assertEqual(general.count(
            "params = {metadata: 'true', issue_facts: '1', ...params}"), 2)

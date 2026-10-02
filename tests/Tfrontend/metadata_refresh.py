"""The existing task submission API remains asynchronous and unchanged."""

import unittest

from fixtures.comicvine_search import FAKE_APP_KEY
from fixtures.metadata_refresh import RefreshHarness

from backend.features.tasks import RefreshAndScanVolume, TaskHandler, UpdateAll
from backend.internals.server import Server


class RefreshTaskAPI(RefreshHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch(
            'frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        self.start_patch('backend.internals.server.WebSocket')
        self.start_patch('backend.internals.server.SimpleQueue')
        self.start_patch('backend.internals.server.MPWebSocketQueue')
        self.queued = self.start_patch(
            'backend.features.tasks.TaskHandler.add',
            return_value=73)
        self.client = Server._create_app().test_client()

    def post(self, **payload):
        return self.client.post('/api/system/tasks', json=payload,
                                query_string={'api_key': FAKE_APP_KEY})

    def test_manual_refresh_queues_local_volume_without_fetching(self):
        result = self.post(cmd='refresh_and_scan', volume_id=self.volume_id)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(
            result.get_json(), {
                'error': None, 'result': {
                    'id': 73}})
        task = self.queued.call_args.args[0]
        self.assertIsInstance(task, RefreshAndScanVolume)
        self.assertEqual(task.volume_id, self.volume_id)
        self.session.get.assert_not_awaited()

    def test_update_all_defaults_to_skipping_but_accepts_ui_forced_flag(self):
        self.assertEqual(self.post(cmd='update_all').status_code, 201)
        self.assertTrue(self.queued.call_args.args[0].allow_skipping)
        result = self.post(cmd='update_all', allow_skipping=False)
        self.assertEqual(
            result.get_json(), {
                'error': None, 'result': {
                    'id': 73}})
        self.assertIsInstance(self.queued.call_args.args[0], UpdateAll)
        self.assertFalse(self.queued.call_args.args[0].allow_skipping)

    def test_invalid_skipping_option_preserves_error_payload(self):
        result = self.post(cmd='update_all', allow_skipping='false')
        self.assertEqual(result.status_code, 400)
        self.assertEqual(
            result.get_json(), {
                'error': 'InvalidKeyValue', 'result': {
                    'key': 'allow_skipping', 'value': 'false'}})
        self.queued.assert_not_called()

    def test_no_separate_metadata_only_task_is_registered(self):
        self.assertEqual(
            {name for name in TaskHandler.tasks
             if 'refresh' in name or 'update' in name},
            {'refresh_and_scan', 'update_all'})

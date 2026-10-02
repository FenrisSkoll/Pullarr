"""Monitoring follows existing API authentication; GET never schedules work."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from flask import Flask

from frontend.api import api


class MonitorAPI(TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        self.client = app.test_client()
        self.settings = SimpleNamespace(api_key='fixture-app-key', folder_monitoring=False)
        settings = patch('frontend.api.Settings')
        settings.start().return_value.sv = self.settings
        self.addCleanup(settings.stop)
        timer = patch('frontend.api.StartTypeHandlers.diffuse_timer')
        timer.start()
        self.addCleanup(timer.stop)
        read = patch('backend.features.folder_monitor.monitoring_status', return_value={'roots': [], 'counts': []})
        self.read = read.start()
        self.addCleanup(read.stop)
        write = patch('backend.features.folder_monitor.request_reconciliation')
        self.request = write.start()
        self.addCleanup(write.stop)

    def test_read_status_never_schedules_or_enables(self):
        response = self.client.get('/api/foldermonitor', query_string={'api_key': self.settings.api_key})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['result']['enabled'])
        self.request.assert_not_called()

    def test_mutation_requires_authentication(self):
        response = self.client.post('/api/foldermonitor', json={})
        self.assertEqual(response.status_code, 401)
        self.request.assert_not_called()
        self.read.assert_not_called()

    def test_disabled_reconciliation_is_explicit_conflict(self):
        response = self.client.post('/api/foldermonitor', query_string={'api_key': self.settings.api_key})
        self.assertEqual(response.status_code, 409)
        self.request.assert_not_called()

    def test_enabled_post_only_requests_advisory_reconciliation(self):
        self.settings.folder_monitoring = True
        response = self.client.post('/api/foldermonitor', query_string={'api_key': self.settings.api_key},
                                    json={'path': '../../not-authorized', 'apply': True})
        self.assertEqual(response.status_code, 202)
        self.request.assert_called_once()
        self.assertEqual(len(self.request.call_args.args), 1)

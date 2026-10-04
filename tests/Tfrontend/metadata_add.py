"""Legacy POST add contract using actual Library.add and disposable SQL."""

import unittest
from os.path import join

from aiohttp import ClientError
from fixtures.comicvine_fetch import (LibraryAddHarness,
                                      issue_response, issue_result)
from fixtures.comicvine_search import (FAKE_APP_KEY, FAKE_CV_KEY,
                                       volume_response)

from backend.internals.server import Server


class AddVolumeMetadata(LibraryAddHarness, unittest.TestCase):
    def test_batman_continuations_add_independently_with_separate_issue_lists(self):
        created = []
        for identity, title, numbers, description in (
            ('100', 'Batman: Rebirth Deluxe Edition', (1, 2, 3), '<p>Continued in <a href="/4050-128991/">Batman: Deluxe Edition</a></p>'),
            ('128991', 'Batman: Deluxe Edition', (4, 5, 6), '<p>Preceded by <a href="/4050-100/">Batman: Rebirth Deluxe Edition</a></p>')):
            self.prepare_fetch(volume_response(id=identity, name=title, count_of_issues='3', description=description),
                [issue_response(id=str(1000 + n), volume={'id': identity}, issue_number=str(n)) for n in numbers])
            response = self.post_volume(comicvine_id=None, provider='comicvine', provider_id=identity)
            self.assertEqual(response.status_code, 201)
            result = response.json['result']
            self.assertEqual(result['metadata_source'], {'provider': 'comicvine', 'id': identity})
            self.assertEqual([i['issue_number'] for i in result['issues']], list(map(str, numbers)))
            created.append(result['id'])
        self.assertNotEqual(*created)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 6)

    def setUp(self):
        super().setUp()
        self.start_patch(
            'frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        self.start_patch('backend.internals.server.WebSocket')
        self.start_patch('backend.internals.server.SimpleQueue')
        self.start_patch('backend.internals.server.MPWebSocketQueue')
        self.app = Server._create_app()
        self.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = self.app.test_client()

    def post_volume(self, **overrides):
        payload = {
            'comicvine_id': 2127, 'root_folder_id': 1, 'monitor': True,
            'monitoring_scheme': 'all', 'monitor_new_issues': True,
            'volume_folder': '', 'special_version': 'auto', 'auto_search': False
        }
        payload.update(overrides)
        return self.client.post('/api/volumes', json=payload,
                                query_string={'api_key': FAKE_APP_KEY})

    def test_exact_legacy_created_response(self):
        response = self.post_volume()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json(), {'error': None, 'result': {
            'id': 1, 'comicvine_id': 2127, 'title': 'Example Hero', 'year': 2021,
            'publisher': 'Example Publisher', 'volume_number': 2,
            'special_version': 'tpb', 'special_version_locked': False,
            'description': '<p>An example series.</p>',
            'site_url': 'https://example.invalid/volume/4050-2127/',
            'monitored': True, 'monitor_new_issues': True,
            'folder': str(self.root / 'Example Hero' / 'Volume 02 (2021)'),
            'root_folder': 1, 'volume_folder': join('Example Hero', 'Volume 02 (2021)'),
            'issue_count': 1, 'issues_downloaded': 0, 'total_size': None,
            'issues': [dict(issue_result(), id=1, volume_id=1, monitored=True, files=[])],
            'general_files': []
        }})

    def test_errors_keep_http_status_and_payload(self):
        for api_status, http_status, error, result in (
            (101, 400, 'VolumeNotMatched', {}),
            (100, 400, 'InvalidKeyValue',
             {'key': 'comicvine_api_key', 'value': '[REDACTED]'}),
                (107, 509, 'MetadataSourceRateLimitReached', {})):
            with self.subTest(api_status=api_status):
                self.respond(None, api_status)
                response = self.post_volume()
                self.assertEqual(response.status_code, http_status)
                self.assertEqual(
                    response.get_json(), {
                        'error': error, 'result': result})
                self.assert_empty_library()

    def test_network_failure_is_legacy_509(self):
        self.session.get.side_effect = ClientError('offline')
        response = self.post_volume()
        self.assertEqual(response.status_code, 509)
        self.assertEqual(
            response.get_json(), {
                'error': 'MetadataSourceRateLimitReached', 'result': {}})
        self.assert_empty_library()

    def test_malformed_required_data_is_legacy_500(self):
        raw = volume_response()
        del raw['image']
        self.prepare_fetch(raw)
        with self.assertLogs(self.app.logger, level='ERROR'):
            response = self.post_volume()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(), {
                'error': 'InternalError', 'result': {}})
        self.assert_empty_library()

    def test_duplicate_add_response_contains_both_legacy_and_local_ids(self):
        self.assertEqual(self.post_volume().status_code, 201)
        self.session.get.reset_mock()
        response = self.post_volume()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(), {
                'error': 'VolumeAlreadyAdded', 'result': {
                    'comicvine_id': 2127, 'volume_id': 1}})
        self.session.get.assert_not_awaited()

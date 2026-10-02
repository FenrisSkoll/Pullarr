"""Characterize the real GET endpoint from raw CV JSON to public response."""

import unittest
from json import JSONDecodeError
from unittest.mock import AsyncMock

from aiohttp import ClientError, ContentTypeError
from fixtures.comicvine_search import (FAKE_APP_KEY, FAKE_CV_KEY,
                                       ComicVineSearchHarness, public_result,
                                       volume_response)

from backend.base.custom_exceptions import (InvalidKeyValue,
                                            MetadataSourceRateLimitReached)
from backend.base.definitions import Constants, StatusType
from backend.implementations.metadata.models import VolumeSearchResult
from backend.internals.server import Server
from frontend.metadata import legacy_volume_search_result


def neutral_result(**overrides):
    fields = public_result()
    del fields['comicvine_id']
    del fields['issues']
    fields.update(provider='comicvine', provider_id='2127')
    fields.update(overrides)
    return VolumeSearchResult(**fields)


class AddVolumeSearch(ComicVineSearchHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch(
            'frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        # Use the real app's HTTP error handlers without socket/background IO.
        self.start_patch('backend.internals.server.WebSocket')
        self.start_patch('backend.internals.server.SimpleQueue')
        self.start_patch('backend.internals.server.MPWebSocketQueue')
        self.app = Server._create_app()
        self.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = self.app.test_client()

    def search(self, query='Example Hero', **parameters):
        return self.client.get('/api/volumes/search', query_string={
            'api_key': FAKE_APP_KEY, 'query': query, **parameters
        })

    def assert_result(self, response, results):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(), {
                'error': None, 'result': results})

    def assert_error(self, response, status, error, result):
        self.assertEqual(response.status_code, status)
        self.assertEqual(
            response.get_json(), {
                'error': error, 'result': result})

    def test_normal_search_exact_public_fields_and_request(self):
        self.assert_result(self.search(), [public_result()])
        self.session.get.assert_awaited_once_with(
            Constants.CV_API_URL + '/search/',
            params={
                'format': 'json', 'api_key': FAKE_CV_KEY,
                'query': 'Example Hero', 'resources': 'volume', 'limit': 50,
                'field_list': (
                    'aliases,count_of_issues,deck,description,id,image,name,'
                    'publisher,site_detail_url,start_year'
                )
            }
        )
        self.status.clear.assert_called_once_with(
            StatusType.CV_RATE_LIMIT, 'search_volumes'
        )
        self.status.report.assert_not_called()

    def test_multiple_results_preserve_order_and_local_already_added_id(self):
        self.db.execute('INSERT INTO volumes VALUES (73, 9001);')
        self.respond([volume_response(id=9001), volume_response()])
        self.assert_result(self.search(), [
            public_result(comicvine_id=9001, already_added=73), public_result()
        ])

    def test_empty_results_do_not_query_library(self):
        self.respond([])
        self.assert_result(self.search(), [])
        self.db_lookup.assert_not_called()
        self.status.clear.assert_called_once()

    def test_direct_id_query_forms_use_volume_endpoint(self):
        for query in ('4050-2127', 'cv:2127', 'cv:4050-2127'):
            with self.subTest(query=query):
                self.respond(volume_response())
                self.assert_result(self.search(query), [public_result()])
                args, kwargs = self.session.get.call_args
                self.assertEqual(
                    args, (Constants.CV_API_URL + '/volume/4050-2127/',))
                self.assertEqual(set(kwargs['params']), {
                    'format', 'api_key', 'field_list'
                })

    def test_bare_numeric_id_is_a_text_search(self):
        self.assert_result(self.search('2127'), [public_result()])
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, (Constants.CV_API_URL + '/search/',))
        self.assertEqual(kwargs['params']['query'], '2127')

    def test_invalid_prefixed_id_is_empty_without_http(self):
        self.assert_result(self.search('cv:not-an-id'), [])
        self.session.get.assert_not_awaited()
        self.db_lookup.assert_not_called()
        self.status.clear.assert_called_once()

    def test_translations_are_marked_not_removed_by_backend(self):
        description = '<p>French publication.</p>'
        self.respond([
            volume_response(description=description), volume_response(id=9001)
        ])
        self.assert_result(self.search(only_english='true'), [
            public_result(description=description, translated=True),
            public_result(comicvine_id=9001)
        ])

    def test_null_optional_fields_and_volume_number_default(self):
        self.respond([volume_response(
            name=None, start_year=None, deck=None, description=None,
            aliases=None, publisher=None, count_of_issues=0
        )])
        self.assert_result(self.search(), [public_result(
            title='', year=None, volume_number=1, description=None,
            aliases=[], publisher=None, issue_count=0
        )])

    def test_missing_optional_fields_are_defaulted(self):
        volume = volume_response()
        for key in ('start_year', 'aliases', 'publisher'):
            del volume[key]
        self.respond([volume])
        self.assert_result(self.search(), [public_result(
            year=None, aliases=[], publisher=None
        )])

    def test_description_cleanup_and_volume_number_from_predecessor(self):
        self.respond([volume_response(
            deck=None,
            description=(
                '<p>preceded by Example Hero Volume 3.</p>'
                '<figure><img src="https://example.invalid/image.jpg"/></figure>'
                '<h2>Credits</h2><ul><li>Creator</li></ul>'
            )
        )])
        self.assert_result(self.search(), [public_result(
            description='<p>preceded by Example Hero Volume 3.</p>',
            volume_number=4
        )])

    def test_provider_not_found_is_empty_without_clearing_status(self):
        self.respond([], status_code=101)
        self.assert_result(self.search('cv:2127'), [])
        self.status.clear.assert_not_called()
        self.status.report.assert_not_called()

    def test_missing_comicvine_key(self):
        self.settings.comicvine_api_key = ''
        self.assert_error(self.search(), 400, 'InvalidKeyValue', {
            'key': 'comicvine_api_key', 'value': '[REDACTED]'
        })
        self.session.get.assert_not_awaited()

    def test_rejected_comicvine_key(self):
        self.respond([], status_code=100)
        self.assert_error(self.search(), 400, 'InvalidKeyValue', {
            'key': 'comicvine_api_key', 'value': '[REDACTED]'
        })
        self.status.clear.assert_not_called()
        self.status.report.assert_not_called()

    def test_provider_rate_limit(self):
        self.respond([], status_code=107)
        self.assert_error(
            self.search(),
            509,
            'MetadataSourceRateLimitReached',
            {})
        self.status.report.assert_called_once_with(
            StatusType.CV_RATE_LIMIT, 'search_volumes'
        )
        self.status.clear.assert_not_called()

    def test_network_failure_is_currently_reported_as_rate_limit(self):
        self.session.get.side_effect = ClientError('synthetic offline failure')
        self.assert_error(
            self.search(),
            509,
            'MetadataSourceRateLimitReached',
            {})
        self.status.report.assert_called_once_with(
            StatusType.CV_RATE_LIMIT, 'search_volumes'
        )

    def test_invalid_json_and_content_type_are_currently_rate_limits(self):
        for error in (
            JSONDecodeError('synthetic invalid JSON', '', 0),
            ContentTypeError(None, (), message='synthetic non-JSON content')
        ):
            with self.subTest(error=type(error).__name__):
                self.status.reset_mock()
                self.response.json.side_effect = error
                self.assert_error(
                    self.search(), 509, 'MetadataSourceRateLimitReached', {}
                )
                self.status.report.assert_called_once_with(
                    StatusType.CV_RATE_LIMIT, 'search_volumes'
                )

    def test_missing_required_data_remains_internal_error(self):
        for field in ('image', 'id', 'count_of_issues', 'site_detail_url'):
            with self.subTest(field=field):
                volume = volume_response()
                del volume[field]
                self.respond([volume])
                with self.assertLogs(self.app.logger, level='ERROR'):
                    response = self.search()
                self.assert_error(response, 500, 'InternalError', {})

    def test_malformed_envelope_remains_internal_error(self):
        self.response.json.return_value = {'results': []}
        with self.assertLogs(self.app.logger, level='ERROR'):
            response = self.search()
        self.assert_error(response, 500, 'InternalError', {})

    def test_missing_and_empty_query_keep_validation_errors(self):
        response = self.client.get('/api/volumes/search', query_string={
            'api_key': FAKE_APP_KEY
        })
        self.assert_error(response, 400, 'KeyNotFound', {'key': 'query'})
        self.assert_error(self.search(''), 400, 'InvalidKeyValue', {
            'key': 'query', 'value': ''
        })
        self.session.get.assert_not_awaited()

    def test_application_authentication_precedes_provider_search(self):
        self.assert_error(
            self.search(api_key='test-only-wrong-key'), 401, 'ApiKeyInvalid', {}
        )
        self.session.get.assert_not_awaited()

    def test_endpoint_resolves_default_provider_and_serializes_legacy_shape(
            self):
        resolver = self.start_patch('frontend.api.get_search_provider')
        search = resolver.return_value.search_volumes = AsyncMock(
            return_value=[neutral_result()]
        )
        response = self.search('cv:2127')
        self.assert_result(response, [public_result()])
        public_fields = list(response.get_json()['result'][0])
        self.assertEqual(public_fields, list(public_result()))
        resolver.assert_called_once_with()
        search.assert_awaited_once_with('cv:2127')
        self.session.get.assert_not_awaited()

    def test_empty_provider_results_preserve_endpoint_envelope(self):
        resolver = self.start_patch('frontend.api.get_search_provider')
        resolver.return_value.search_volumes = AsyncMock(return_value=[])
        self.assert_result(self.search(), [])
        resolver.return_value.search_volumes.assert_awaited_once()

    def test_provider_exceptions_keep_existing_http_errors(self):
        resolver = self.start_patch('frontend.api.get_search_provider')
        for error, status, result in (
            (InvalidKeyValue('comicvine_api_key', FAKE_CV_KEY), 400,
             {'key': 'comicvine_api_key', 'value': '[REDACTED]'}),
            (MetadataSourceRateLimitReached(), 509, {})
        ):
            with self.subTest(error=type(error).__name__):
                search = AsyncMock(side_effect=error)
                resolver.return_value.search_volumes = search
                self.assert_error(
                    self.search(),
                    status,
                    type(error).__name__,
                    result)

    def test_invalid_requests_do_not_resolve_provider(self):
        resolver = self.start_patch('frontend.api.get_search_provider')
        self.search(api_key='test-only-wrong-key')
        self.search('')
        resolver.assert_not_called()

    def test_folder_preview_post_does_not_resolve_provider(self):
        resolver = self.start_patch('frontend.api.get_search_provider')
        naming = self.start_patch(
            'frontend.api.generate_volume_folder_name',
            return_value='Example Hero')
        response = self.client.post(
            '/api/volumes/search',
            query_string={'api_key': FAKE_APP_KEY}, json={
                'comicvine_id': 2127, 'title': 'Example Hero', 'year': 2021,
                'volume_number': 2, 'publisher': 'Example Publisher'
            }
        )
        self.assert_result(response, {'folder': 'Example Hero'})
        self.assertEqual(naming.call_args[0][0].comicvine_id, 2127)
        resolver.assert_not_called()


class LegacySearchMapping(unittest.TestCase):
    def test_does_not_publish_non_comicvine_ids_under_legacy_key(self):
        candidate = neutral_result()
        candidate.provider = 'other-provider'
        for provider_id in ('2127', 'opaque-id'):
            candidate.provider_id = provider_id
            with self.subTest(provider_id=provider_id):
                with self.assertRaises(ValueError):
                    legacy_volume_search_result(candidate)

    def test_serialization_does_not_mutate_neutral_identity(self):
        candidate = neutral_result()
        self.assertEqual(
            legacy_volume_search_result(candidate),
            public_result())
        self.assertEqual(candidate.provider_id, '2127')
        self.assertEqual(candidate.provider, 'comicvine')

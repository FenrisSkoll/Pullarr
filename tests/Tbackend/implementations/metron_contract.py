"""Offline API contracts committed before enabling production registration.

The adapter imports are deliberately inside setup: before implementation these
tests constitute the red TDD gate, not evidence of a working provider.
"""

from asyncio import run
from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, patch

from fixtures.metron import SERIES, issue, issue_summary, page, series_summary

from backend.base.definitions import DateType


class MetronMappingContract(TestCase):
    def setUp(self):
        from backend.implementations.metadata.metron import \
            MetronMetadataProvider
        self.provider = MetronMetadataProvider()

    def test_series_detail_namespace_fields_and_references(self):
        result = self.provider.volume_result(deepcopy(SERIES), [], DateType.COVER_DATE)
        value = result.metadata
        self.assertEqual((value.provider, value.provider_id), ('metron', '700'))
        self.assertEqual((value.title, value.year, value.volume_number),
                         ('Example Collection', 2020, 2))
        self.assertEqual(value.publisher, 'Example Publisher')
        self.assertEqual(value.aliases, ['Example Alias'])
        self.assertEqual(value.issue_count, 2)
        self.assertEqual(value.description, SERIES['desc'])
        self.assertEqual(value.site_url, SERIES['resource_url'])
        self.assertIsNone(value.cover_link)
        self.assertEqual([(r.provider, r.provider_id, r.provenance)
                          for r in result.enrichment], [('gcd', '800', 'metron')])
        self.assertEqual([(r.entity, r.owner_provider, r.owner_id)
                          for r in result.enrichment], [('volume', 'metron', '700')])

    def test_search_shape_does_not_invent_unavailable_metadata(self):
        result = self.provider.search_result(series_summary())
        self.assertEqual((result.provider, result.provider_id), ('metron', '700'))
        self.assertEqual(result.title, 'Example Collection (2020)')
        self.assertIsNone(result.cover_link)
        self.assertIsNone(result.description)
        self.assertEqual(result.aliases, [])

    def test_collection_title_precedes_story_titles(self):
        result = self.provider.issue_metadata(issue(), '700', DateType.COVER_DATE)
        self.assertEqual(result.title, 'A meaningful collection')
        self.assertEqual(result.date, '2020-02-01')
        self.assertEqual(result.description, 'Issue description.')
        self.assertEqual((result.provider_id, result.volume_provider_id), ('701', '700'))

    def test_story_titles_and_store_date(self):
        result = self.provider.issue_metadata(issue(title=''), '700', DateType.STORE_DATE)
        self.assertEqual(result.title, 'First story; Second story')
        self.assertEqual(result.date, '2020-01-15')

    def test_optional_null_fields(self):
        result = self.provider.issue_metadata(issue(
            title=None, name=[], desc=None, store_date=None), '700', DateType.STORE_DATE)
        self.assertIsNone(result.title)
        self.assertIsNone(result.description)
        self.assertIsNone(result.date)
        data = deepcopy(SERIES)
        data.update(publisher=None, alt_names=None, desc=None, year_began=None)
        volume = self.provider.volume_metadata(data, [])
        self.assertIsNone(volume.publisher)
        self.assertIsNone(volume.year)
        self.assertEqual(volume.aliases, [])

    def test_language_and_empty_series(self):
        data = deepcopy(SERIES)
        data.update(language='fr', issue_count=0)
        volume = self.provider.volume_metadata(data, [])
        self.assertTrue(volume.translated)
        self.assertEqual(volume.issues, [])

    def test_issue_numbers_stay_strings(self):
        for number in ('1', '1.5', '1A', 'Annual', '-1'):
            with self.subTest(number=number):
                result = self.provider.issue_metadata(issue(number=number), '700', DateType.COVER_DATE)
                self.assertEqual(result.issue_number, number)
                self.assertIsInstance(result.calculated_issue_number, float)

    def test_both_external_references_are_namespaced_strings(self):
        fetched = self.provider.volume_result(deepcopy(SERIES),
            [issue(cv_id=901, gcd_id=902)], DateType.COVER_DATE)
        result = fetched.metadata.issues[0]
        refs = [r for r in fetched.enrichment if r.entity == 'issue']
        self.assertEqual([(r.provider, r.provider_id) for r in refs],
                         [('comicvine', '901'), ('gcd', '902')])
        self.assertEqual(result.provider, 'metron')
        self.assertEqual([(r.owner_provider, r.owner_id, r.provenance) for r in refs],
                         [('metron', '701', 'metron'), ('metron', '701', 'metron')])

    def test_malformed_references_and_wrong_parent_rejected(self):
        from backend.implementations.metadata.metron_client import MetronError
        for value in (True, -1, 0, 'not-an-id', 1.5):
            with self.subTest(value=value), self.assertRaises(MetronError):
                self.provider.issue_metadata(issue(cv_id=value), '700', DateType.COVER_DATE)
        with self.assertRaises(MetronError):
            self.provider.issue_metadata(issue(), 'other-parent', DateType.COVER_DATE)


class MetronHTTPContract(TestCase):
    def setUp(self):
        from backend.implementations.metadata.metron_client import (
            MetronClient, MetronError)
        self.error = MetronError
        # A conspicuously synthetic value, generated in the test, not a fixture token.
        self.client = MetronClient(token='unit-' + 'not-a-credential')
        self.transport = patch('backend.implementations.metadata.metron_client.Session')
        self.session = self.transport.start().return_value.__enter__.return_value
        self.addCleanup(self.transport.stop)
        self.rate = patch('backend.implementations.metadata.metron_client.RATE_STATE', {})
        self.rate.start()
        self.addCleanup(self.rate.stop)

    def response(self, body, code=200, headers=None):
        response = Mock(status_code=code, headers=headers or {})
        response.json.return_value = body
        self.session.get.return_value = response
        return response

    def test_series_search_query_and_token_header(self):
        self.response(page([series_summary()]))
        result = self.client.get('series/', {'name': 'Example'})
        self.assertEqual(result['count'], 1)
        args, kwargs = self.session.get.call_args
        self.assertEqual(args[0], 'https://metron.cloud/api/series/')
        self.assertEqual(kwargs['params'], {'name': 'Example'})
        self.assertTrue(kwargs['headers']['Authorization'].startswith('Bearer '))
        self.assertFalse(kwargs['allow_redirects'])

    def test_multiple_and_empty_paginated_results(self):
        self.response(page([]))
        self.assertEqual(self.client.pages('series/'), [])
        first = self.response(page([series_summary()],
                                  'https://metron.cloud/api/series/?page=2', 2))
        second = self.response(page([series_summary()], count=2))
        self.session.get.side_effect = [first, second]
        self.assertEqual(len(self.client.pages('series/')), 2)

    def test_issue_list_is_not_full_detail(self):
        self.response(page([issue_summary(issue())]))
        result = self.client.pages('series/700/issue_list/')
        self.assertNotIn('cv_id', result[0])
        self.assertNotIn('title', result[0])

    def test_missing_token_does_not_send_request(self):
        from backend.implementations.metadata.metron_client import MetronClient
        with self.assertRaises(self.error):
            MetronClient(token='').get('series/')
        self.session.get.assert_not_called()

    def test_http_failures_are_not_empty_success_or_secret_echo(self):
        for status, reason in ((401, 'credentials'), (403, 'forbidden'),
                               (404, 'not_found'), (500, 'unavailable')):
            self.response({'detail': 'sensitive upstream body'}, status)
            with self.subTest(status=status), self.assertRaises(self.error) as error:
                self.client.get('series/700/')
            self.assertEqual(error.exception.reason, reason)
            self.assertNotIn('sensitive', str(error.exception))

    def test_429_records_retry_after_without_retry_loop(self):
        from backend.implementations.metadata.metron_client import RATE_STATE
        self.response({}, 429, {'Retry-After': '3600'})
        with self.assertRaises(self.error) as error:
            self.client.get('series/')
        self.assertEqual(error.exception.reason, 'rate_limited')
        self.assertTrue(RATE_STATE)
        self.assertEqual(self.session.get.call_count, 1)
        with self.assertRaises(self.error):
            self.client.get('series/')
        self.assertEqual(self.session.get.call_count, 1)

    def test_sustained_headers_prevent_next_request(self):
        from time import time
        self.response(page([]), headers={
            'X-RateLimit-Sustained-Limit': '42',
            'X-RateLimit-Sustained-Remaining': '0',
            'X-RateLimit-Sustained-Reset': str(int(time()) + 3600)})
        self.client.get('series/')
        with self.assertRaises(self.error):
            self.client.get('series/')
        self.assertEqual(self.session.get.call_count, 1)

    def test_network_and_malformed_json_errors(self):
        from requests import Timeout
        self.session.get.side_effect = Timeout('do not disclose transport internals')
        with self.assertRaises(self.error) as error:
            self.client.get('series/')
        self.assertEqual(error.exception.reason, 'unavailable')
        self.session.get.side_effect = None
        self.response(None).json.side_effect = ValueError('malformed body')
        with self.assertRaises(self.error) as error:
            self.client.get('series/')
        self.assertEqual(error.exception.reason, 'malformed')

    def test_pagination_failure_is_not_a_partial_success(self):
        first = self.response(page([series_summary()],
                                  'https://metron.cloud/api/series/?page=2', 2))
        second = self.response({}, 503)
        self.session.get.side_effect = [first, second]
        with self.assertRaises(self.error):
            self.client.pages('series/')

    def test_foreign_next_url_and_redirect_never_receive_token(self):
        self.response(page([], 'https://example.invalid/steal-token', 1))
        with self.assertRaises(self.error):
            self.client.pages('series/')
        self.assertEqual(self.session.get.call_count, 1)
        self.response({}, 302, {'Location': 'https://example.invalid/'})
        with self.assertRaises(self.error):
            self.client.get('series/')

    def test_repeated_or_incomplete_pages_rejected(self):
        for body in (page([], count=2), page([], 'https://metron.cloud/api/series/')):
            self.session.get.reset_mock()
            self.response(body)
            with self.assertRaises(self.error):
                self.client.pages('series/')
            self.assertLessEqual(self.session.get.call_count, 2)

"""Characterization of full-volume fetching before the provider refactor."""

import unittest
from asyncio import run
from json import JSONDecodeError

from aiohttp import ClientError
from fixtures.comicvine_fetch import (COVER, ComicVineFetchHarness,
                                      envelope, fetched_result,
                                      issue_response, issue_result)
from fixtures.comicvine_search import volume_response

from backend.base.custom_exceptions import (InvalidKeyValue,
                                            MetadataSourceRateLimitReached,
                                            VolumeNotMatched)
from backend.base.definitions import Constants, DateType, StatusType
from backend.implementations.comicvine import ComicVine


class ComicVineFetch(ComicVineFetchHarness, unittest.TestCase):
    def test_volume_and_issue_transformation_exact_fields(self):
        self.assertEqual(run(ComicVine().fetch_volume(2127)), fetched_result())
        self.assertEqual(self.session.get.await_args_list[0].args,
                         (Constants.CV_API_URL + '/volume/4050-2127/',))
        self.assertEqual(
            self.session.get.await_args_list[1].kwargs['params']['filter'],
            'volume:2127')
        self.session.get_content.assert_awaited_once_with(
            'https://example.invalid/cover.jpg', quiet_fail=True)
        self.status.clear.assert_any_call(
            StatusType.CV_RATE_LIMIT, 'fetch_volume')
        self.status.clear.assert_any_call(
            StatusType.CV_RATE_LIMIT, 'fetch_issues')

    def test_supported_id_forms(self):
        for identity in (2127, '2127', '4050-2127', 'cv:2127'):
            with self.subTest(identity=identity):
                self.prepare_fetch()
                self.assertEqual(
                    run(ComicVine().fetch_volume(identity)),
                    fetched_result())

    def test_multiple_issues_preserve_response_order(self):
        self.prepare_fetch(issues=[issue_response(id='302', issue_number='2'),
                                   issue_response()])
        result = run(ComicVine().fetch_volume(2127))
        self.assertEqual(result['issues'], [
            issue_result(comicvine_id=302, issue_number='2', calculated_issue_number=2.0),
            issue_result()
        ])

    def test_issue_number_normalization_and_calculation(self):
        for number, display, calculated in (
            (' 2 ', '2', 2.0), ('1/2', '1-2', 1.0),
            ('1.5', '1.5', 1.5), ('?', '?', 0.0),
            ('unknown', 'unknown', 0.21141114152314)
        ):
            with self.subTest(number=number):
                self.prepare_fetch(issues=[issue_response(issue_number=number)])
                issue = run(ComicVine().fetch_volume(2127))['issues'][0]
                self.assertEqual(issue['issue_number'], display)
                self.assertEqual(issue['calculated_issue_number'], calculated)

    def test_nullable_optional_fields_and_missing_cover(self):
        self.prepare_fetch(volume_response(
            name=None, start_year=None, deck=None, description=None,
            publisher=None, aliases=None, image={'small_url': None}),
            [issue_response(name=None, cover_date=None, description=None)])
        self.session.get_content.return_value = b''
        result = run(ComicVine().fetch_volume(2127))
        self.assertEqual(result, fetched_result(
            title='', year=None, volume_number=1, description=None,
            publisher=None, aliases=[], cover_link=None, cover=None,
            issues=[issue_result(title=None, date=None, description=None)]))

    def test_store_date_setting(self):
        self.settings.date_type = DateType.STORE_DATE
        self.assertEqual(run(ComicVine().fetch_volume(2127))
                         ['issues'][0]['date'], '2020-12-15')

    def test_translated_metadata_is_not_filtered(self):
        self.prepare_fetch(
            volume_response(
                description='<p>French translation of Example Hero.</p>'))
        result = run(ComicVine().fetch_volume(2127))
        self.assertTrue(result['translated'])
        self.assertEqual(result['aliases'], ['Alternate Hero', 'Another Hero'])

    def test_invalid_id_does_not_make_request(self):
        with self.assertRaises(VolumeNotMatched):
            run(ComicVine().fetch_volume('not-an-id'))
        self.session.get.assert_not_awaited()

    def test_api_error_semantics(self):
        for status, exception in (
            (101, VolumeNotMatched),
            (100, InvalidKeyValue),
                (107, MetadataSourceRateLimitReached)):
            with self.subTest(status=status):
                self.respond(None, status)
                with self.assertRaises(exception):
                    run(ComicVine().fetch_volume(2127))

    def test_missing_key(self):
        self.settings.comicvine_api_key = ''
        with self.assertRaises(InvalidKeyValue):
            run(ComicVine().fetch_volume(2127))
        self.session.get.assert_not_awaited()

    def test_network_and_json_failures_keep_rate_limit_semantics(self):
        for error in (
            ClientError('offline'),
            JSONDecodeError(
                'invalid',
                '',
                0)):
            with self.subTest(error=type(error)):
                self.response.json.side_effect = error
                with self.assertRaises(MetadataSourceRateLimitReached):
                    run(ComicVine().fetch_volume(2127))
        self.status.report.assert_any_call(
            StatusType.CV_RATE_LIMIT, 'fetch_volume')

    def test_issue_rate_limit_returns_empty_issues_and_still_fetches_cover(
        self):
        self.response.json.side_effect = [
            envelope(
                volume_response()), envelope(
                [], 107)]
        result = run(ComicVine().fetch_volume(2127))
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['cover'], COVER)
        self.status.report.assert_called_once_with(
            StatusType.CV_RATE_LIMIT, 'fetch_issues')

    def test_later_issue_page_failure_preserves_partial_results(self):
        self.response.json.side_effect = [
            envelope(volume_response()),
            envelope([issue_response()], number_of_total_results=101),
            envelope([], 107)
        ]
        self.assertEqual(run(ComicVine().fetch_volume(2127))
                         ['issues'], [issue_result()])
        self.assertEqual(
            self.session.get.await_args_list[-1].kwargs['params']['offset'],
            100)

    def test_issue_pagination_appends_in_page_order(self):
        self.response.json.side_effect = [
            envelope(volume_response()),
            envelope([issue_response(id=302)], number_of_total_results=101),
            envelope([issue_response()])
        ]
        self.assertEqual(
            [i['comicvine_id']
             for i in run(ComicVine().fetch_volume(2127))['issues']],
            [302, 301])

    def test_required_malformed_data_is_not_silently_repaired(self):
        raw = volume_response()
        del raw['image']
        self.prepare_fetch(raw)
        with self.assertRaises(KeyError):
            run(ComicVine().fetch_volume(2127))

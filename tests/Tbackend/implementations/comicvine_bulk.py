"""Raw HTTP characterization for the optimized ComicVine refresh methods."""

import unittest
from asyncio import run
from json import JSONDecodeError
from unittest.mock import AsyncMock

from aiohttp import ClientError
from fixtures.comicvine_fetch import (COVER, ComicVineFetchHarness,
                                      envelope, fetched_result,
                                      issue_response, issue_result)
from fixtures.comicvine_search import volume_response

from backend.base.custom_exceptions import InvalidKeyValue, VolumeNotMatched
from backend.base.definitions import Constants, StatusType
from backend.implementations.comicvine import ComicVine


class ComicVineBulk(ComicVineFetchHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sleep = self.start_patch(
            'backend.implementations.comicvine.sleep',
            new_callable=AsyncMock)

    def test_bulk_volume_exact_metadata_without_issues_and_server_order(self):
        self.respond([volume_response(id=9001), volume_response()])
        result = run(ComicVine().fetch_volumes((2127, 9001)))
        self.assertEqual(
            result, [
                fetched_result(
                    comicvine_id=9001, issues=None), fetched_result(
                    issues=None)])
        self.assertEqual(
            self.session.get.await_args.kwargs['params']['filter'],
            'id:2127|9001')
        self.assertEqual(self.session.get_content.await_count, 2)
        self.status.report.assert_not_called()
        self.status.clear.assert_not_called()

    def test_100_ids_per_request_1000_per_batch_with_cooldown(self):
        ids = tuple(range(1, 1002))
        self.respond([])
        self.assertEqual(run(ComicVine().fetch_volumes(ids)), [])
        groups = [call.kwargs['params']['filter'][3:].split('|')
                  for call in self.session.get.await_args_list]
        self.assertEqual([len(group) for group in groups], [100] * 10 + [1])
        self.assertEqual(
            [value for group in groups for value in group],
            list(map(str, ids)))
        self.sleep.assert_awaited_once_with(Constants.CV_BRAKE_TIME * 10)

    def test_failed_volume_request_does_not_discard_other_results(self):
        self.response.json.side_effect = [
            envelope([], 107), envelope([volume_response(id=101)])]
        result = run(ComicVine().fetch_volumes(tuple(range(1, 102))))
        self.assertEqual(
            result, [
                fetched_result(
                    comicvine_id=101, issues=None)])
        self.assertEqual(self.session.get.await_count, 2)
        self.status.report.assert_not_called()

    def test_next_batch_still_runs_after_rate_limited_batch(self):
        self.response.json.side_effect = [
            envelope([], 107)] * 10 + [envelope([volume_response(id=1001)])]
        result = run(ComicVine().fetch_volumes(tuple(range(1, 1002))))
        self.assertEqual([v['comicvine_id'] for v in result], [1001])
        self.sleep.assert_awaited_once_with(10.0)

    def test_bulk_network_and_json_failures_are_empty_defaults(self):
        for error in (
            ClientError('offline'),
            JSONDecodeError(
                'invalid',
                '',
                0)):
            with self.subTest(error=type(error)):
                self.response.json.side_effect = error
                self.assertEqual(run(ComicVine().fetch_volumes((2127,))), [])
        self.status.report.assert_not_called()

    def test_bulk_invalid_key_and_not_found_propagate(self):
        for code, error in ((100, InvalidKeyValue), (101, VolumeNotMatched)):
            with self.subTest(code=code):
                self.respond([], code)
                with self.assertRaises(error):
                    run(ComicVine().fetch_volumes((2127,)))

    def test_invalid_id_is_rejected_before_http(self):
        with self.assertRaises(VolumeNotMatched):
            run(ComicVine().fetch_volumes(('invalid',)))
        self.session.get.assert_not_awaited()

    def test_empty_collections_do_not_request_http(self):
        cv = ComicVine()
        self.assertEqual(run(cv.fetch_volumes(())), [])
        self.assertEqual(run(cv.fetch_issues(())), [])
        self.session.get.assert_not_awaited()

    def test_issue_groups_50_ids_and_stop_after_initial_page_rate_limit(self):
        self.response.json.side_effect = [
            envelope(
                [issue_response(volume={'id': 1})],
                number_of_total_results=1),
            envelope([],
                     107)]
        result = run(ComicVine().fetch_issues(tuple(range(1, 102))))
        self.assertEqual(result, [issue_result(volume_id=1)])
        filters = [
            call.kwargs['params']['filter']
            for call in self.session.get.await_args_list]
        self.assertEqual(filters, [
            'volume:' + '|'.join(map(str, range(1, 51))),
            'volume:' + '|'.join(map(str, range(51, 101)))
        ])
        self.status.clear.assert_called_once_with(
            StatusType.CV_RATE_LIMIT, 'fetch_issues')
        self.status.report.assert_called_once_with(
            StatusType.CV_RATE_LIMIT, 'fetch_issues')
        self.sleep.assert_not_awaited()

    def test_issue_offsets_groups_of_ten_and_cooldown_preserve_page_order(self):
        self.response.json.side_effect = [
            envelope([issue_response(id=1)], number_of_total_results=1101)
        ] + [envelope([issue_response(id=value)]) for value in range(2, 13)]
        result = run(ComicVine().fetch_issues((2127,)))
        self.assertEqual([i['comicvine_id']
                         for i in result], list(range(1, 13)))
        self.assertEqual([call.kwargs['params'].get('offset')
                          for call in self.session.get.await_args_list],
                         [None] + list(range(100, 1101, 100)))
        self.sleep.assert_awaited_once_with(Constants.CV_BRAKE_TIME * 10)

    def test_failed_offset_keeps_later_pages_and_no_status_report(self):
        self.response.json.side_effect = [
            envelope([issue_response(id=1)], number_of_total_results=201),
            envelope([], 107), envelope([issue_response(id=3)])
        ]
        self.assertEqual([i['comicvine_id']
                          for i in run(ComicVine().fetch_issues((2127,)))],
                         [1, 3])
        self.status.report.assert_not_called()

    def test_cover_request_still_uses_quiet_fail_and_null_fallback(self):
        self.respond([volume_response()])
        self.session.get_content.return_value = b''
        self.assertEqual(run(ComicVine().fetch_volumes((2127,)))[
                         0]['cover'], None)
        self.session.get_content.assert_awaited_once_with(
            'https://example.invalid/cover.jpg', quiet_fail=True)

    def test_cover_fetch_happens_before_issue_fetch_in_bulk_refresh(self):
        events = []

        async def get(url, **kwargs):
            events.append(url.rsplit('/', 2)[-2])
            return self.response

        async def cover(*args, **kwargs):
            events.append('cover')
            return COVER

        self.session.get.side_effect = get
        self.session.get_content.side_effect = cover
        self.response.json.side_effect = [
            envelope([volume_response()]),
            envelope([issue_response()],
                     number_of_total_results=1)]
        cv = ComicVine()
        run(cv.fetch_volumes((2127,)))
        run(cv.fetch_issues((2127,)))
        self.assertEqual(events, ['volumes', 'cover', 'issues'])

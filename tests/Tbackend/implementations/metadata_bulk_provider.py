"""Full legacy structure/request parity for the optional bulk capability."""

import unittest
from asyncio import run
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from fixtures.comicvine_fetch import (ComicVineFetchHarness,
                                      envelope, issue_response)
from fixtures.comicvine_search import volume_response

from backend.base.custom_exceptions import InvalidKeyValue, VolumeNotMatched
from backend.base.definitions import DateType
from backend.implementations.comicvine import ComicVine
from backend.implementations.metadata.legacy import (legacy_issue_metadata,
                                                     legacy_volume_identities,
                                                     legacy_volume_metadata)
from backend.implementations.metadata.provider import (
    MetadataBulkVolumeProvider, MetadataVolumeProvider)
from backend.implementations.metadata.registry import (
    PROVIDERS, get_bulk_volume_provider, get_volume_provider)


class BulkProviderParity(ComicVineFetchHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sleep = self.start_patch('backend.implementations.comicvine.sleep', new_callable=AsyncMock)

    def test_single_and_multi_volume_full_structure_and_request_parity(self):
        for ids, raw in (((2127,), [volume_response()]),
                         ((2127, 9001), [volume_response(id=9001), volume_response()])):
            with self.subTest(ids=ids):
                self.respond(raw)
                self.session.get.reset_mock()
                legacy = run(ComicVine().fetch_volumes(ids))
                requests = self.session.get.await_args_list[:]
                self.session.get.reset_mock()
                neutral = run(get_bulk_volume_provider().fetch_volumes(tuple(map(str, ids))))
                self.assertEqual([legacy_volume_metadata(v) for v in neutral], legacy)
                self.assertEqual(self.session.get.await_args_list, requests)
                self.assertTrue(all(v.issues is None for v in neutral))

    def test_partial_bulk_1001_ids_preserve_calls_order_and_cooldown(self):
        responses = [envelope([], 107)] + [envelope([volume_response(id=i)]) for i in range(2, 12)]
        ids = tuple(range(1, 1002))
        self.response.json.side_effect = responses
        legacy = run(ComicVine().fetch_volumes(ids))
        requests = self.session.get.await_args_list[:]
        covers = self.session.get_content.await_args_list[:]
        sleeps = self.sleep.await_args_list[:]
        self.response.json.side_effect = responses
        self.session.get.reset_mock()
        self.session.get_content.reset_mock()
        self.sleep.reset_mock()
        neutral = run(get_bulk_volume_provider().fetch_volumes(tuple(map(str, ids))))
        self.assertEqual([legacy_volume_metadata(v) for v in neutral], legacy)
        self.assertEqual(self.session.get.await_args_list, requests)
        self.assertEqual(self.session.get_content.await_args_list, covers)
        self.assertEqual(self.sleep.await_args_list, sleeps)
        self.assertEqual(len(requests), 11)

    def test_partial_issue_groups_and_pagination_preserve_full_structures(self):
        responses = [
            envelope([issue_response()], number_of_total_results=201),
            envelope([], 107), envelope([issue_response(id=302)]), envelope([], 107)
        ]
        ids = tuple(range(1, 102))
        self.response.json.side_effect = responses
        legacy = run(ComicVine().fetch_issues(ids))
        requests, statuses = self.session.get.await_args_list[:], self.status.mock_calls[:]
        self.response.json.side_effect = responses
        self.session.get.reset_mock()
        self.status.reset_mock()
        neutral = run(get_bulk_volume_provider().fetch_issues(tuple(map(str, ids))))
        self.assertEqual([legacy_issue_metadata(i) for i in neutral], legacy)
        self.assertEqual(self.session.get.await_args_list, requests)
        self.assertEqual(self.status.mock_calls, statuses)

    def test_empty_and_unfetched_issues_are_distinct(self):
        self.respond([volume_response()])
        metadata = run(get_bulk_volume_provider().fetch_volumes(('2127',)))[0]
        self.assertIsNone(legacy_volume_metadata(metadata)['issues'])
        self.assertEqual(legacy_volume_metadata(replace(metadata, issues=[]))['issues'], [])

    def test_same_client_settings_snapshot_across_volume_and_issue_stages(self):
        self.response.json.side_effect = [envelope([volume_response()]),
                                         envelope([issue_response()], number_of_total_results=1)]
        provider = get_bulk_volume_provider()
        run(provider.fetch_volumes(('2127',)))
        self.settings.date_type = DateType.STORE_DATE
        self.settings.comicvine_api_key = 'test-only-replacement-key'
        issues = run(provider.fetch_issues(('2127',)))
        self.assertEqual(issues[0].date, '2021-01-01')
        params = [c.kwargs['params']['api_key'] for c in self.session.get.await_args_list]
        self.assertEqual(params[0], params[1])

    def test_invalid_key_and_not_found_errors_propagate_by_identity(self):
        client = self.start_patch('backend.implementations.metadata.comicvine.ComicVine').return_value
        for method in ('fetch_volumes', 'fetch_issues'):
            for error in (InvalidKeyValue('comicvine_api_key', 'test-only-key'), VolumeNotMatched()):
                with self.subTest(method=method, error=type(error)):
                    setattr(client, method, AsyncMock(side_effect=error))
                    with self.assertRaises(type(error)) as caught:
                        run(getattr(get_bulk_volume_provider(), method)(('2127',)))
                    self.assertIs(caught.exception, error)

    def test_bulk_reuses_mapping_for_translations_nulls_and_issue_numbers(self):
        self.respond([volume_response(description='<p>French translation of Example.</p>',
                                      aliases=None, publisher=None, start_year=None)])
        legacy = run(ComicVine().fetch_volumes((2127,)))
        neutral = run(get_bulk_volume_provider().fetch_volumes(('2127',)))
        self.assertTrue(neutral[0].translated)
        self.assertEqual([legacy_volume_metadata(v) for v in neutral], legacy)
        self.respond([issue_response(issue_number='1/2', name='TPB')])
        self.response.json.return_value['number_of_total_results'] = 1
        old_issues = run(ComicVine().fetch_issues((2127,)))
        new_issues = run(get_bulk_volume_provider().fetch_issues(('2127',)))
        self.assertEqual([legacy_issue_metadata(i) for i in new_issues], old_issues)

    def test_issue_mapper_rejects_foreign_namespace(self):
        self.respond([issue_response()])
        self.response.json.return_value['number_of_total_results'] = 1
        issue = run(get_bulk_volume_provider().fetch_issues(('2127',)))[0]
        with self.assertRaisesRegex(ValueError, 'ComicVine'):
            legacy_issue_metadata(replace(issue, provider='other'))


class BulkProviderResolution(unittest.TestCase):
    def test_legacy_input_bridge_is_explicit_and_ids_are_strings(self):
        self.assertEqual(legacy_volume_identities((2127, 9001)), ('comicvine', ('2127', '9001')))
        self.assertEqual(legacy_volume_identities(()), ('comicvine', ()))

    def test_optional_bulk_capability_does_not_force_single_only_providers(self):
        single = Mock(spec=MetadataVolumeProvider)
        with patch.dict(PROVIDERS, {'single-only': Mock(return_value=single)}):
            self.assertIs(get_volume_provider('single-only'), single)
            with self.assertRaisesRegex(TypeError, 'bulk volume fetch'):
                get_bulk_volume_provider('single-only')

    def test_default_capability_and_unknown_provider(self):
        self.assertIsInstance(get_bulk_volume_provider(), MetadataBulkVolumeProvider)
        with self.assertRaises(KeyError):
            get_bulk_volume_provider('unregistered')

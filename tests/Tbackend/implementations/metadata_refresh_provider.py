"""End-to-end refresh parity with the original direct client and SQL state."""

import sqlite3
import unittest
from asyncio import run
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from fixtures.comicvine_fetch import envelope, issue_response
from fixtures.comicvine_search import volume_response
from fixtures.metadata_refresh import NOW, RefreshHarness

from backend.implementations.comicvine import ComicVine
from backend.implementations.metadata.comicvine import \
    ComicVineMetadataProvider
from backend.implementations.metadata.persistence import (issue_input,
                                                          volume_input)
from backend.implementations.metadata.registry import (
    get_bulk_volume_provider, get_search_provider)
from backend.implementations.volumes import Library, refresh_and_scan


class RefreshProvider(RefreshHarness, unittest.TestCase):
    def test_manual_refresh_resolves_explicit_legacy_provider_and_two_stages(self):
        real = get_bulk_volume_provider()
        provider = Mock(wraps=real)
        provider.fetch_volumes = AsyncMock(wraps=real.fetch_volumes)
        provider.fetch_issues = AsyncMock(wraps=real.fetch_issues)
        resolver = self.start_patch('backend.implementations.volumes.get_bulk_volume_provider',
                                     return_value=provider)
        self.refresh()
        resolver.assert_called_once_with('comicvine')
        provider.fetch_volumes.assert_awaited_once_with(('2127',))
        provider.fetch_issues.assert_awaited_once_with(('2127',))

    def test_scheduled_refresh_uses_one_provider_for_both_volumes(self):
        self.add_second_volume()
        self.prepare_refresh([volume_response(count_of_issues=2),
                              volume_response(id=9001, count_of_issues=1)],
                             [issue_response(), issue_response(id=302, issue_number='2'),
                              issue_response(id=901, volume={'id': 9001})])
        real = get_bulk_volume_provider()
        resolver = self.start_patch('backend.implementations.volumes.get_bulk_volume_provider',
                                     return_value=real)
        refresh_and_scan()
        resolver.assert_called_once_with('comicvine')
        self.assertEqual(self.session.get.await_count, 2)
        self.assertEqual(self.session.get.await_args_list[0].kwargs['params']['filter'], 'id:2127|9001')
        self.assertEqual(self.session.get.await_args_list[1].kwargs['params']['filter'], 'volume:2127|9001')

    def test_foreign_volume_namespace_rejected_before_volume_writes(self):
        self.respond([volume_response()])
        volume = run(get_bulk_volume_provider().fetch_volumes(('2127',)))[0]
        provider = self.start_patch('backend.implementations.volumes.get_bulk_volume_provider').return_value
        provider.fetch_volumes = AsyncMock(return_value=[replace(volume, provider='other')])
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'ComicVine'):
            self.refresh()
        self.assertEqual(self.snapshot(), before)
        self.commit.assert_not_called()

    def test_foreign_issue_namespace_rejected_before_issue_writes(self):
        real = get_bulk_volume_provider()
        self.respond([issue_response()])
        self.response.json.return_value['number_of_total_results'] = 1
        issue = run(real.fetch_issues(('2127',)))[0]
        self.prepare_refresh([volume_response(name='Volume Committed', count_of_issues=2)])
        real.fetch_issues = AsyncMock(return_value=[replace(issue, provider='other')])
        self.start_patch('backend.implementations.volumes.get_bulk_volume_provider', return_value=real)
        before = self.rows('issues')
        with self.assertRaisesRegex(ValueError, 'ComicVine'):
            self.refresh()
        self.assertEqual(self.rows('issues'), before)
        self.assertEqual(self.rows('volumes')[0]['title'], 'Volume Committed')
        self.assertEqual(self.commit.call_count, 1)

    def test_search_add_persist_refresh_combined_path(self):
        # Start with a new, synthetic identity alongside the fixture's volume.
        self.respond([volume_response(id=9001, name='New Series', count_of_issues=1)])
        candidate = run(get_search_provider().search_volumes('New Series'))[0]
        self.prepare_fetch(volume_response(id=9001, name='New Series', count_of_issues=1),
                           [issue_response(id=901, volume={'id': 9001})])
        identity = Library.add(int(candidate.provider_id), 1, True)
        self.prepare_refresh([volume_response(id=9001, name='Updated Series', count_of_issues=2)],
                             [issue_response(id=901, volume={'id': 9001}),
                              issue_response(id=902, volume={'id': 9001}, issue_number='2')])
        refresh_and_scan(identity)
        row = self.rows('volumes')[1]
        self.assertEqual((row['id'], row['comicvine_id'], row['title']),
                         (identity, 9001, 'Updated Series'))
        self.assertEqual([i['comicvine_id'] for i in self.rows('issues') if i['volume_id'] == identity],
                         [901, 902])

    def test_full_persistence_requests_timestamps_and_errors_match_legacy_client(self):
        self.add_second_volume()
        self.link_file(issue_ids=(1, 2))
        checkpoint = sqlite3.connect(':memory:')
        self.addCleanup(checkpoint.close)
        self.db.backup(checkpoint)
        cases = [
            (self.volume_id, [envelope([volume_response(count_of_issues=2)]),
                              envelope([issue_response(), issue_response(id=302, issue_number='2')],
                                       number_of_total_results=2)]),
            (None, [envelope([volume_response(id=9001, count_of_issues=1), volume_response(count_of_issues=2)]),
                    envelope([issue_response(id=901, volume={'id': 9001}), issue_response(),
                              issue_response(id=302, issue_number='2')], number_of_total_results=3)]),
            (None, [envelope([volume_response(count_of_issues=101)]),
                    envelope([issue_response(name='Partial')], number_of_total_results=101), envelope([], 107)]),
            (self.volume_id, [envelope([volume_response(count_of_issues=1)]),
                              envelope([issue_response(id=302, issue_number='2')], number_of_total_results=1)]),
            (self.volume_id, [envelope([volume_response(count_of_issues=0)]),
                              envelope([], number_of_total_results=0)]),
            (self.volume_id, [envelope([volume_response(name='Committed')]), envelope([], 100)]),
            (None, [envelope([], 107)])
        ]

        def capture(identity, responses):
            from datetime import datetime, timezone
            self.response.json.side_effect = responses
            for mock in (self.session.get, self.session.get_content, self.commit,
                         self.scan, self.pool.starmap, self.status):
                mock.reset_mock()
            error = None
            try:
                # Receipt UTC time is independent of the frozen evaluator clock.
                with patch('backend.internals.classification_provenance.datetime', wraps=datetime) as receipt_clock:
                    receipt_clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                    refresh_and_scan(identity, allow_skipping=False)
            except Exception as exc:
                error = (type(exc), exc.args)
            return (self.snapshot(), error, self.db.in_transaction,
                    self.session.get.await_args_list[:], self.session.get_content.await_args_list[:],
                    self.commit.call_count, self.scan.call_args_list[:],
                    self.pool.starmap.call_args_list[:], self.status.mock_calls[:])

        for identity, responses in cases:
            with self.subTest(identity=identity, responses=responses):
                self.db.rollback()
                checkpoint.backup(self.db)
                # The pre-provider client returns original legacy dicts. Bypass
                # only neutral conversion to compare the unchanged reconciliation
                # with identical transport, initial SQL state and fixed clock.
                with patch('backend.implementations.volumes.get_bulk_volume_provider', return_value=ComicVine()), \
                     patch('backend.implementations.volumes.volume_input', side_effect=lambda x, p: volume_input(ComicVineMetadataProvider._volume_metadata(x), p)), \
                     patch('backend.implementations.volumes.issue_input', side_effect=lambda x, p: issue_input(ComicVineMetadataProvider._issue_metadata(x), p)):
                    legacy = capture(identity, responses)
                self.db.rollback()
                checkpoint.backup(self.db)
                neutral = capture(identity, responses)
                self.assertEqual(neutral, legacy)

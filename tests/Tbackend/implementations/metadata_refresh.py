"""Characterization of existing refresh reconciliation, before abstraction."""

import sqlite3
import unittest
from datetime import timedelta

from aiohttp import ClientError
from fixtures.comicvine_fetch import envelope, issue_response
from fixtures.comicvine_search import volume_response
from fixtures.metadata_refresh import NOW, RefreshHarness

from backend.base.custom_exceptions import InvalidKeyValue, VolumeNotMatched
from backend.base.definitions import SpecialVersion, StatusType
from backend.implementations.volumes import Volume, refresh_and_scan
from backend.internals.provider_authority import AuthorityToken


class MetadataRefresh(RefreshHarness, unittest.TestCase):
    def test_unchanged_metadata_only_updates_timestamp_and_scans(self):
        before = self.rows('volumes'), self.rows('issues')
        expected = before[0]
        expected[0]['last_cv_fetch'] = NOW.timestamp()
        self.refresh(update_websocket=True)
        self.assertEqual(
            (self.rows('volumes'),
             self.rows('issues')),
            (expected, before[1]))
        self.scan.assert_called_once_with(self.volume_id, update_websocket=True,
            expected_authority=AuthorityToken(self.volume_id, 'comicvine', '2127', 0))
        self.assertEqual(self.commit.call_count, 3)

    def test_changed_volume_metadata_alias_translation_cover_and_no_folder_rename(
        self):
        expected = self.rows('volumes')[0]
        description = '<p>French translation of Example Hero.</p>'
        self.prepare_refresh([
                              volume_response(
                                  name='Changed', start_year='2022',
                                  deck='Volume 3',
                                  publisher={'name': 'New Publisher'},
                                  aliases='New Alias\r\nSecond Alias',
                                  description=description, count_of_issues=2,
                                  site_detail_url='https://example.invalid/changed')])
        self.session.get_content.return_value = b'changed-cover'
        expected.update(
            title='Changed',
            year=2022,
            volume_number=3,
            publisher='New Publisher',
            alt_title='New Alias',
            description=description,
            site_url='https://example.invalid/changed',
            last_cv_fetch=NOW.timestamp())
        self.refresh()
        self.assertEqual(self.rows('volumes'), [expected])
        self.assertEqual(
            self.db.execute('SELECT cover FROM volumes_covers').fetchone(),
            (b'changed-cover',))

    def test_aliases_removed_and_null_optional_metadata(self):
        self.prepare_refresh([
                              volume_response(
                                  aliases=None, publisher=None, start_year=None,
                                  description=None, count_of_issues=2)])
        self.session.get_content.return_value = None
        self.refresh()
        row = self.rows('volumes')[0]
        for key in ('alt_title', 'publisher', 'year', 'description'):
            self.assertIsNone(row[key])
        self.assertIsNone(self.db.execute(
            'SELECT cover FROM volumes_covers').fetchone()[0])

    def test_issue_upsert_preserves_local_id_monitoring_and_file_links(self):
        self.link_file()
        self.db.execute('UPDATE issues SET monitored=0 WHERE id=1')
        self.db.commit()
        before = self.snapshot()
        expected = self.rows('issues')[0]
        self.prepare_refresh(
            issues=[
                issue_response(
                    issue_number='1.5',
                    name='Changed Issue',
                    cover_date='2022-02-01',
                    description=None),
                issue_response(
                    id=302,
                    issue_number='2')])
        expected.update(
            issue_number='1.5',
            calculated_issue_number=1.5,
            title='Changed Issue',
            date='2022-02-01',
            description=None)
        self.refresh()
        self.assertEqual(self.rows('issues')[0], expected)
        for table in ('files', 'issues_files'):
            self.assertEqual(self.snapshot()[table], before[table])

    def test_new_issue_inserted_with_parent_monitor_policy(self):
        self.db.execute(
            'UPDATE volumes SET monitor_new_issues=0 WHERE id=?',
            (self.volume_id,))
        self.db.commit()
        self.prepare_refresh([volume_response(count_of_issues=3)], [
            issue_response(), issue_response(id=302, issue_number='2'),
            issue_response(id=303, issue_number='3', name='New Issue')
        ])
        self.refresh()
        issue = self.rows('issues')[-1]
        self.assertEqual(
            (issue['comicvine_id'],
             issue['volume_id'],
             issue['monitored']),
            (303, self.volume_id, False))
        self.assertTrue(self.rows('issues')[0]['monitored'])

    def test_complete_results_delete_missing_issue_and_its_shared_file_links(
            self):
        path = self.link_file(issue_ids=(1, 2))
        self.prepare_refresh([volume_response(count_of_issues=1)],
                             [issue_response(id=302, issue_number='2')])
        self.refresh()
        self.assertEqual([r['comicvine_id']
                         for r in self.rows('issues')], [302])
        self.assertEqual(self.snapshot()['files'], [])
        self.assertEqual(self.snapshot()['issues_files'], [])
        self.assertTrue(path.exists())
        self.assertEqual(self.commit.call_count, 4)

    def test_partial_count_does_not_delete_missing_issues_or_links(self):
        self.link_file()
        before = self.snapshot()
        self.prepare_refresh(issues=[issue_response(id=302, issue_number='2')])
        self.refresh()
        for table in ('issues', 'files', 'issues_files'):
            self.assertEqual(self.snapshot()[table], before[table])

    def test_empty_issue_result_with_positive_count_preserves_issues(self):
        before = self.rows('issues')
        self.prepare_refresh(issues=[])
        self.refresh()
        self.assertEqual(self.rows('issues'), before)
        self.assertEqual(
            self.rows('volumes')[0]['last_cv_fetch'],
            NOW.timestamp())

    def test_zero_advertised_count_retains_existing_keyerror_quirk(self):
        before = self.rows('issues')
        self.prepare_refresh([volume_response(count_of_issues=0)], [])
        with self.assertRaises(KeyError) as caught:
            self.refresh()
        self.assertEqual(caught.exception.args, (2127,))
        self.assertEqual(self.rows('issues'), before)
        self.assertEqual(
            self.rows('volumes')[0]['last_cv_fetch'],
            NOW.timestamp())
        self.assertEqual(self.commit.call_count, 2)
        self.scan.assert_not_called()

    def test_rate_limited_issue_page_keeps_partial_updates_without_deletion(
            self):
        self.response.json.side_effect = [
            envelope([volume_response(count_of_issues=101)]),
            envelope(
                [issue_response(name='Partial Change')],
                number_of_total_results=101),
            envelope([],
                     107)]
        self.refresh()
        self.assertEqual(self.rows('issues')[0]['title'], 'Partial Change')
        self.assertEqual(len(self.rows('issues')), 2)
        self.status.report.assert_not_called()

    def test_issue_initial_rate_limit_returns_empty_and_still_scans(self):
        before = self.rows('issues')
        self.response.json.side_effect = [
            envelope([volume_response(count_of_issues=2)]), envelope([], 107)]
        self.refresh()
        self.assertEqual(self.rows('issues'), before)
        self.status.report.assert_called_once_with(
            StatusType.CV_RATE_LIMIT, 'fetch_issues')
        self.scan.assert_called_once_with(
            self.volume_id, update_websocket=False,
            expected_authority=AuthorityToken(self.volume_id, 'comicvine', '2127', 0))

    def test_volume_rate_limit_or_network_failure_keeps_metadata_but_scans(
        self):
        before = self.snapshot()
        for error in (envelope([], 107), ClientError('offline')):
            with self.subTest(error=type(error)):
                self.response.json.side_effect = [error]
                self.refresh()
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.scan.call_count, 2)
        self.status.report.assert_not_called()

    def test_volume_errors_before_writes_preserve_all_rows(self):
        before = self.snapshot()
        for code, exception in (
            (100, InvalidKeyValue),
                (101, VolumeNotMatched)):
            with self.subTest(code=code):
                self.respond(None, code)
                with self.assertRaises(exception):
                    self.refresh()
                self.assertEqual(self.snapshot(), before)
        self.commit.assert_not_called()
        self.scan.assert_not_called()

    def test_issue_key_failure_keeps_committed_volume_changes(self):
        before = self.rows('issues')
        self.response.json.side_effect = [
            envelope([volume_response(name='Already Committed', count_of_issues=2)]),
            envelope([], 100)
        ]
        with self.assertRaises(InvalidKeyValue):
            self.refresh()
        self.db.rollback()
        self.assertEqual(self.rows('volumes')[0]['title'], 'Already Committed')
        self.assertEqual(
            self.rows('volumes')[0]['last_cv_fetch'],
            NOW.timestamp())
        self.assertEqual(self.rows('issues'), before)
        self.assertEqual(self.commit.call_count, 1)
        self.scan.assert_not_called()

    def test_mid_issue_sql_failure_rolls_back_guarded_issue_stage(
        self):
        original = self.rows('issues')
        self.prepare_refresh(
            issues=[
                issue_response(
                    name='Pending'), issue_response(
                    id=303, issue_number='3', cover_date='bad-date')])
        # SQL failure on the second row, after first row was updated.
        self.db.execute(
            "CREATE TEMP TRIGGER reject_issue BEFORE INSERT ON issues "
            "WHEN NEW.comicvine_id=303 BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.refresh()
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.rows('issues'), original)
        self.assertEqual(
            self.rows('volumes')[0]['last_cv_fetch'],
            NOW.timestamp())

    def test_special_version_recomputed_but_locked_override_preserved(self):
        self.prepare_refresh(
            [volume_response(count_of_issues=1)],
            [issue_response(name='TPB')])
        self.refresh()
        self.assertEqual(
            Volume(
                self.volume_id).vd.special_version,
            SpecialVersion.TPB)
        self.assertEqual(self.rows('issues')[0]['title'], 'TPB')
        self.db.execute(
            "UPDATE volumes SET special_version='hard-cover', special_version_locked=1")
        self.db.commit()
        self.prepare_refresh(
            [volume_response(count_of_issues=1)],
            [issue_response(name='TPB')])
        self.refresh()
        self.assertEqual(
            Volume(
                self.volume_id).vd.special_version,
            SpecialVersion.HARD_COVER)

    def test_issue_read_order_is_date_then_number_not_response_order(self):
        self.prepare_refresh(
            issues=[
                issue_response(
                    id=302,
                    issue_number='2',
                    cover_date='2020-01-01'),
                issue_response()])
        self.refresh()
        self.assertEqual([i.comicvine_id
                          for i in Volume(self.volume_id).get_issues()],
                         [302, 301])

    def test_only_expected_metadata_tables_change_and_schema_is_identical(self):
        before, schema = self.snapshot(), self.schema()
        self.prepare_refresh(
            [volume_response(name='Changed', count_of_issues=2)])
        self.refresh()
        after = self.snapshot()
        self.assertEqual(self.schema(), schema)
        for table in before:
            if table not in ('volumes', 'issues', 'volumes_covers',
                             'volume_external_ids', 'issue_external_ids',
                             'classification_state', 'classification_provenance',
                             'classification_evidence_receipts'):
                self.assertEqual(after[table], before[table], table)
        self.assertEqual(self.db.execute(
            'PRAGMA integrity_check').fetchone(), ('ok',))

    def test_manual_refresh_bypasses_recent_timestamp_and_uses_bulk_endpoint(
            self):
        self.set_timestamp(self.volume_id, NOW.timestamp())
        self.refresh()
        self.assertTrue(
            self.session.get.await_args_list[0].args[0].endswith('/volumes/'))
        self.assertEqual(self.session.get.await_count, 2)

    def test_empty_selection_has_no_transport_or_scan(self):
        refresh_and_scan(999)
        self.session.get.assert_not_awaited()
        self.scan.assert_not_called()
        self.pool_factory.assert_not_called()

    def test_scheduled_selection_skips_recent_and_preserves_oldest_order(self):
        second = self.add_second_volume()
        self.set_timestamp(
            self.volume_id, (NOW - timedelta(days=2)).timestamp())
        self.set_timestamp(second, (NOW - timedelta(days=3)).timestamp())
        self.prepare_refresh([volume_response(id=9001, count_of_issues=1),
                              volume_response(count_of_issues=2)], [])
        refresh_and_scan()
        params = self.session.get.await_args_list[0].kwargs['params']
        self.assertEqual(params['filter'], 'id:9001|2127')
        # Equal issue counts, both <30 days: no /issues request.
        self.assertEqual(self.session.get.await_count, 1)
        self.pool.starmap.assert_called_once_with(self.scan,
            [(second, [], False, False, AuthorityToken(second, 'comicvine', '9001', 0)),
             (self.volume_id, [], False, False, AuthorityToken(self.volume_id, 'comicvine', '2127', 0))])
        self.session.get.reset_mock()
        refresh_and_scan()
        self.session.get.assert_not_awaited()

    def test_thirty_day_boundary_forces_issue_fetch_despite_equal_count(self):
        self.set_timestamp(
            self.volume_id, (NOW - timedelta(days=30)).timestamp())
        refresh_and_scan()
        self.assertEqual(self.session.get.await_count, 2)

    def test_changed_count_fetches_issues_before_thirty_day_cutoff(self):
        self.set_timestamp(
            self.volume_id, (NOW - timedelta(days=2)).timestamp())
        self.prepare_refresh([volume_response(count_of_issues=3)])
        refresh_and_scan()
        self.assertEqual(self.session.get.await_count, 2)

    def test_forced_update_all_fetches_recent_volumes_and_issues(self):
        self.set_timestamp(self.volume_id, NOW.timestamp())
        refresh_and_scan(allow_skipping=False, update_websocket=True)
        self.assertEqual(self.session.get.await_count, 2)
        self.pool.istarmap_unordered.assert_called_once_with(self.scan,
            [(self.volume_id, [], False, True, AuthorityToken(self.volume_id, 'comicvine', '2127', 0))])
        self.socket.emit.assert_called_once()

    def test_partial_bulk_results_leave_missing_volume_unchanged_but_scan_it(
            self):
        second = self.add_second_volume()
        before = self.rows('volumes')[1]
        self.prepare_refresh()
        refresh_and_scan(allow_skipping=False)
        self.assertEqual(self.rows('volumes')[1], before)
        self.assertEqual(
            self.session.get.await_args_list[1].kwargs['params']['filter'],
            'volume:2127')
        self.pool.starmap.assert_called_once_with(self.scan,
            [(self.volume_id, [], False, False, AuthorityToken(self.volume_id, 'comicvine', '2127', 0)),
             (second, [], False, False, AuthorityToken(second, 'comicvine', '9001', 0))])

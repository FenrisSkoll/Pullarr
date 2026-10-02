"""Real schema-53 writes, real Metron mapping, deterministic HTTP only."""

from asyncio import run
from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, patch

from fixtures.comicvine_fetch import LibraryAddHarness
from fixtures.metron import SERIES, issue, issue_summary, page, series_summary

from backend.implementations.metadata.identity_enrichment import \
    IdentityEnrichmentConflict
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.metadata.metron_budget import \
    charge_background_request
from backend.implementations.metadata.metron_client import MetronError
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.naming import get_issue_naming_keys
from backend.implementations.volumes import Library, Volume, refresh_and_scan
from backend.internals.provider_identity import (ExternalIdentity,
                                                 ProviderIdentityDB)
from frontend.metadata import volume_identity_results


class MetronHarness(LibraryAddHarness):
    def setUp(self):
        super().setUp()
        self.settings.metron_api_token = 'unit-not-a-credential'
        self.settings.metron_refresh_requests_per_day = 250
        self.series = deepcopy(SERIES)
        self.issues = [issue(cv_id=901, gcd_id=902), issue(702, '2')]
        for module in ('backend.implementations.metadata.metron',
                       'backend.implementations.metadata.metron_budget',
                       'backend.internals.settings'):
            self.start_patch(module + '.Settings').return_value.sv = self.settings
        for module in ('metron', 'metron_budget', 'identity_enrichment', 'refresh'):
            self.start_patch('backend.implementations.metadata.' + module + '.get_db',
                             side_effect=self.db.cursor)
        self.start_patch('backend.implementations.metadata.metron_budget.commit', side_effect=self.db.commit)
        self.start_patch('backend.implementations.file_matching.scan_files')
        self.start_patch('backend.implementations.metadata.metron.MetronMetadataProvider.cache_directory',
                         return_value=self.root / 'cache')
        self.http = self.start_patch('backend.implementations.metadata.metron_client.Session').return_value.__enter__.return_value
        self.http.get.side_effect = self.respond_metron
        for target, values in (('backend.implementations.metadata.registry.PROVIDERS',
                               {'metron': MetronMetadataProvider}),
                               ('backend.implementations.metadata.metron_client.RATE_STATE', {})):
            p = patch.dict(target, values, clear=target.endswith('RATE_STATE'))
            p.start()
            self.addCleanup(p.stop)

    def respond_metron(self, url, **kwargs):
        if url.endswith('/series/'):
            data = page([series_summary(self.series)])
        elif url.endswith('/series/700/'):
            data = deepcopy(self.series)
        elif url.endswith('/issue_list/'):
            data = page([issue_summary(i) for i in self.issues])
        else:
            identity = int(url.rstrip('/').split('/')[-1])
            data = deepcopy(next(i for i in self.issues if i['id'] == identity))
        response = Mock(status_code=200, headers={})
        response.json.return_value = data
        return response

    def add_metron(self):
        return Library.add_metadata(ProviderVolumeIdentity('metron', '700'), 1, True)

    def state(self):
        return {table: self.db.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                for table in ('volumes', 'issues', 'volume_external_ids', 'issue_external_ids', 'volumes_covers')}


class MetronLifecycle(MetronHarness, TestCase):
    def test_full_add_refresh_enrichment_lifecycle_no_comicvine_calls(self):
        result = run(MetronMetadataProvider().search_volumes('Example'))[0]
        self.assertEqual((result.provider, result.provider_id), ('metron', '700'))
        local = self.add_metron()
        first, deleted = [i.id for i in Volume(local).get_issues()]
        refs = ProviderIdentityDB.issue_identities(first)
        self.assertEqual({r.provider: r.provider_id for r in refs},
                         {'metron': '701', 'comicvine': '901', 'gcd': '902'})
        self.assertTrue(all(r.provenance == 'metron' for r in refs if r.provider != 'metron'))
        ProviderIdentityDB.put_issue_identity(ExternalIdentity(deleted, 'gcd', '903', 'verified'))
        self.db.commit()
        self.series['name'] = 'Changed'
        self.issues = [issue(title='Updated', cv_id=901, gcd_id=902,
                             modified='2026-01-02T00:00:00Z'), issue(703, '3')]
        refresh_and_scan(local)
        retained, created = Volume(local).get_issues()
        self.assertEqual(retained.id, first)
        self.assertEqual(retained.title, 'Updated')
        self.assertEqual(ProviderIdentityDB.issue_identities(first), refs)
        self.assertEqual(ProviderIdentityDB.issue_identities(deleted), [])
        self.assertNotEqual(created.id, first)
        self.assertIsNone(created.comicvine_id)
        self.assertEqual(get_issue_naming_keys(Volume(local).get_data(), created).comicvine_id, '')
        self.assertEqual(get_issue_naming_keys(Volume(local).get_data(), created).issue_comicvine_id, '')
        output = volume_identity_results([Volume(local).get_public_data()], True)[0]
        self.assertIsNone(output['comicvine_id'])
        self.assertEqual(output['metadata_source'], {'provider': 'metron', 'id': '700'})
        self.assertEqual(output['issues'][0]['comicvine_id'], 901)
        self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.session.get.assert_not_called()

    def test_enrichment_conflict_rolls_back_all_metadata_and_preserves_references(self):
        local = self.add_metron()
        before = self.state()
        self.series['name'] = 'Must roll back'
        self.issues[0].update(cv_id=999, modified='2026-01-02T00:00:00Z')
        with self.assertRaises(IdentityEnrichmentConflict):
            refresh_and_scan(local)
        self.assertEqual(self.state(), before)

    def test_partial_fetch_does_not_mutate_domain_or_advance_timestamp(self):
        local = self.add_metron()
        before = self.state()
        self.series['issue_count'] = 3
        with self.assertRaises(MetronError):
            refresh_and_scan(local)
        self.assertEqual(before, self.state())

    def test_empty_complete_snapshot_deletes_owned_identities(self):
        local = self.add_metron()
        self.series['issue_count'] = 0
        self.issues = []
        refresh_and_scan(local)
        self.assertEqual(Volume(local).get_issues(), [])
        self.assertEqual(self.db.execute('SELECT * FROM issue_external_ids').fetchall(), [])

    def test_unchanged_cache_avoids_issue_detail_requests(self):
        local = self.add_metron()
        self.http.get.reset_mock()
        refresh_and_scan(local)
        self.assertEqual(self.http.get.call_count, 2)

    def test_deferred_cold_fetch_resumes_cached_progress_without_partial_writes(self):
        self.settings.metron_refresh_requests_per_day = 3
        with self.assertRaises(MetronError):
            run(MetronMetadataProvider().fetch_volume_scheduled('700'))
        self.assertEqual(self.http.get.call_count, 3)
        self.assertTrue(all(not rows for rows in self.state().values()))
        self.assertEqual([p.name for p in self.root.iterdir()], ['cache'])
        self.db.execute("UPDATE config SET value='0:3' WHERE key='metron_background_usage'")
        self.db.commit()
        self.http.get.reset_mock()
        result = run(MetronMetadataProvider().fetch_volume_scheduled('700'))
        self.assertEqual(len(result.metadata.issues), 2)
        self.assertEqual(self.http.get.call_count, 3)
        self.assertEqual({ref.provider for ref in result.enrichment}, {'comicvine', 'gcd'})
        self.assertTrue(all(not rows for rows in self.state().values()))

    def test_scheduled_dispatch_budget_and_restart_ledger(self):
        local = self.add_metron()
        self.db.execute('UPDATE volume_external_ids SET last_fetch=0 WHERE volume_id=?', (local,))
        self.db.commit()
        self.http.get.reset_mock()
        refresh_and_scan()
        self.assertEqual(self.http.get.call_count, 2)
        self.assertIsNotNone(self.db.execute("SELECT value FROM config WHERE key='metron_background_usage'").fetchone())
        self.settings.metron_refresh_requests_per_day = 2
        self.db.execute('UPDATE volume_external_ids SET last_fetch=0 WHERE volume_id=?', (local,))
        self.db.commit()
        self.http.get.reset_mock()
        refresh_and_scan()
        self.http.get.assert_not_called()
        self.assertEqual(ProviderIdentityDB.resolve_volume_metadata_identity(local, PROVIDERS.keys()).last_fetch, 0)

    def test_100_and_1000_volume_request_pressure_is_bounded(self):
        for volumes in (100, 1000):
            self.db.execute("DELETE FROM config WHERE key='metron_background_usage'")
            self.db.commit()
            requests = 0
            # Warm unchanged series: detail + one manifest page, cached issues.
            for _ in range(volumes * 2):
                try:
                    charge_background_request()
                    requests += 1
                except MetronError:
                    break
            self.assertEqual(requests, min(volumes * 2, 250))
            print('Metron warm scheduled volumes/budgeted requests:', volumes, requests)

    def test_generated_libraries_bound_actual_scheduled_http_requests(self):
        self.series.update(issue_count=0, gcd_id=None)
        self.issues = []
        template = self.add_metron()
        columns = [row[1] for row in self.db.execute('PRAGMA table_info(volumes)') if row[1] != 'id']
        names = ','.join(columns)

        def response(url, **kwargs):
            data = page([]) if url.endswith('/issue_list/') else dict(
                self.series, id=int(url.rstrip('/').split('/')[-1]))
            result = Mock(status_code=200, headers={})
            result.json.return_value = data
            return result

        self.http.get.side_effect = response
        count = 1
        for target in (100, 1000):
            while count < target:
                local = self.db.execute('INSERT INTO volumes (' + names + ') SELECT ' + names
                                        + ' FROM volumes WHERE id=?', (template,)).lastrowid
                ProviderIdentityDB.put_volume_identity(ExternalIdentity(
                    local, 'metron', str(700 + count), 'provider'))
                count += 1
            self.db.execute("UPDATE volume_external_ids SET last_fetch=0 WHERE provider='metron'")
            self.db.execute("DELETE FROM config WHERE key='metron_background_usage'")
            self.db.commit()
            self.http.get.reset_mock()
            refresh_and_scan()
            self.assertEqual(self.http.get.call_count, min(target * 2, 250))
            self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])
            print('Generated Metron volumes/actual scheduled HTTP:', target, self.http.get.call_count)
        self.session.get.assert_not_called()

    def test_volume_reference_is_genuine_and_does_not_select_comicvine(self):
        self.series['cv_id'] = 4567
        local = self.add_metron()
        self.assertEqual(Volume(local).get_data().comicvine_id, 4567)
        self.assertEqual(ProviderIdentityDB.selected_provider(local), 'metron')
        refresh_and_scan(local)
        self.session.get.assert_not_called()

    def test_duplicate_source_identity_fails_without_fetch_or_merge(self):
        from backend.base.custom_exceptions import InvalidKeyValue
        self.add_metron()
        self.http.get.reset_mock()
        with self.assertRaises(InvalidKeyValue):
            self.add_metron()
        self.http.get.assert_not_called()

    def test_retained_references_survive_absence_in_new_provider_response(self):
        local = self.add_metron()
        first = Volume(local).get_issues()[0].id
        before = ProviderIdentityDB.issue_identities(first)
        self.issues[0].update(cv_id=None, gcd_id=None, modified='2026-01-02T00:00:00Z')
        refresh_and_scan(local)
        self.assertEqual(ProviderIdentityDB.issue_identities(first), before)

    def test_auth_rate_and_network_failure_preserve_persisted_state(self):
        from requests import Timeout
        local = self.add_metron()
        before = self.state()
        for status in (401, 403, 404, 429):
            with patch.dict('backend.implementations.metadata.metron_client.RATE_STATE', {}, clear=True):
                self.http.get.side_effect = None
                self.http.get.return_value = Mock(status_code=status, headers={'Retry-After': '60'})
                with self.assertRaises(MetronError):
                    refresh_and_scan(local)
                self.assertEqual(self.state(), before)
        self.http.get.side_effect = Timeout('No actual network')
        with self.assertRaises(MetronError):
            refresh_and_scan(local)
        self.assertEqual(self.state(), before)

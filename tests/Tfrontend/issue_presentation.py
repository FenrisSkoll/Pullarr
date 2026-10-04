"""Real storage and API routes, without network access."""

from copy import deepcopy
from unittest import TestCase

from fixtures.collected_titles import cv_issue_snapshot, record
from fixtures.comicvine_search import FAKE_APP_KEY
from fixtures.metadata_refresh import RefreshHarness
from Tfrontend.issue_presentation_contract import PresentationHarness

from backend.implementations.volumes import Library, Volume, refresh_and_scan
from backend.internals.server import Server
from frontend.metadata import issue_identity_result, volume_identity_results


class PresentationAPI(PresentationHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        self.start_patch('backend.internals.server.WebSocket')
        self.start_patch('backend.internals.server.SimpleQueue')
        self.start_patch('backend.internals.server.MPWebSocketQueue')
        self.app = Server._create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def get(self, path, qualified=True):
        args = {'api_key': FAKE_APP_KEY, 'metadata': str(qualified).lower()}
        response = self.client.get('/api' + path, query_string=args)
        self.assertEqual(response.status_code, 200)
        return response.get_json()['result']

    def test_api_optin_embedded_and_standalone_match_legacy_unchanged(self):
        volume = self.add_fixture()
        issue = volume.get_issues()[0]
        legacy = self.get('/volumes/%d' % volume.id, False)
        enriched = self.get('/volumes/%d' % volume.id)
        expected = enriched['issues'][0]
        self.assertEqual(expected['title'], 'TPB')
        self.assertEqual(expected['display_title'], volume.get_data().title)
        self.assertEqual(expected['display_title_source'], 'parent_volume_context')
        self.assertEqual(self.get('/issues/%d' % issue.id), expected)
        self.assertEqual(self.get('/issues/%d' % issue.id, False), legacy['issues'][0])
        projected = deepcopy(enriched)
        for row in [projected] + projected['issues']:
            self.assertEqual(row['description_text'], row['description'])  # fixture is already plain text
            for key in ('metadata_source', 'external_ids', 'display_title', 'display_title_source', 'description_text'):
                row.pop(key, None)
        self.assertEqual(projected, legacy)
        self.assertEqual(volume.get_issues()[0].title, 'TPB')

    def test_hc_real_persistence_and_no_input_mutation(self):
        for prefix in ('halloween-hc', 'overture-deluxe', 'rai'):
            key = prefix + '-cv-volume'
            self.prepare_fetch(record(key), cv_issue_snapshot(key, []))
            volume = Volume(Library.add(record(key)['id'], 1, True))
            raw = volume.get_public_data()
            before = deepcopy(raw)
            output = volume_identity_results([raw], True)[0]
            self.assertEqual(raw, before)
            self.assertEqual(output['issues'][0]['title'], 'HC')
            self.assertEqual(output['issues'][0]['display_title'], raw['title'])
            self.assertEqual(output['issues'][0]['display_title_source'], 'parent_volume_context')
            self.assertEqual(volume.get_issues()[0].title, 'HC')

    def test_standalone_uses_full_volume_count_not_single_response_count(self):
        volume = self.add_fixture()
        self.db.execute('INSERT INTO issues (volume_id,comicvine_id,issue_number,calculated_issue_number,title,monitored) VALUES (?,?,?,?,?,?)',
                        (volume.id, 999999, '2', 2.0, 'TPB', 1))
        for issue in volume.get_issues():
            result = issue_identity_result(issue, True)
            self.assertEqual(result['display_title'], 'TPB')
            self.assertEqual(result['display_title_source'], 'existing_issue_title')

    def test_thousand_issue_serialization_adds_zero_queries_or_writes(self):
        volume = self.add_fixture()
        self.db.executemany('INSERT INTO issues (volume_id,comicvine_id,issue_number,calculated_issue_number,title,monitored) VALUES (?,?,?,?,?,?)',
                            [(volume.id, 2000000 + i, str(i), float(i), 'TPB', 1) for i in range(2, 1001)])
        raw = volume.get_public_data()
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            output = volume_identity_results([raw], True)[0]
        finally:
            self.db.set_trace_callback(None)
        # Same two existing identity SELECTs; presentation is CPU-local.
        self.assertEqual(len(statements), 2)
        self.assertTrue(all(s.lstrip().upper().startswith('SELECT') for s in statements))
        self.assertEqual(len(output['issues']), 1000)
        self.assertTrue(all(i['display_title'] == 'TPB' for i in output['issues']))

    def test_volume_page_renders_without_provider_request(self):
        volume = self.add_fixture()
        response = self.client.get('/volumes/%d' % volume.id)
        self.assertEqual(response.status_code, 200)

    def test_null_output_and_single_issue_context_read(self):
        volume = self.add_fixture()
        self.db.execute('UPDATE issues SET title=NULL WHERE volume_id=?', (volume.id,))
        issue = volume.get_issues()[0]
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            result = issue_identity_result(issue, True)
        finally:
            self.db.set_trace_callback(None)
        self.assertIsNone(result['title'])
        self.assertIsNone(result['display_title'])
        self.assertEqual(result['display_title_source'], 'unknown')
        self.assertEqual(len(statements), 3)  # two identity reads + one context
        self.assertTrue(all(s.lstrip().upper().startswith('SELECT') for s in statements))


class PresentationRefresh(RefreshHarness, TestCase):
    def test_refresh_retains_provider_title_and_presentation_is_computed(self):
        raw_volume = record('cyberpunk-cv-collected-volume')
        raw_issue = record('cyberpunk-cv-collected-issue')
        self.prepare_fetch(raw_volume, [raw_issue])
        volume = Volume(Library.add(raw_volume['id'], 1, True))
        folder = volume.get_data().folder
        before = volume_identity_results([volume.get_public_data()], True)[0]['issues'][0]
        self.prepare_refresh([raw_volume], [raw_issue])
        refresh_and_scan(volume.id, allow_skipping=False)
        after = volume_identity_results([volume.get_public_data()], True)[0]['issues'][0]
        self.assertEqual(before, after)
        self.assertEqual(after['title'], 'TPB')
        self.assertEqual(after['display_title'], raw_volume['name'])
        self.assertEqual(after['display_title_source'], 'parent_volume_context')
        self.assertEqual(volume.get_data().folder, folder)

"""Real graph -> explicit claim -> selected file, with offline/API boundaries."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from Tbackend.implementations import reprint_graph as previous

from backend.base.content_claims import (ClaimKind, ContentConflict,
                                         PublicationRef)
from backend.internals.content_claims import (apply_coverage, claim_history,
                                              claim_preview, confirm_claim,
                                              contents, coverage_preview,
                                              retire, retirement_preview)
from backend.internals.issue_ownership import load_ownership


class CollectedLifecycleTests(previous.GcdLifecycleHarness, TestCase):
    sync = previous.GraphLifecycleTests.sync

    def setUp(self):
        super().setUp()
        self.local = self.add_gcd()
        self.iid = self.db.execute('SELECT id FROM issues').fetchone()[0]
        temporary = TemporaryDirectory(prefix='kapowarr-content-graph-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'catalog.sqlite'
        self.catalog_db = previous.fixture(self.path)
        self.addCleanup(self.catalog_db.close)
        self.catalog_db.execute('UPDATE gcd_issue SET series_id=1 WHERE id=2')
        # Catalog source 2 -> local target 1, all four legitimate forms.
        self.catalog_db.execute('''UPDATE gcd_reprint SET origin_issue_id=2,target_issue_id=1,
            origin_id=CASE WHEN target_id IS NULL THEN NULL ELSE 200 END,
            target_id=CASE WHEN origin_id IS NULL THEN NULL ELSE 100 END,
            notes='complete full entire issue' ''')
        self.catalog_db.commit()
        self.sync()
        self.file = self.db.execute("INSERT INTO files(filepath,size) VALUES('/fixture/collected.cbz',1)").lastrowid
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,?)', (self.file, self.iid))
        self.db.commit()
        self.c = self.db.cursor()
        self.source = PublicationRef('gcd', '2')

    def confirm(self):
        preview = claim_preview(self.c, self.iid, self.source, ClaimKind.COMPLETE)
        self.assertEqual(preview['evidence_outcome'], 'multiple_reprint_edges')
        return confirm_claim(self.c, self.iid, self.source, ClaimKind.COMPLETE, preview['preview_token'])

    def add_source(self):
        iid = self.db.execute("INSERT INTO issues(volume_id,issue_number,monitored) VALUES(?,'1A',1)", (self.local,)).lastrowid
        self.db.execute("INSERT INTO issue_external_ids VALUES(?,'gcd','2','provider')", (iid,))
        return iid

    def test_external_source_later_mapping_explicit_apply_and_graph_drift(self):
        requests = len(self.fake.requests)
        claim = self.confirm()
        historical = claim_history(self.c, claim)
        self.assertEqual(len(historical['evidence']), 4)
        with self.assertRaises(ContentConflict):
            coverage_preview(self.c, self.iid, self.file, [claim])
        source_id = self.add_source()
        self.assertFalse(load_ownership(self.c, issue_ids=(source_id,))[source_id]['owned'])
        preview = coverage_preview(self.c, self.iid, self.file, [claim])
        apply_coverage(self.c, self.iid, self.file, [claim], preview['preview_token'])
        self.assertEqual(load_ownership(self.c, issue_ids=(source_id,))[source_id]['state'], 'collected')
        # Current edge removal cannot erase immutable operator evidence/coverage.
        self.catalog_db.execute('DELETE FROM gcd_reprint')
        self.catalog_db.commit()
        self.db.commit()
        self.sync()
        self.assertEqual(claim_history(self.c, claim), historical)
        current = contents(self.c, self.iid)
        self.assertEqual(current['claims'][0]['evidence_no_longer_current'], 4)
        self.assertTrue(current['coverage'][0]['valid'])
        self.assertEqual(len(self.fake.requests), requests)
        self.assertEqual(self.db.execute('SELECT issue_id FROM issues_files').fetchall()[0][0], self.iid)

    def test_stale_graph_preview_rejects_and_no_heuristic_cross_provider_mapping(self):
        preview = claim_preview(self.c, self.iid, self.source, ClaimKind.COMPLETE)
        self.db.execute("UPDATE bibliographic_reprint_edges SET origin_story=NULL WHERE provider_id='40'")
        with self.assertRaises(ContentConflict):
            confirm_claim(self.c, self.iid, self.source, ClaimKind.COMPLETE, preview['preview_token'])
        self.add_source()
        self.db.execute("UPDATE volumes SET metadata_provider='metron' WHERE id=?", (self.local,))
        from backend.internals.content_claims import exact_local
        self.assertIsNone(exact_local(self.c, self.source))

    def test_api_auth_preview_confirm_apply_revoke_and_get_readonly(self):
        from fixtures.comicvine_search import FAKE_APP_KEY

        from backend.internals.server import Server
        source_id = self.add_source()
        self.start_patch('frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.get_db', side_effect=self.db.cursor)
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        for name in ('WebSocket', 'SimpleQueue', 'MPWebSocketQueue'):
            self.start_patch('backend.internals.server.' + name)
        app = Server._create_app()
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        client = app.test_client()
        args = {'api_key': FAKE_APP_KEY}
        route = f'/api/issues/{self.iid}/contents'
        body = {'source_provider': 'gcd', 'source_provider_id': '2', 'kind': 'complete_issue_containment'}
        self.assertEqual(client.post(route + '/claim-confirm', json=body).status_code, 401)
        self.assertEqual(client.get(route + '/claim-confirm', query_string=args).status_code, 405)
        before = self.db.total_changes
        self.assertEqual(client.get(route, query_string=args).status_code, 200)
        self.assertEqual(self.db.total_changes, before)
        def post(path, payload):
            response = client.post(path, query_string=args, json=payload)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            return response.json['result']
        for bad in ({'path': '../../library.cbz'}, {'file_id': '/etc/passwd'}, {'source_provider_id': 'not-in-graph'}):
            self.assertNotEqual(client.post(route + '/claim-preview', query_string=args, json=bad).status_code, 200)
        preview = post(route + '/claim-preview', body)
        claim = post(route + '/claim-confirm', dict(body, preview_token=preview['preview_token']))['claim_id']
        self.assertFalse(load_ownership(self.c, issue_ids=(source_id,))[source_id]['owned'])
        coverage = {'file_id': self.file, 'claim_ids': [claim]}
        preview = post(route + '/coverage-preview', coverage)
        post(route + '/coverage-apply', dict(coverage, preview_token=preview['preview_token']))
        owned = client.get(f'/api/issues/{source_id}/ownership', query_string=args).json['result']['issues'][0]
        self.assertEqual(owned['state'], 'collected')
        self.assertEqual(owned['direct_files'], [])
        self.assertEqual(client.get(f'/api/content-claims/{claim}', query_string=args).status_code, 200)
        preview = post(f'/api/content-claims/{claim}/retire-preview', {})
        post(f'/api/content-claims/{claim}/retire', {'preview_token': preview['preview_token']})
        self.assertFalse(load_ownership(self.c, issue_ids=(source_id,))[source_id]['owned'])
        page = client.get(f'/volumes/{self.local}')
        self.assertIn(b'issue-contents-content', page.data)

    def test_offline_wanted_counts_only_applied_coverage(self):
        import sqlite3

        from backend.internals.wanted import WantedStore
        source_id = self.add_source()
        claim = self.confirm()
        self.db.commit()
        with TemporaryDirectory(prefix='kapowarr-content-wanted-') as folder:
            path = Path(folder) / 'wanted.sqlite'
            disk = sqlite3.connect(path)
            self.db.backup(disk)
            disk.close()
            store = WantedStore(path)
            try:
                self.assertTrue(store.eligible((source_id,)))
                cursor = store.db.cursor()
                preview = coverage_preview(cursor, self.iid, self.file, [claim])
                apply_coverage(cursor, self.iid, self.file, [claim], preview['preview_token'])
                self.assertFalse(store.eligible((source_id,)))
                self.assertNotIn(source_id, {r['id'] for r in store.due()})
                preview = retirement_preview(cursor, claim)
                retire(cursor, claim, preview['preview_token'])
                self.assertTrue(store.eligible((source_id,)))
            finally:
                store.close()

"""Catalog -> real local GCD authority -> graph/API, with core state isolation."""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from Tbackend.implementations.gcd_catalog import fixture
from Tbackend.implementations.gcd_lifecycle import GcdLifecycleHarness

from backend.implementations.metadata.gcd_catalog import Catalog, CatalogError
from backend.internals.reprint_graph import (issue_graph, issue_stories,
                                             persist, seeds)


class GraphLifecycleTests(GcdLifecycleHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.local = self.add_gcd()
        self.iid = self.db.execute('SELECT id FROM issues').fetchone()[0]
        temporary = TemporaryDirectory(prefix='kapowarr-graph-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'catalog.sqlite'
        self.catalog_db = fixture(self.path)
        self.addCleanup(self.catalog_db.close)

    def sync(self):
        local = seeds(self.db.cursor())
        graph = Catalog(self.path).acquire({i: data[0] for i, data in local.items()})
        receipt = persist(self.db.cursor(), graph, local)
        self.db.commit()
        return receipt

    def view(self, **kwargs):
        return issue_graph(self.db.cursor(), self.iid, **kwargs)

    def protected(self):
        return {r[0]: self.db.execute('SELECT * FROM "' + r[0] + '" ORDER BY rowid').fetchall()
            for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'bibliographic_%'")}

    def test_end_to_end_offline_graph_does_not_change_core_or_rest(self):
        before = self.protected()
        requests = len(self.fake.requests)
        receipt = self.sync()
        value = self.view()
        self.assertEqual(receipt['edges'], 4)
        self.assertEqual(len(value['outgoing']), 4)
        self.assertEqual(len(value['incoming']), 0)
        self.assertEqual([i['local_issue_id'] for i in value['issues']], [self.iid, None])
        self.assertEqual(value['semantics'], 'material_reprint_evidence_only')
        self.assertEqual(before, self.protected())
        self.assertEqual(len(self.fake.requests), requests)
        self.catalog_db.close()
        self.path.unlink()
        self.assertEqual(value, self.view())
        with self.assertRaises(CatalogError):
            self.sync()
        self.assertEqual(value, self.view())

    def test_idempotent_semantics_edge_add_remove_and_endpoint_change(self):
        first = self.sync()
        second = self.sync()
        self.assertEqual(first['digest'], second['digest'])
        self.assertNotEqual(first['snapshot_id'], second['snapshot_id'])
        self.catalog_db.execute('DELETE FROM gcd_reprint WHERE id=41')
        self.catalog_db.execute('UPDATE gcd_reprint SET origin_id=NULL WHERE id=40')
        self.catalog_db.execute("INSERT INTO gcd_reprint VALUES(44,2,1,NULL,NULL,'new','2026')")
        self.catalog_db.commit()
        self.sync()
        view = self.view()
        self.assertEqual([r['provider_id'] for r in view['outgoing']], ['40', '42', '43'])
        self.assertEqual(view['outgoing'][0]['shape'], 'issue_to_story')
        self.assertEqual(view['incoming'][0]['provider_id'], '44')

    def test_soft_deletion_not_active_and_no_identity_churn(self):
        self.sync()
        self.catalog_db.execute('UPDATE gcd_story SET deleted=1 WHERE id=100')
        self.catalog_db.execute("UPDATE gcd_creator SET gcd_official_name='Renamed' WHERE id=10")
        self.catalog_db.commit()
        self.sync()
        value = self.view()
        self.assertFalse(value['outgoing'][0]['active'])
        self.assertTrue(value['outgoing'][-1]['active'])
        self.assertEqual(value['credits'][0]['creator_id'], '10')
        self.assertEqual(value['credits'][0]['creator_name'], 'Renamed')

    def test_injected_persistence_failure_rolls_back_entire_graph(self):
        self.sync()
        before = tuple(self.db.iterdump())
        self.db.execute('''CREATE TEMP TRIGGER reject_graph BEFORE INSERT ON bibliographic_reprint_edges
            BEGIN SELECT RAISE(ABORT,'injected'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.sync()
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_failed_acquisition_retains_previous_graph(self):
        self.sync()
        before = tuple(self.db.iterdump())
        self.catalog_db.execute('DELETE FROM gcd_story WHERE id=100')
        self.catalog_db.commit()
        with self.assertRaises(CatalogError):
            self.sync()
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_local_mapping_and_authority_are_not_cross_references(self):
        self.sync()
        self.db.execute("INSERT INTO issues(volume_id,issue_number) VALUES(?,'Annual')", (self.local,))
        new_id = self.db.execute('SELECT MAX(id) FROM issues').fetchone()[0]
        self.db.execute("INSERT INTO issue_external_ids VALUES(?,'gcd','2','provider')", (new_id,))
        self.assertEqual(self.view()['issues'][1]['local_issue_id'], new_id)
        self.db.execute("UPDATE volumes SET metadata_provider='metron' WHERE id=?", (self.local,))
        self.assertEqual(seeds(self.db.cursor()), {})
        self.assertFalse(self.view()['available'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bibliographic_reprint_edges').fetchone()[0], 4)

    def test_paginated_evidence_bounded_queries_and_all_stories_not_containment(self):
        self.sync()
        queries = []
        self.db.set_trace_callback(queries.append)
        value = self.view(limit=2)
        self.db.set_trace_callback(None)
        self.assertEqual(value['next_offset'], 2)
        self.assertLessEqual(sum(q.lstrip().upper().startswith('SELECT') for q in queries), 7)
        self.assertEqual(queries[0], 'SAVEPOINT graph_view')
        self.assertEqual(queries[-1], 'RELEASE graph_view')
        page = self.view(offset=2, limit=2)
        self.assertIsNone(page['next_offset'])
        self.assertEqual(page['outgoing'][-1]['shape'], 'issue_to_issue')
        self.assertNotIn('contains', repr(value))
        self.assertNotIn('coverage', value)

    def test_authenticated_api_saved_path_only_and_no_get_mutation(self):
        from fixtures.comicvine_search import FAKE_APP_KEY

        from backend.internals.server import Server
        self.sync()
        self.start_patch('frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.get_db', side_effect=self.db.cursor)
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        for name in ('WebSocket', 'SimpleQueue', 'MPWebSocketQueue'):
            self.start_patch('backend.internals.server.' + name)
        app = Server._create_app()
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        client = app.test_client()
        args = dict(api_key=FAKE_APP_KEY)
        self.assertEqual(client.post('/api/settings/gcd/catalog/sync', json={}).status_code, 401)
        for payload in ({'path': '../../private.db'}, {'url': 'https://example.com'}, {'enabled': True}):
            self.assertEqual(client.post('/api/settings/gcd/catalog/sync', query_string=args, json=payload).status_code, 400)
        before = self.db.total_changes
        response = client.get(f'/api/issues/{self.iid}/reprints', query_string=args)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['result']['schema'], 'issue-reprint-graph/v1')
        self.assertNotIn(str(self.path), response.get_data(as_text=True))
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(client.get(f'/api/issues/{self.iid}/reprints', query_string=dict(args, offset='-1')).status_code, 400)
        page = client.get(f'/volumes/{self.local}')
        self.assertIn(b'issue-reprints-content', page.data)

    def test_production_explicit_sync_service_and_connection_test(self):
        from types import SimpleNamespace

        from backend.features.catalog_graph import operate, status

        # Saved slash spelling need not equal WindowsPath's display spelling.
        settings = SimpleNamespace(gcd_catalog_enabled=True, gcd_catalog_path=self.path.as_posix())
        with patch('backend.features.catalog_graph.Settings') as config, patch(
                'backend.features.catalog_graph.get_db', side_effect=self.db.cursor):
            config.return_value.sv = settings
            before = self.db.total_changes
            self.assertTrue(operate(sync=False)['readable'])
            self.assertEqual(before, self.db.total_changes)
            acquire = Catalog.acquire
            def guarded(catalog, scope):
                self.assertFalse(self.db.in_transaction)
                return acquire(catalog, scope)
            with patch.object(Catalog, 'acquire', guarded):
                self.assertEqual(operate(sync=True)['edges'], 4)
            self.assertIsNotNone(status()['last_sync'])
            settings.gcd_catalog_enabled = False
            with self.assertRaises(CatalogError):
                operate(sync=True)
            self.assertTrue(self.view()['available'])

    def test_no_edge_story_inventory_and_stale_sync_refusal(self):
        from backend.base.reprint_graph import GraphConflict
        local = seeds(self.db.cursor())
        graph = Catalog(self.path).acquire({'1': '1'})
        self.sync()
        with self.assertRaises(GraphConflict):
            persist(self.db.cursor(), graph, local, revision=None)
        self.catalog_db.execute('DELETE FROM gcd_reprint')
        self.catalog_db.commit()
        self.sync()
        self.assertEqual(self.view()['outgoing'], [])
        value = issue_stories(self.db.cursor(), self.iid)
        self.assertEqual([r['provider_id'] for r in value['stories']], ['100'])
        self.assertEqual(len(value['credits']), 2)

    def test_graph_never_satisfies_wanted_even_when_target_file_is_owned(self):
        from backend.internals.wanted import WantedStore
        self.catalog_db.execute('UPDATE gcd_issue SET series_id=1 WHERE id=2')
        self.catalog_db.commit()
        target = self.db.execute("INSERT INTO issues(volume_id,issue_number,monitored) VALUES(?,'Annual',1)", (self.local,)).lastrowid
        self.db.execute("INSERT INTO issue_external_ids VALUES(?,'gcd','2','provider')", (target,))
        file_id = self.db.execute("INSERT INTO files(filepath,size) VALUES('/disposable/trade.cbz',1)").lastrowid
        self.db.execute('INSERT INTO issues_files(issue_id,file_id) VALUES(?,?)', (target, file_id))
        self.db.commit()
        self.sync()
        path = self.path.parent / 'wanted.sqlite'
        connection = sqlite3.connect(path)
        try:
            self.db.backup(connection)
        finally:
            connection.close()
        store = WantedStore(path)
        try:
            self.assertTrue(store.eligible((self.iid,)))
            self.assertFalse(store.eligible((target,)))
            self.assertIn(self.iid, {r['id'] for r in store.due()})
        finally:
            store.close()

    def test_large_fanout_page_is_bounded(self):
        self.catalog_db.executemany("INSERT INTO gcd_reprint VALUES(?,1,2,NULL,NULL,'','2026')",
                                   ((i,) for i in range(1000, 2100)))
        self.catalog_db.commit()
        self.sync()
        first = self.view()
        self.assertEqual(len(first['outgoing']), 100)
        self.assertEqual(first['next_offset'], 100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bibliographic_reprint_edges').fetchone()[0], 1104)

    def test_graph_page_plan_uses_both_scoped_endpoint_indexes(self):
        from backend.internals.reprint_graph import EDGE_PAGE
        self.sync()
        plan = self.db.execute('EXPLAIN QUERY PLAN ' + EDGE_PAGE, ('gcd', '1', 'gcd', '1', 100, 0)).fetchall()
        details = '\n'.join(r[3] for r in plan)
        self.assertIn('graph_origin_issue', details)
        self.assertIn('graph_target_issue', details)
        self.assertNotIn('SCAN bibliographic_reprint_edges', details)

    def test_batch_and_memory_diagnostics(self):
        import tracemalloc
        from time import perf_counter

        # Remove fixture graph, keep complete empty credit tables and a single
        # external endpoint; grow only the explicitly managed GCD seed scope.
        self.catalog_db.execute('DELETE FROM gcd_reprint')
        self.catalog_db.execute('DELETE FROM gcd_story_credit')
        self.catalog_db.execute('DELETE FROM gcd_story')
        self.catalog_db.execute('DELETE FROM gcd_issue WHERE id!=1')
        self.catalog_db.execute("INSERT INTO gcd_issue VALUES(999999,2,'External','',0)")
        previous = 0
        reports = []
        for count in (10, 100, 1000):
            for i in range(previous + 1, count + 1):
                if i != 1:
                    self.catalog_db.execute('INSERT INTO gcd_issue VALUES(?,1,?,\'\',0)', (i, str(i)))
                    iid = self.db.execute('INSERT INTO issues(volume_id,issue_number) VALUES(?,?)', (self.local, str(i))).lastrowid
                    self.db.execute("INSERT INTO issue_external_ids VALUES(?,'gcd',?,'provider')", (iid, str(i)))
                self.catalog_db.execute('INSERT INTO gcd_story VALUES(?,?,\'Story\',1,0)', (i, i))
                self.catalog_db.execute("INSERT INTO gcd_reprint VALUES(?,?,999999,?,NULL,'','2026')", (i, i, i))
            self.db.commit()
            self.catalog_db.commit()
            local = seeds(self.db.cursor())
            tracemalloc.start()
            start = perf_counter()
            graph = Catalog(self.path).acquire({i: value[0] for i, value in local.items()})
            acquired = perf_counter() - start
            queries = []
            self.db.set_trace_callback(queries.append)
            start = perf_counter()
            receipt = persist(self.db.cursor(), graph, local)
            self.db.commit()
            persisted = perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.db.set_trace_callback(None)
            reads = sum(q.lstrip().upper().startswith('SELECT') for q in queries)
            self.assertEqual(receipt['edges'], count)
            self.assertLess(graph.select_count, 40)
            self.assertLess(reads, 12)
            reports.append((count, graph.select_count, reads, round(acquired, 4), round(persisted, 4), peak))
            previous = count
        print('Catalog seeds/SELECTs/app SELECTs/acquire sec/persist sec/Python peak:', reports)

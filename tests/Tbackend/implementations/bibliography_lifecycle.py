"""Actual provider HTTP, add/refresh transaction and local opt-in transport."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from Tbackend.implementations.gcd_bibliography import story
from Tbackend.implementations.gcd_lifecycle import GcdLifecycleHarness

from backend.implementations.metadata.gcd_client import GcdError
from backend.implementations.volumes import Volume, refresh_and_scan
from backend.internals.bibliography import detail, summaries


class BibliographyLifecycleTests(GcdLifecycleHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.fake.issues['1'].update(isbn='0-306-40615-2', barcode='abc-123', page_count='32.00',
                                     story_set=[story()], cover='https://images.comics.org/img/gcd/covers_by_id/1/w400/1.jpg')
        self.fake.series.update(binding='hardcover', publishing_format='collection')

    def loaded(self):
        iid = self.db.execute('SELECT id FROM issues ORDER BY id').fetchone()[0]
        return detail(self.db.cursor(), iid)

    def test_add_offline_opt_in_no_extra_requests(self):
        local = self.add_gcd()
        self.assertEqual(len(self.fake.requests), 4)
        before = len(self.fake.requests)
        value = self.loaded()
        self.assertEqual(value['edition']['isbn'], '0-306-40615-2')
        self.assertEqual(value['edition']['page_count_numeric'], '32')
        self.assertEqual(value['publication']['binding'], 'hardcover')
        self.assertEqual(value['stories'][0]['credits'][0]['role'], 'colors')
        self.assertIsNone(value['stories'][0]['provider_story_id'])
        self.assertEqual(value['schema'], 'issue-bibliography/v1')
        self.assertEqual(summaries(self.db.cursor(), local)[value['issue_id']]['story_count'], 1)
        public = Volume(local).get_public_data(rich_issues=True)
        self.assertNotIn('bibliography', public['issues'][0])
        self.assertNotIn('stories', public['issues'][0])
        self.assertEqual(len(self.fake.requests), before)
        self.scan.assert_not_called()
        self.process.assert_not_called()

    def test_refresh_retains_sets_and_noop_is_idempotent(self):
        local = self.add_gcd()
        old = self.loaded()
        refresh_and_scan(local)
        self.assertEqual(self.loaded()['story_set']['id'], old['story_set']['id'])
        self.fake.issues['1']['story_set'][0].update(title='Changed', script='Other')
        self.fake.issues['1'].update(isbn='9780306406157', page_count='48')
        refresh_and_scan(local)
        new = self.loaded()
        self.assertEqual(new['issue_id'], old['issue_id'])
        self.assertEqual(new['edition']['page_count'], '48')
        self.assertEqual(len(new['retained_sets']), 2)
        self.assertEqual(detail(self.db.cursor(), old['issue_id'], old['story_set']['id'])['stories'], old['stories'])
        self.fake.issues['1']['story_set'] = []
        refresh_and_scan(local)
        self.assertEqual(self.loaded()['stories'], [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM story_observations').fetchone()[0], 2)

    def test_unavailable_invalid_and_explicit_empty_scalar_semantics(self):
        local = self.add_gcd()
        del self.fake.issues['1']['isbn']
        self.fake.issues['1']['barcode'] = ['invalid']
        refresh_and_scan(local)
        value = self.loaded()
        self.assertEqual(value['edition']['isbn'], '0-306-40615-2')
        self.assertEqual(value['edition']['barcode'], 'abc-123')
        self.assertIn('isbn:unavailable', value['diagnostics'])
        self.fake.issues['1']['isbn'] = ''
        refresh_and_scan(local)
        self.assertEqual(self.loaded()['edition']['isbn'], '')
        self.assertIsNone(self.loaded()['edition']['isbn_normalized'])

    def test_variant_independent_and_remote_missing_preserved(self):
        row = deepcopy(self.fake.issues['1'])
        row.update(api_url=self.fake.base + 'issue/2/', variant_of=self.fake.base + 'issue/1/',
                   number='1A', page_count='40', isbn=None, story_set=[], key_date='2022-05-01',
                   cover='https://images.comics.org/img/gcd/covers_by_id/1/w400/2.jpg')
        self.fake.issues['2'] = row
        self.fake.series['active_issues'].append(row['api_url'])
        local = self.add_gcd()
        rows = self.db.execute('SELECT id FROM issues ORDER BY id').fetchall()
        base, variant = [detail(self.db.cursor(), r[0]) for r in rows]
        self.assertIsNone(variant['edition']['isbn'])
        self.assertEqual(variant['edition']['page_count'], '40')
        self.assertNotEqual(base['issue_id'], variant['issue_id'])
        self.assertNotEqual(base['edition']['cover_reference'], variant['edition']['cover_reference'])
        self.assertNotEqual(base['dates'], variant['dates'])
        self.assertIsNotNone(variant['variant_of'])
        self.fake.series['active_issues'].pop()
        refresh_and_scan(local)
        self.assertEqual(variant, detail(self.db.cursor(), variant['issue_id']))
        with self.assertRaises(ValueError):
            detail(self.db.cursor(), base['issue_id'], variant['story_set']['id'])

    def test_malformed_container_and_local_failure_are_atomic(self):
        local = self.add_gcd()
        before = self.loaded()
        core = self.state()
        self.fake.issues['1'].update(story_set={}, number='10')
        with self.assertRaises(GcdError):
            refresh_and_scan(local)
        self.assertEqual(self.loaded(), before)
        self.assertEqual(self.state(), core)
        self.fake.issues['1']['story_set'] = [story()]
        from backend.internals.bibliography import persist_bibliography
        def fail(*args):
            persist_bibliography(*args)
            raise RuntimeError('bibliography checkpoint')
        with patch('backend.internals.bibliography.persist_bibliography', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'bibliography checkpoint'):
                refresh_and_scan(local)
        self.assertEqual(self.loaded(), before)
        self.assertEqual(self.state(), core)

    def test_story_reads_bounded_and_summary_does_not_load_text(self):
        local = self.add_gcd()
        counts = []
        for count in (10, 100, 1000):
            self.fake.issues['1']['story_set'] = [story(i) for i in range(count)]
            refresh_and_scan(local)
            queries = []
            self.db.set_trace_callback(queries.append)
            result = self.loaded()
            counts.append(sum(q.lstrip().upper().startswith('SELECT') for q in queries))
            self.assertEqual(len(result['stories']), count)
            queries.clear()
            summary = summaries(self.db.cursor(), local)
            self.assertEqual(len(queries), 1)
            self.assertNotIn('story_observations ', queries[0])
            self.assertEqual(next(iter(summary.values()))['story_count'], count)
            self.db.set_trace_callback(None)
        self.assertEqual(len(set(counts)), 1)
        print('Bibliography detail SELECTs at 10/100/1000 stories (including owner lookup):', counts)

    def test_http_and_persistence_diagnostics_with_rich_payloads(self):
        from asyncio import run
        from time import perf_counter

        from backend.implementations.metadata.enrichment import \
            VolumeFetchResult
        from backend.implementations.metadata.registry import \
            get_volume_provider
        from backend.implementations.metadata.snapshot_persistence import \
            reconcile_snapshot
        local = self.add_gcd()
        template = deepcopy(self.fake.issues['1'])
        reports = []
        for count in (10, 100, 1000):
            self.fake.issues = {str(i): dict(template, number=str(i),
                api_url=self.fake.base + f'issue/{i}/') for i in range(1, count + 1)}
            self.fake.series['active_issues'] = [r['api_url'] for r in self.fake.issues.values()]
            before = len(self.fake.requests)
            snapshot = run(get_volume_provider('gcd').fetch_snapshot('1'))
            self.assertEqual(len(self.fake.requests) - before, count + 3)
            self.assertEqual(sum(len(i.bibliography.stories) for i in snapshot.issues), count)
            queries = []
            self.db.set_trace_callback(queries.append)
            started = perf_counter()
            reconcile_snapshot(VolumeFetchResult(snapshot.volume, (), snapshot=snapshot), local)
            seconds = perf_counter() - started
            reads = sum(q.lstrip().upper().startswith('SELECT') for q in queries)
            queries.clear()
            self.assertEqual(len(summaries(self.db.cursor(), local)), count)
            self.assertEqual(len(queries), 1)
            self.db.set_trace_callback(None)
            self.assertLess(reads, 25)
            reports.append((count, count + 3, reads, round(seconds, 4)))
        print('Bibliography issues/HTTP/persistence SELECTs/seconds:', reports)

    def test_large_ignored_story_text_still_obeys_transport_limit(self):
        from asyncio import run

        from backend.implementations.metadata.registry import \
            get_volume_provider
        self.fake.issues['1']['story_set'][0]['synopsis'] = 'x' * (8 * 1024 * 1024 - 4096)
        snapshot = run(get_volume_provider('gcd').fetch_snapshot('1'))
        self.assertLess(snapshot.issues[0].bibliography.text_bytes, 1024)
        self.fake.issues['1']['story_set'][0]['synopsis'] = 'x' * (8 * 1024 * 1024)
        with self.assertRaises(GcdError) as error:
            run(get_volume_provider('gcd').fetch_snapshot('1'))
        self.assertEqual(error.exception.reason, 'response_limit')

    def test_authenticated_read_api_no_mutation_and_owner_scope(self):
        from fixtures.comicvine_search import FAKE_APP_KEY

        from backend.internals.server import Server
        local = self.add_gcd()
        iid = self.loaded()['issue_id']
        self.start_patch('frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.get_db', side_effect=self.db.cursor)
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        for name in ('WebSocket', 'SimpleQueue', 'MPWebSocketQueue'):
            self.start_patch('backend.internals.server.' + name)
        app = Server._create_app()
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        http = app.test_client()
        self.assertEqual(http.get(f'/api/issues/{iid}/bibliography').status_code, 401)
        before = self.db.total_changes
        requests = len(self.fake.requests)
        args = dict(api_key=FAKE_APP_KEY)
        response = http.get(f'/api/issues/{iid}/bibliography', query_string=args)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['result']['stories'][0]['title'], 'Beginning')
        page = http.get(f'/volumes/{local}')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'issue-bibliography-content', page.data)
        rich = http.get(f'/api/volumes/{local}', query_string=dict(args, metadata='true', issue_facts='1'))
        self.assertEqual(rich.status_code, 200)
        self.assertEqual(rich.json['result']['issues'][0]['issue_number'], '[nn]')
        self.assertEqual(http.get(f'/api/volumes/{local}/bibliography', query_string=args).status_code, 200)
        self.assertEqual(http.get(f'/api/issues/{iid}/bibliography', query_string=dict(args, set_id='99999')).status_code, 400)
        self.assertEqual(http.get(f'/api/issues/{iid}/bibliography', query_string=dict(args, set_id='../secret')).status_code, 400)
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(len(self.fake.requests), requests)

    def test_foreign_bibliography_cannot_be_overwritten(self):
        local = self.add_gcd()
        self.db.execute("UPDATE issue_bibliography SET provider='other'")
        self.db.commit()
        before = self.loaded()
        core = self.state()
        self.fake.issues['1']['number'] = '99'
        with self.assertRaisesRegex(ValueError, 'authority conflict'):
            refresh_and_scan(local)
        self.assertEqual(self.loaded(), before)
        self.assertEqual(self.state(), core)

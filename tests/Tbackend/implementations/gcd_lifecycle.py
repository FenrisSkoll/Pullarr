"""Rich snapshot add/refresh through the production provider capabilities."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from fixtures.comicvine_fetch import LibraryAddHarness
from Tbackend.implementations.gcd_client import FakeGcd

from backend.base.definitions import UnsupportedLegacyIssue
from backend.implementations.issue_transport import rich_issue_rows
from backend.implementations.metadata.gcd import GcdMetadataProvider
from backend.implementations.metadata.gcd_budget import GcdBudget
from backend.implementations.metadata.gcd_client import GcdClient, GcdError
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.volumes import Library, Volume, refresh_and_scan
from backend.internals.issue_facts import load_records


class GcdLifecycleHarness(LibraryAddHarness):
    def setUp(self):
        super().setUp()
        self.fake = FakeGcd()
        self.addCleanup(self.fake.close)
        self.fake.issues['1']['number'] = '[nn]'
        self.budget = GcdBudget(True, cursor=self.db.cursor)
        for module in ('backend.implementations.metadata.gcd',
                       'backend.implementations.metadata.snapshot_persistence',
                       'backend.implementations.metadata.refresh',
                       'backend.implementations.issue_transport'):
            self.start_patch(module + '.get_db', side_effect=self.db.cursor)
        owner = self

        class Provider(GcdMetadataProvider):
            def __init__(self):
                super().__init__(lambda: GcdClient(base=owner.fake.base,
                    session=owner.fake.session(),
                    charge=owner.budget.charge, preflight=owner.budget.preflight,
                    limited=owner.budget.limited))

        mapping = patch.dict('backend.implementations.metadata.registry.PROVIDERS', {'gcd': Provider})
        mapping.start()
        self.addCleanup(mapping.stop)
        self.start_patch('backend.implementations.file_matching.scan_files',
                         side_effect=AssertionError('No scan during rich refresh'))

    def add_gcd(self):
        return Library.add_metadata(ProviderVolumeIdentity('gcd', '1'), 1, True)

    def state(self):
        return {table: self.db.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                for table in ('volumes', 'issues', 'volume_external_ids', 'issue_external_ids',
                              'issue_number_facts', 'issue_date_facts', 'issue_variant_of')}


class GcdLifecycleTests(GcdLifecycleHarness, TestCase):

    def test_add_rich_and_local_transport_without_library_mutation(self):
        local = self.add_gcd()
        self.scan.assert_not_called()
        self.process.assert_not_called()
        self.assertFalse(list(self.root.iterdir()))
        record = load_records(self.db.cursor(), volume_id=local)[0]
        self.assertIsNone(record.legacy_number)
        self.assertIsNone(record.legacy_date)
        self.assertEqual(record.facts.number.raw_label, '[nn]')
        with self.assertRaises(UnsupportedLegacyIssue):
            Volume(local).get_public_data()
        rich = Volume(local).get_public_data(rich_issues=True)['issues'][0]
        self.assertEqual(rich['issue_number'], '[nn]')
        self.assertEqual(rich['date_display'], '2021-12')
        self.assertFalse(rich['legacy_projection_available'])
        before = len(self.fake.requests)
        rich_issue_rows(local)
        self.assertEqual(len(self.fake.requests), before)

    def test_refresh_same_id_projection_emergence_and_loss(self):
        local = self.add_gcd()
        original = self.db.execute('SELECT id FROM issues').fetchone()[0]
        self.fake.issues['1'].update(number='1', key_date='2021-12-15')
        refresh_and_scan(local)
        row = self.db.execute('SELECT id,calculated_issue_number,date FROM issues').fetchone()
        self.assertEqual(tuple(row), (original, 1.0, '2021-12-15'))
        self.fake.issues['1'].update(number='1A', key_date='2021-12-00')
        refresh_and_scan(local)
        row = self.db.execute('SELECT id,calculated_issue_number,date FROM issues').fetchone()
        self.assertEqual(tuple(row), (original, None, None))
        self.assertEqual(load_records(self.db.cursor(), volume_id=local)[0].facts.number.raw_label, '1A')

    def test_failed_refresh_preserves_previous_snapshot(self):
        local = self.add_gcd()
        before = self.state()
        self.fake.issues.clear()
        with self.assertRaises(GcdError):
            refresh_and_scan(local)
        self.assertEqual(before, self.state())

    def test_remote_missing_preserves_local_identity_and_monitoring(self):
        local = self.add_gcd()
        before = self.db.execute('SELECT * FROM issues').fetchall()
        self.fake.series['active_issues'] = []
        refresh_and_scan(local)
        self.assertEqual(before, self.db.execute('SELECT * FROM issues').fetchall())
        self.assertIn('retained_missing_issue_ids', self.db.execute(
            "SELECT value FROM config WHERE key='provider_snapshot:1'").fetchone()[0])

    def test_add_failure_is_atomic(self):
        with patch('backend.implementations.metadata.snapshot_persistence.write_facts',
                   side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.add_gcd()
        self.assert_empty_library()

    def test_add_rechecks_identity_after_remote_acquisition(self):
        from asyncio import run

        from backend.internals.provider_identity import MetadataIdentityError
        def acquired(coroutine):
            fetched = run(coroutine)
            # Another completed add wins after the caller's initial lookup.
            with patch('backend.implementations.volumes.run', run):
                self.add_gcd()
            return fetched
        with patch('backend.implementations.volumes.run', side_effect=acquired):
            with self.assertRaises(MetadataIdentityError):
                self.add_gcd()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 1)

    def test_variant_separate_identity_and_complete_relation_removal(self):
        row = deepcopy(self.fake.issues['1'])
        row.update(api_url=self.fake.base + 'issue/2/', variant_of=self.fake.base + 'issue/1/', number='1A')
        self.fake.issues['2'] = row
        self.fake.series['active_issues'].append(row['api_url'])
        local = self.add_gcd()
        records = load_records(self.db.cursor(), volume_id=local)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1].variant_of.provider_id, '1')
        self.fake.issues['2']['variant_of'] = None
        refresh_and_scan(local)
        self.assertIsNone(load_records(self.db.cursor(), volume_id=local)[1].variant_of)

    def test_authenticated_search_add_rich_read_monitor_and_legacy_refusal(self):
        from fixtures.comicvine_search import FAKE_APP_KEY

        from backend.internals.server import Server
        self.start_patch('frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.get_db', side_effect=self.db.cursor)
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        self.start_patch('backend.implementations.root_folders.get_db', side_effect=self.db.cursor)
        for name in ('WebSocket', 'SimpleQueue', 'MPWebSocketQueue'):
            self.start_patch('backend.internals.server.' + name)
        app = Server._create_app()
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        http = app.test_client()
        args = dict(api_key=FAKE_APP_KEY, metadata='true', issue_facts='1')
        self.assertEqual(http.post('/api/volumes', json={}).status_code, 401)
        transport = GcdClient
        def local_client(**kwargs):
            return transport(base=self.fake.base, session=self.fake.session(), **kwargs)
        self.fake.override = lambda path: (200, {'count': 0, 'results': []},
            'application/json', {}) if path.startswith('/api/series/?') else None
        self.start_patch('backend.implementations.metadata.gcd_budget.get_db', side_effect=self.db.cursor)
        # GcdBudget's injected default is bound at definition, so explicitly bind
        # this API's ledger to the same disposable DB.
        with patch('backend.implementations.metadata.gcd_client.GcdClient', side_effect=local_client), \
                patch('backend.implementations.metadata.gcd_budget.GcdBudget',
                      side_effect=lambda auth: GcdBudget(auth, cursor=self.db.cursor)):
            connection = http.post('/api/settings/gcd/test', query_string=args,
                json={'username': 'fixture', 'password': 'fixture-only'})
            self.assertEqual(connection.status_code, 200, connection.json)
            self.assertTrue(connection.json['result']['authenticated'])
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)
            self.assertNotIn('fixture-only', connection.get_data(as_text=True))
            self.assertNotEqual(http.post('/api/settings/gcd/test', query_string=args,
                json={'url': 'http://untrusted.invalid'}).status_code, 200)
        self.fake.override = None
        search = http.get('/api/volumes/search', query_string=dict(args, provider='gcd', query='Example'))
        self.assertEqual(search.status_code, 200, search.json)
        added = http.post('/api/volumes', query_string=args,
                         json=dict(provider='gcd', provider_id='1', root_folder_id=1, auto_search=False))
        self.assertEqual(added.status_code, 201, added.json)
        volume = added.json['result']
        iid = volume['issues'][0]['id']
        self.assertEqual(volume['metadata_source'], {'provider': 'gcd', 'id': '1'})
        self.assertEqual(volume['issues'][0]['date_display'], '2021-12')
        self.assertEqual(http.get('/api/volumes/1', query_string=dict(api_key=FAKE_APP_KEY,
                         metadata='true')).status_code, 409)
        before = len(self.fake.requests)
        read = http.get('/api/issues/' + str(iid), query_string=args)
        self.assertEqual(read.status_code, 200, read.json)
        self.assertEqual(read.json['result']['issue_number'], '[nn]')
        updated = http.put('/api/issues/' + str(iid), query_string=args, json={'monitored': False})
        self.assertEqual(updated.status_code, 200, updated.json)
        self.assertFalse(updated.json['result']['monitored'])
        self.assertEqual(len(self.fake.requests), before)
        self.assertNotIn('story_set', read.get_data(as_text=True))

    def test_refresh_membership_race_rolls_back_nothing_because_no_write_begins(self):
        local = self.add_gcd()
        before = self.state()
        def override(path):
            if path.startswith('/api/issue/'):
                self.fake.series['active_issues'] = []
        self.fake.override = override
        with self.assertRaises(GcdError):
            refresh_and_scan(local)
        self.assertEqual(self.state(), before)

    def test_real_gcd_add_then_canonical_library_import(self):
        import sqlite3
        from dataclasses import fields
        from pathlib import Path
        from zipfile import ZipFile

        from Tbackend.features.organization_plan import NAMING

        from backend.features.local_organization import (_sessions,
                                                         apply_preview,
                                                         import_preview)
        self.addCleanup(_sessions.clear)
        self.fake.issues['1']['number'] = '1A'
        local = self.add_gcd()
        source = self.root / 'Example 1A (2021).cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'disposable comic')
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>Example</Series><Number>1A</Number><Year>2021</Year></ComicInfo>')
        path = str(self.root / 'organizer.db')
        copied = sqlite3.connect(path)
        try:
            self.db.backup(copied)
            copied.execute("INSERT INTO config VALUES('database_version',60)")
            copied.executemany('INSERT INTO config VALUES(?,?)', ((f.name, getattr(NAMING, f.name)) for f in fields(NAMING)))
            copied.commit()
            before = len(self.fake.requests)
            result = import_preview(path, [{'filepath': str(source), 'provider': 'gcd', 'provider_id': '1'}], True)
            self.assertEqual(result['plans'][0]['status'], 'ready', result)
            applied = apply_preview(path, result['id'])
            self.assertEqual(applied['jobs'][0]['state'], 'completed', applied)
            self.assertTrue(Path(result['plans'][0]['target']).exists())
            self.assertEqual(copied.execute('SELECT issue_id FROM issues_files').fetchall(), [(1,)])
            self.assertEqual(len(self.fake.requests), before)
            self.assertEqual(local, 1)
        finally:
            copied.close()

    def test_scheduled_group_bounded_and_failed_oldest_does_not_starve(self):
        from datetime import datetime

        from backend.implementations.metadata.refresh import \
            refresh_single_group
        from backend.implementations.metadata.registry import \
            get_volume_provider
        local = self.add_gcd()
        before = len(self.fake.requests)
        provider = get_volume_provider('gcd')
        # Scheduler groups contain real selected authorities, not synthetic IDs.
        self.db.execute("INSERT INTO volumes(id,title,root_folder,folder,metadata_provider) SELECT 99,'Other',root_folder,'other','gcd' FROM volumes WHERE id=?", (local,))
        self.db.execute("INSERT INTO volume_external_ids VALUES(99,'gcd','2','fixture',NULL)")
        self.db.commit()
        # First target missing: one attempt only, persisted cooldown means the
        # next scheduler invocation can consider other due identities.
        with patch.object(provider, 'fetch_snapshot_scheduled', side_effect=GcdError('not_found')) as fetch:
            refresh_single_group(provider, 'gcd', {'1': (local, 0), '2': (99, 1)},
                                 datetime(2026, 1, 1), None, False)
            self.assertEqual(fetch.call_count, 1)
            refresh_single_group(provider, 'gcd', {'1': (local, 0), '2': (99, 1)},
                                 datetime(2026, 1, 1), None, False)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args.args, ('2',))
        self.assertEqual(len(self.fake.requests), before)

    def test_foreign_date_facts_block_refresh_before_replacement(self):
        from backend.internals.provider_identity import MetadataIdentityError
        local = self.add_gcd()
        self.db.execute("UPDATE issue_date_facts SET provenance='other_authority' WHERE source_field='on_sale_date'")
        self.db.commit()
        before = self.state()
        with self.assertRaises(MetadataIdentityError):
            refresh_and_scan(local)
        self.assertEqual(before, self.state())

    def test_batched_persistence_and_no_change_refresh_diagnostics(self):
        from asyncio import run
        from dataclasses import replace
        from time import perf_counter

        from backend.base.issue_facts import IssueNumberFacts
        from backend.implementations.metadata.enrichment import \
            VolumeFetchResult
        from backend.implementations.metadata.registry import \
            get_volume_provider
        from backend.implementations.metadata.snapshot_persistence import \
            reconcile_snapshot
        local = self.add_gcd()
        template = run(get_volume_provider('gcd').fetch_snapshot('1'))
        results = []
        for count in (10, 100, 1000):
            issues = tuple(replace(template.issues[0], provider_id=str(i),
                facts=replace(template.issues[0].facts,
                    number=IssueNumberFacts.interpret(str(i), 'gcd-rest/v1', 'number')))
                for i in range(1, count + 1))
            snapshot = replace(template, volume=replace(template.volume, issue_count=count),
                issues=issues, receipt=replace(template.receipt, expected_count=count))
            result = VolumeFetchResult(snapshot.volume, (), snapshot=snapshot)
            traces = []
            self.db.set_trace_callback(traces.append)
            start = perf_counter()
            reconcile_snapshot(result, local)
            elapsed = perf_counter() - start
            self.db.set_trace_callback(None)
            reads = sum(s.lstrip().upper().startswith('SELECT') for s in traces)
            self.assertLess(reads, 35)
            identities = self.db.execute('SELECT id FROM issues ORDER BY id').fetchall()
            reconcile_snapshot(result, local)
            self.assertEqual(identities, self.db.execute('SELECT id FROM issues ORDER BY id').fetchall())
            results.append((count, reads, round(elapsed, 4)))
        print('GCD canonical persistence rows/SELECTs/seconds:', results)

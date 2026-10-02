"""Real 8D/8G service workflows behind authenticated bounded transport."""

from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

import TMaintenanceAPI as fixtures
import TProviderSwitchApply as provider_fixture

from backend.features.maintenance_runtime import MaintenanceRuntime
from backend.implementations.metadata.switch_target import admit


class MaintenanceSpecializedAPITests(TestCase):
    def setUp(self):
        self.fixture = fixtures.MaintenanceAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.request, self.runtime = self.fixture.request, self.fixture.runtime
        self.db = self.fixture.fixture.db
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',66)")
        self.db.commit()

    def finish(self, delivery):
        self.fixture.tasks[-1].run()
        status = self.request('GET', '/action-tasks/' + delivery['id'])
        self.assertEqual(status['state'], 'complete', status)
        return status['result']

    def worklist(self, action, code='filename_deviation', level='inventory'):
        scan = self.request('POST', '/scans', dict(scope=dict(kind='volumes', ids=[1]), level=level))
        self.fixture.tasks[-1].run()
        worklist = self.request('POST', '/worklists', dict(scan_id=scan['id']))
        path = '/worklists/' + worklist['id']
        item = next(i for i in self.request('GET', path + '/items')['items'] if i['finding']['code'] == code)
        self.request('POST', path + '/revise', dict(revision=0, edits=[dict(finding_id=item['finding']['id'],
            selected=True, excluded=False, action=action)]))
        self.fixture.tasks[-1].run()
        return self.request('GET', path), item['finding']['id']

    def handoff(self, worklist):
        return dict(worklist_id=worklist['id'], revision=worklist['revision'], digest=worklist['manifest_digest'])

    def test_metadata_exact_selection_apply_and_durable_lookup_after_restart(self):
        fetches = []
        async def acquire(reference):
            fetches.append(reference)
            return admit(provider_fixture.remote(reference.provider, parent=reference.provider_id, first=101, count=1), reference)
        self.runtime.metadata.acquire = acquire
        worklist, finding = self.worklist('metadata_repair')
        result = self.finish(self.request('POST', '/repair/metadata/reviews', dict(self.handoff(worklist), selected=[finding])))
        path = '/repair/metadata/reviews/' + result['id']
        review = self.request('GET', path)
        self.assertFalse(review['apply_available'])
        selected = next(f for f in review['items'] if f['key'] == 'volume:1:title')
        self.finish(self.request('POST', path + '/selection', dict(revision=0, edits=[dict(key=selected['key'], selected=True)])))
        review = self.request('GET', path)
        self.assertEqual(len(fetches), 1)
        old_year = self.db.execute('SELECT year FROM volumes WHERE id=1').fetchone()[0]
        old_bytes = (self.fixture.fixture.volume / 'wrong.cbz').read_bytes()
        body = dict(revision=review['revision'], digest=review['digest'], confirmed=True)
        with patch('socket.socket', side_effect=AssertionError('no provider on apply')):
            result = self.finish(self.request('POST', path + '/apply', body))
        self.assertEqual(result['kind'], 'metadata_receipt')
        self.assertEqual(self.db.execute('SELECT title,year FROM volumes WHERE id=1').fetchone(), ('Target comicvine', old_year))
        self.assertEqual(old_bytes, (self.fixture.fixture.volume / 'wrong.cbz').read_bytes())
        self.fixture.app.extensions['maintenance'] = MaintenanceRuntime(str(self.fixture.fixture.database),
            enqueue=lambda t: self.fixture.tasks.append(t) or len(self.fixture.tasks))
        found = self.request('GET', path + f"/result?revision={body['revision']}&digest={body['digest']}")
        self.assertTrue(found['found'])
        self.assertEqual(found['id'], result['id'])
        retry = self.finish(self.request('POST', path + '/apply', body))
        self.assertEqual(retry['id'], result['id'])
        self.request('GET', path + '/result?revision=1&digest=' + '0' * 64, status=409)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM metadata_repair_receipts').fetchone()[0], 1)

    def test_comicinfo_safe_fields_journal_and_retry(self):
        source = self.fixture.fixture.volume / 'wrong.cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('001.jpg', b'preserved-page')
            archive.writestr('ComicInfo.xml', '<ComicInfo><Year>bad</Year><Notes>untouched unknown text</Notes></ComicInfo>')
        self.db.execute('UPDATE files SET size=? WHERE id=1', (source.stat().st_size,))
        self.db.commit()
        worklist, finding = self.worklist('comicinfo_repair', 'invalid_field', 'archive')
        result = self.finish(self.request('POST', '/repair/comicinfo/reviews', dict(self.handoff(worklist), finding_id=finding)))
        path = '/repair/comicinfo/reviews/' + result['id']
        review = self.request('GET', path)
        self.assertNotIn('<ComicInfo', str(review))
        self.assertNotIn('raw_bytes', str(review))
        self.finish(self.request('POST', path + '/selection', dict(revision=0, selected=['Series', 'Number', 'Provider identities'])))
        review = self.request('GET', path)
        body = dict(revision=review['revision'], digest=review['digest'], confirmed=True)
        result = self.finish(self.request('POST', path + '/apply', body))
        self.assertEqual(result['entry']['state'], 'complete', result)
        with ZipFile(source) as archive:
            self.assertIn(b'untouched unknown text', archive.read('ComicInfo.xml'))
            self.assertEqual(archive.read('001.jpg'), b'preserved-page')
            self.assertIsNone(archive.testzip())
        found = self.request('GET', path + f"/result?revision={body['revision']}&digest={body['digest']}")
        self.assertEqual(found['entry']['id'], result['entry']['id'])
        self.assertEqual(self.finish(self.request('POST', path + '/apply', body))['entry']['id'], result['entry']['id'])

    def test_quarantine_prepare_no_internal_storage_restore_and_retry(self):
        source = self.fixture.fixture.volume / 'wrong.cbz'
        duplicate = self.fixture.fixture.volume / 'copy.cbz'
        duplicate.write_bytes(source.read_bytes())
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,?)', (str(duplicate), duplicate.stat().st_size))
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(2,1)')
        self.db.commit()
        worklist, finding = self.worklist('duplicate_review', 'exact_byte_duplicate', 'deep')
        result = self.finish(self.request('POST', '/duplicate/reviews', dict(self.handoff(worklist), selected=[finding])))
        path = '/duplicate/reviews/' + result['id']
        review = self.request('GET', path)
        group = review['items'][0]
        self.finish(self.request('POST', path + '/selection', dict(revision=review['revision'], choices=[dict(group_id=group['id'], action='quarantine_selected', quarantine=[2])])))
        review = self.request('GET', path)
        self.assertFalse(review['apply_available'])
        self.assertTrue(review['prepare_available'])
        self.finish(self.request('POST', path + '/prepare', dict(revision=review['revision'])))
        review = self.request('GET', path)
        members = self.request('GET', path + '?group_id=' + group['id'])
        self.assertTrue(review['apply_available'], review)
        self.assertNotIn('.kapowarr-quarantine', str(review) + str(members))
        body = dict(revision=review['revision'], digest=review['digest'], origin=review['origin'], selected=review['selected'], confirmed=True)
        result = self.finish(self.request('POST', path + '/apply', body))
        self.assertEqual(result['state'], 'completed', result)
        self.assertFalse(duplicate.exists())
        self.assertTrue(source.exists())
        retry = self.finish(self.request('POST', path + '/apply', body))
        self.assertEqual(retry['id'], result['id'])
        batch = self.request('GET', '/batches/' + result['id'])
        job = batch['items'][0]['id']
        preview = self.finish(self.request('POST', '/history/organization/' + job + '/inverse-preview', {}))['preview']
        self.assertTrue(preview['eligible'], preview)
        restored = self.finish(self.request('POST', '/history/organization/' + job + '/inverse', dict(digest=preview['digest'], confirmed=True)))
        self.assertEqual(restored['entry']['state'], 'complete')
        self.assertEqual(sha256(duplicate.read_bytes()).digest(), sha256(source.read_bytes()).digest())
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())

    def test_every_specialized_route_auth_and_strict_shape(self):
        identifier = 'a' * 32
        routes = []
        for family in ('metadata', 'comicinfo'):
            base = '/repair/' + family + '/reviews'
            routes += [('POST', base), ('GET', base + '/' + identifier), ('GET', base + '/' + identifier + '/result'),
                ('POST', base + '/' + identifier + '/selection'), ('POST', base + '/' + identifier + '/apply')]
        base = '/duplicate/reviews'
        routes += [('POST', base), ('GET', base + '/' + identifier), ('POST', base + '/' + identifier + '/selection'),
                   ('POST', base + '/' + identifier + '/prepare'), ('POST', base + '/' + identifier + '/apply')]
        for method, path in routes:
            self.request(method, path, authorized=False, status=401)
            if method == 'POST':
                self.request(method, path, {'target': '/arbitrary', 'force': True}, status=400)
                self.request(method, path, status=400, data='{broken', content_type='application/json')
                self.request(method, path, status=413, data='x' * (512 * 1024 + 1), content_type='application/json')
        self.assertFalse(self.fixture.tasks)

    def test_controlled_provider_failure_and_unexpected_failure_are_safe(self):
        from backend.implementations.metadata.errors import \
            MetadataProviderError
        worklist, finding = self.worklist('metadata_repair')
        for failure, reason in ((MetadataProviderError('comicvine', 'credentials'), 'provider_configuration'),
                                (RuntimeError('secret https://private/?key=never-return'), 'internal_error')):
            async def acquire(reference):
                raise failure
            self.runtime.metadata.acquire = acquire
            delivery = self.request('POST', '/repair/metadata/reviews', dict(self.handoff(worklist), selected=[finding]))
            self.fixture.tasks[-1].run()
            status = self.request('GET', '/action-tasks/' + delivery['id'])
            self.assertEqual(status['state'], 'failed')
            self.assertEqual(status['reason'], reason)
            self.assertNotIn('private', str(status))
            self.assertNotIn('never-return', str(status))

    def test_malformed_comicinfo_cannot_be_acquired_or_submitted(self):
        source = self.fixture.fixture.volume / 'wrong.cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('001.jpg', b'preserved')
            archive.writestr('ComicInfo.xml', '<ComicInfo><Title>broken')
        self.db.execute('UPDATE files SET size=? WHERE id=1', (source.stat().st_size,))
        self.db.commit()
        before = source.read_bytes()
        worklist, finding = self.worklist('comicinfo_repair', 'filename_deviation', 'archive')
        delivery = self.request('POST', '/repair/comicinfo/reviews', dict(self.handoff(worklist), finding_id=finding))
        self.fixture.tasks[-1].run()
        result = self.request('GET', '/action-tasks/' + delivery['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(self.db.execute('SELECT id FROM organization_jobs').fetchall())

    def test_history_transport_response_bound_and_cheap_page(self):
        with patch.object(self.runtime.history, 'page', return_value={'next_cursor': None, 'value': 'x' * (2 * 1024 * 1024)}):
            self.request('GET', '/history', status=409)
        with patch('backend.features.organization_execution.OrganizationExecutor.preview_undo', side_effect=AssertionError('overview must remain cheap')):
            self.request('GET', '/history?limit=50')

    def test_fifty_row_specialized_page_diagnostics(self):
        import json
        import sqlite3
        import tracemalloc
        from time import perf_counter

        from TBulkFolder import BulkFolderTests
        from TBulkRename import BulkRenameTests
        from TDuplicateReview import DuplicateReviewTests

        def measure(endpoint):
            statements = []
            original = sqlite3.connect
            def connect(*args, **kwargs):
                db = original(*args, **kwargs)
                db.set_trace_callback(lambda sql: statements.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
                return db
            tracemalloc.start()
            started = perf_counter()
            with patch('sqlite3.connect', side_effect=connect), patch('socket.socket', side_effect=AssertionError('no provider on retained page')):
                result = self.request('GET', endpoint + '?limit=50')
            elapsed = perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(len(result['items']), 50)
            self.assertLess(len(statements), 150)
            print('8I specialized page', dict(kind=endpoint.split('/')[1:3], rows=50,
                selects=len(statements), seconds=round(elapsed, 4), peak_bytes=peak,
                response_bytes=len(json.dumps(result).encode())))

        for family, fixture_class in (('rename', BulkRenameTests), ('folder', BulkFolderTests)):
            fixture = fixture_class()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            fixture.batch(50)
            review = fixture.review()
            with patch.object(self.runtime, family, fixture.service):
                measure('/' + family + '/reviews/' + review.id)
        duplicate = DuplicateReviewTests()
        duplicate.setUp()
        self.addCleanup(duplicate.doCleanups)
        duplicate.test_review_scale_100_findings_and_capacity_failure()
        review = next(iter(duplicate.service._sessions.values()))
        with patch.object(self.runtime, 'duplicates', duplicate.service):
            measure('/duplicate/reviews/' + review.id)
        for issue in range(2, 21):
            self.db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(?,1,?,?,?)',
                (issue, 100 + issue, str(issue), issue))
        self.db.commit()
        async def acquire(reference):
            return admit(provider_fixture.remote(reference.provider, parent=reference.provider_id, first=101, count=20), reference)
        self.runtime.metadata.acquire = acquire
        worklist, finding = self.worklist('metadata_repair')
        review = self.finish(self.request('POST', '/repair/metadata/reviews', dict(self.handoff(worklist), selected=[finding])))
        measure('/repair/metadata/reviews/' + review['id'])

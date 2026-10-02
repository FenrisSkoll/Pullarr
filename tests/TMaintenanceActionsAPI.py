"""Reviewed rename and history transport against real disposable journals."""

from hashlib import sha256
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import TMaintenanceAPI as fixtures

from backend.features.maintenance_runtime import MaintenanceRuntime


class MaintenanceActionsAPITests(TestCase):
    def setUp(self):
        self.fixture = fixtures.MaintenanceAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.request = self.fixture.request
        self.runtime = self.fixture.runtime
        self.db = self.fixture.fixture.db
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',66)")
        self.db.commit()

    def finish(self, delivery):
        self.fixture.tasks[-1].run()
        return self.request('GET', '/action-tasks/' + delivery['id'])

    def rename_review(self):
        worklist = self.fixture.worklist()
        path = '/worklists/' + worklist['id']
        item = next(i for i in self.request('GET', path + '/items')['items']
                    if i['finding']['code'] == 'filename_deviation')
        self.request('POST', path + '/revise', dict(revision=0, edits=[dict(
            finding_id=item['finding']['id'], selected=True, excluded=False, action='rename')]))
        self.fixture.tasks[-1].run()
        worklist = self.request('GET', path)
        delivery = self.request('POST', '/rename/reviews', dict(worklist_id=worklist['id'],
            revision=worklist['revision'], digest=worklist['manifest_digest'], selected=worklist['rename_selection']))
        status = self.finish(delivery)
        self.assertEqual(status['state'], 'complete', status)
        return self.request('GET', '/rename/reviews/' + status['result']['id'])

    def test_all_new_routes_auth_and_reject_arbitrary_intent(self):
        sid = 'a' * 32
        routes = [('POST', '/rename/reviews'), ('GET', '/rename/reviews/' + sid),
            ('POST', '/rename/reviews/' + sid + '/selection'), ('POST', '/rename/reviews/' + sid + '/apply'),
            ('GET', '/action-tasks/' + sid), ('POST', '/history/organization/job/recovery-preview'),
            ('POST', '/history/organization/job/inverse-preview'), ('POST', '/history/organization/job/recover'),
            ('POST', '/history/organization/job/inverse')]
        for method, path in routes:
            self.request(method, path, authorized=False, status=401)
            if method == 'POST':
                self.request(method, path, {'force': True, 'target': 'C:\\arbitrary'}, status=400)
                response = self.fixture.client.get('/api/maintenance' + path + '?api_key=fixture-auth-key')
                self.assertEqual(response.status_code, 405)
        self.assertFalse(self.fixture.tasks)

    def test_real_rename_response_loss_restart_and_inverse(self):
        source = self.fixture.fixture.volume / 'wrong.cbz'
        original_hash = sha256(source.read_bytes()).hexdigest()
        links = self.db.execute('SELECT * FROM issues_files').fetchall()
        review = self.rename_review()
        self.assertTrue(review['apply_available'])
        self.assertTrue(source.exists())
        target = Path(review['items'][0]['target'])
        body = {k: review[k] for k in ('revision', 'digest', 'origin', 'selected')}
        body['confirmed'] = True
        path = '/rename/reviews/' + review['id'] + '/apply'
        with patch('socket.socket', side_effect=AssertionError('network')):
            result = self.finish(self.request('POST', path, body))
        self.assertEqual(result['state'], 'complete', result)
        self.assertEqual(result['result']['state'], 'completed', result)
        self.assertFalse(source.exists())
        self.assertEqual(sha256(target.read_bytes()).hexdigest(), original_hash)
        self.assertEqual(links, self.db.execute('SELECT * FROM issues_files').fetchall())
        batch = self.request('GET', '/batches/' + result['result']['id'])
        self.assertEqual(batch['mutation_job_count'], 1)
        job = batch['items'][0]['id']
        # New app-owned graph, no transient child review: exact apply returns
        # the original durable batch, not another mutation.
        self.runtime = MaintenanceRuntime(str(self.fixture.fixture.database),
            enqueue=lambda task: self.fixture.tasks.append(task) or len(self.fixture.tasks))
        self.fixture.app.extensions['maintenance'] = self.runtime
        retried = self.finish(self.request('POST', path, body))
        self.assertEqual(retried['result']['id'], result['result']['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
        preview = self.finish(self.request('POST', '/history/organization/' + job + '/inverse-preview', {}))
        self.assertEqual(preview['state'], 'complete', preview)
        inverse = preview['result']['preview']
        self.assertTrue(inverse['eligible'], inverse)
        restored = self.finish(self.request('POST', '/history/organization/' + job + '/inverse',
            dict(digest=inverse['digest'], confirmed=True)))
        self.assertEqual(restored['result']['entry']['state'], 'complete', restored)
        self.assertEqual(sha256(source.read_bytes()).hexdigest(), original_hash)
        self.assertFalse(target.exists())
        repeat = self.finish(self.request('POST', '/history/organization/' + job + '/inverse',
            dict(digest=inverse['digest'], confirmed=True)))
        self.assertEqual(repeat['result']['entry']['id'], restored['result']['entry']['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 2)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())

    def test_stale_review_cannot_change_files_and_unknown_error_is_private(self):
        review = self.rename_review()
        self.db.execute("UPDATE volumes SET authority_generation=authority_generation+1 WHERE id=1")
        self.db.commit()
        body = {k: review[k] for k in ('revision', 'digest', 'origin', 'selected')}
        body['confirmed'] = True
        result = self.finish(self.request('POST', '/rename/reviews/' + review['id'] + '/apply', body))
        self.assertEqual(result['reason'], 'stale', result)
        self.assertTrue((self.fixture.fixture.volume / 'wrong.cbz').exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        with patch.object(self.runtime.actions.recovery, 'preview_inverse', side_effect=RuntimeError('SECRET internal quarantine path')):
            result = self.finish(self.request('POST', '/history/organization/job/inverse-preview', {}))
        self.assertEqual(result['reason'], 'internal_error')
        self.assertNotIn('SECRET', str(result))

    def test_confirmation_syntax_and_page_bounds(self):
        review = self.rename_review()
        body = {k: review[k] for k in ('revision', 'digest', 'origin', 'selected')}
        for key, value in (('confirmed', False), ('revision', True), ('digest', 'X' * 64),
                           ('selected', []), ('origin', ['../path', 0, 'a' * 64])):
            altered = dict(body, confirmed=True)
            altered[key] = value
            self.request('POST', '/rename/reviews/' + review['id'] + '/apply', altered, status=400)
        self.request('GET', '/rename/reviews/' + review['id'] + '?limit=101', status=400)
        self.request('GET', '/rename/reviews/' + review['id'] + '?offset=251', status=400)

    def folder_review(self, custom=False):
        self.db.execute("UPDATE config SET value='{series_name} ({year})' WHERE key='volume_folder_naming'")
        self.db.execute('UPDATE volumes SET custom_folder=? WHERE id=1', (int(custom),))
        self.db.commit()
        worklist = self.fixture.worklist()
        path = '/worklists/' + worklist['id']
        item = next(i for i in self.request('GET', path + '/items')['items']
                    if i['finding']['code'] == 'folder_deviation')
        self.request('POST', path + '/revise', dict(revision=0, edits=[dict(
            finding_id=item['finding']['id'], selected=True, excluded=False, action='folder_organization')]))
        self.fixture.tasks[-1].run()
        worklist = self.request('GET', path)
        payload = dict(worklist_id=worklist['id'], revision=worklist['revision'],
            digest=worklist['manifest_digest'], selected=worklist['folder_selection'], canonical_custom=[])
        result = self.finish(self.request('POST', '/folder/reviews', payload))
        self.assertEqual(result['state'], 'complete', result)
        return self.request('GET', '/folder/reviews/' + result['result']['id']), payload

    def test_folder_routes_auth_bounds_and_no_arbitrary_target(self):
        sid = 'a' * 32
        for method, path in [('POST', '/folder/reviews'), ('GET', '/folder/reviews/' + sid),
                ('POST', '/folder/reviews/' + sid + '/selection'), ('POST', '/folder/reviews/' + sid + '/apply')]:
            self.request(method, path, authorized=False, status=401)
            if method == 'POST':
                self.request(method, path, dict(target='elsewhere', force=True), status=400)
                self.assertEqual(self.fixture.client.get('/api/maintenance' + path + '?api_key=fixture-auth-key').status_code, 405)
        review, payload = self.folder_review()
        self.request('GET', '/folder/reviews/' + review['id'] + '?offset=51', status=400)
        self.request('POST', '/folder/reviews', dict(payload, canonical_custom=['a' * 64]), status=400)
        self.request('POST', '/folder/reviews', dict(payload, selected=['a' * 64] * 51), status=400)
        before = tuple(self.db.iterdump())
        with patch('backend.features.bulk_folder.inspect_folder', side_effect=AssertionError('no repeated inventory')):
            page = self.request('GET', '/folder/reviews/' + review['id'])
        self.assertEqual(before, tuple(self.db.iterdump()))
        self.assertNotIn('tree_database_before', str(page))
        self.assertNotIn('inventory_digest', str(page))

    def test_local_volume_picker_is_paged_literal_and_read_only(self):
        self.request('GET', '/volumes', authorized=False, status=401)
        for index in range(2, 65):
            self.db.execute('INSERT INTO volumes(id,title,root_folder,folder,comicvine_id) VALUES(?,?,1,?,?)',
                (index, '100%_literal <script>' if index == 64 else 'Example', 'unused-' + str(index), index + 1000))
        self.db.commit()
        before = tuple(self.db.iterdump())
        with patch('socket.socket', side_effect=AssertionError('no provider search')):
            first = self.request('GET', '/volumes?limit=50')
            second = self.request('GET', '/volumes?limit=50&after=' + str(first['next_after']))
            literal = self.request('GET', '/volumes?q=100%25_')
        self.assertEqual(len(first['items']), 50)
        self.assertEqual(len(second['items']), 14)
        self.assertIsNone(second['next_after'])
        self.assertEqual([i['id'] for i in literal['items']], [64])
        self.assertNotIn('folder', str(first))
        self.assertEqual(before, tuple(self.db.iterdump()))
        for query in ('limit=101', 'after=-1', 'path=x', 'q=' + 'x' * 201):
            self.request('GET', '/volumes?' + query, status=400)

    def test_folder_real_move_retry_and_inverse_preserve_tree(self):
        source = self.fixture.fixture.volume
        (source / 'empty').mkdir()
        (source / 'notes.txt').write_bytes(b'ancillary preserved')
        before = {str(p.relative_to(source)): sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
        links = self.db.execute('SELECT * FROM issues_files').fetchall()
        review, _ = self.folder_review()
        self.assertTrue(review['apply_available'], review)
        self.assertEqual(review['items'][0]['ancillary_count'], 1)
        self.assertEqual(review['items'][0]['directory_count'], 1)
        target = Path(review['items'][0]['target'])
        body = {k: review[k] for k in ('revision', 'digest', 'origin', 'selected')}
        body['confirmed'] = True
        path = '/folder/reviews/' + review['id'] + '/apply'
        result = self.finish(self.request('POST', path, body))
        self.assertEqual(result['result']['state'], 'completed', result)
        self.assertFalse(source.exists())
        self.assertEqual(before, {str(p.relative_to(target)): sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file()})
        self.assertTrue((target / 'empty').is_dir())
        self.assertEqual(links, self.db.execute('SELECT * FROM issues_files').fetchall())
        self.fixture.app.extensions['maintenance'] = MaintenanceRuntime(str(self.fixture.fixture.database),
            enqueue=lambda task: self.fixture.tasks.append(task) or len(self.fixture.tasks))
        retry = self.finish(self.request('POST', path, body))
        self.assertEqual(retry['result']['id'], result['result']['id'])
        batch = self.request('GET', '/batches/' + result['result']['id'])
        self.assertEqual(batch['mutation_job_count'], 1)
        job = batch['items'][0]['id']
        preview = self.finish(self.request('POST', '/history/organization/' + job + '/inverse-preview', {}))['result']['preview']
        self.assertTrue(preview['eligible'], preview)
        inverse = self.finish(self.request('POST', '/history/organization/' + job + '/inverse', dict(digest=preview['digest'], confirmed=True)))
        self.assertEqual(inverse['result']['entry']['state'], 'complete', inverse)
        self.assertEqual(before, {str(p.relative_to(source)): sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()})
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())

    def test_custom_folder_preserved_until_new_explicit_canonical_review(self):
        review, payload = self.folder_review(custom=True)
        self.assertEqual(review['mutation_count'], 0)
        self.assertTrue(review['items'][0]['custom_after'])
        self.assertEqual(review['items'][0]['state'], 'no_changes')
        result = self.finish(self.request('POST', '/folder/reviews', dict(payload, canonical_custom=payload['selected'])))
        canonical = self.request('GET', '/folder/reviews/' + result['result']['id'])
        self.assertNotEqual(canonical['digest'], review['digest'])
        self.assertFalse(canonical['items'][0]['custom_after'])
        self.assertEqual(canonical['mutation_count'], 1)
        self.assertEqual(self.db.execute('SELECT custom_folder FROM volumes WHERE id=1').fetchone()[0], 1)

    def test_quarantine_target_only_recovery_projection_hides_storage_and_retries(self):
        import TQuarantineExecution as quarantine_fixtures
        case = quarantine_fixtures.QuarantineExecutionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.register_fixture()
        case.interrupt('after_effect', 0)
        runtime = MaintenanceRuntime(str(case.health.database),
            enqueue=lambda task: self.fixture.tasks.append(task) or len(self.fixture.tasks))
        self.fixture.app.extensions['maintenance'] = runtime
        before = tuple(case.db.iterdump())
        preview = self.finish(self.request('POST', '/history/organization/' + case.job + '/recovery-preview', {}))
        self.assertEqual(preview['state'], 'complete', preview)
        value = preview['result']['preview']
        self.assertTrue(value['eligible'], value)
        self.assertNotIn(str(case.target), str(value))
        self.assertEqual(before, tuple(case.db.iterdump()))
        body = dict(digest=value['digest'], confirmed=True)
        result = self.finish(self.request('POST', '/history/organization/' + case.job + '/recover', body))
        self.assertEqual(result['result']['entry']['state'], 'complete', result)
        case.assert_inactive()
        result = self.finish(self.request('POST', '/history/organization/' + case.job + '/recover', body))
        self.assertEqual(result['result']['entry']['state'], 'complete', result)

    def test_ambiguous_quarantine_has_no_recovery_bypass(self):
        import TQuarantineExecution as quarantine_fixtures
        case = quarantine_fixtures.QuarantineExecutionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.register_fixture()
        case.interrupt('after_effect', 0)
        case.source.write_bytes(case.target.read_bytes())
        runtime = MaintenanceRuntime(str(case.health.database),
            enqueue=lambda task: self.fixture.tasks.append(task) or len(self.fixture.tasks))
        self.fixture.app.extensions['maintenance'] = runtime
        before = tuple(case.db.iterdump())
        result = self.finish(self.request('POST', '/history/organization/' + case.job + '/recovery-preview', {}))
        preview = result['result']['preview']
        self.assertFalse(preview['eligible'])
        self.assertTrue(preview['manual_inspection_required'])
        self.assertEqual(before, tuple(case.db.iterdump()))
        result = self.finish(self.request('POST', '/history/organization/' + case.job + '/recover',
            dict(digest=preview['digest'], confirmed=True)))
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(case.source.exists())
        self.assertTrue(case.target.exists())

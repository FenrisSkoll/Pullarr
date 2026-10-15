"""Custom identities and ignored archives use the real HTTP/SQLite/import paths."""

import sqlite3
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fixtures.comicvine_search import FAKE_APP_KEY
from Tfrontend.library_import_contract import ImportAPIHarness


class CustomImport(ImportAPIHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('backend.features.custom_publications.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.features.ignored_import_files.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.features.direct_library_import.get_db', side_effect=self.db.cursor)
        self.start_patch('frontend.api.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.implementations.issue_transport.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.features.download_queue.DownloadHandler').return_value.download_for_volume_queued.return_value = False
        self.start_patch('backend.features.tasks.TaskHandler.task_for_volume_running', return_value=False)

    def api(self, method, path, **kwargs):
        path, _, query = path.partition('?')
        params = {'api_key': FAKE_APP_KEY}
        if query:
            params.update(dict(part.split('=', 1) for part in query.split('&')))
        return self.client.open('/api' + path, method=method, query_string=params, **kwargs)

    def disk_snapshot(self):
        path = self.root / 'catalogue.db'
        with closing(sqlite3.connect(path)) as disk:
            self.db.backup(disk)
        from backend.internals.db import DBConnection
        override = patch.object(DBConnection, 'default_file', str(path))
        override.start()
        self.addCleanup(override.stop)

    def create(self, paths):
        result = self.api('POST', '/local-publications', json=dict(title='Civil War Ultimate Edition',
            year=2024, sources=paths, issues=[dict(number=str(i), title='') for i in range(1, 14)]))
        self.assertEqual(result.status_code, 201, result.get_json())
        return result.get_json()['result']

    def test_custom_catalogue_and_keep_import(self):
        paths = [self.comic_file(name=f'Civil War Ultimate Edition #{i:02}.cbz') for i in (3, 4)]
        with patch('backend.features.import_issue_catalog.fetch_volume_result', side_effect=AssertionError('remote')):
            publication = self.create(paths)
            self.disk_snapshot()
            identity = publication['provider_id']
            result = self.api('POST', '/libraryimport/catalog', json=dict(provider='local',
                provider_id=identity, files=paths))
            self.assertEqual(result.status_code, 200, result.get_json())
            catalogue = result.get_json()['result']
            self.assertEqual((catalogue['loaded_issue_count'], catalogue['reported_issue_count']), (13, 13))
            self.assertEqual(catalogue['proposals'][paths[0]]['issue_ids'], [publication['issues'][2]['provider_id']])
            selected = self.api('POST', '/libraryimport/submit', json={'files': [dict(
                source=paths[0], provider='local', provider_id=identity,
                issue_ids=[publication['issues'][2]['provider_id']], mode='keep')]})
            self.assertEqual(selected.status_code, 200, selected.get_json())
            self.assertEqual(selected.get_json()['result']['results'][0]['status'], 'imported')
        self.assertTrue(Path(paths[0]).exists())
        self.assertIsNone(self.db.execute('SELECT comicvine_id FROM volumes WHERE id=?',
                                          (publication['volume_id'],)).fetchone()[0])

    def test_ignored_paths_do_not_consume_scan_limit(self):
        paths = [self.comic_file(name=f'Civil War #{i:02}.cbz') for i in range(14)]
        ignored = self.api('POST', '/libraryimport/ignore', json={'files': paths[:11]})
        self.assertEqual(ignored.status_code, 200, ignored.get_json())
        self.assertTrue(all(row['status'] == 'ignored' for row in ignored.get_json()['result']))
        self.disk_snapshot()
        scanned = self.api('GET', '/libraryimport/files?max_unregistered=20')
        self.assertEqual(scanned.status_code, 200, scanned.get_json())
        self.assertEqual([row['filepath'] for row in scanned.get_json()['result']['items']], paths[11:])
        self.assertEqual(scanned.get_json()['result']['counts']['ignored'], 11)
        restored = self.api('POST', '/libraryimport/restore', json={'files': [paths[0]]})
        self.assertEqual(restored.status_code, 200)
        with closing(sqlite3.connect(self.root / 'catalogue.db')) as disk:
            self.db.backup(disk)
        scanned = self.api('GET', '/libraryimport/files?max_unregistered=20')
        self.assertIn(paths[0], [row['filepath'] for row in scanned.get_json()['result']['items']])
        changed = Path(paths[1])
        changed.write_bytes(changed.read_bytes() + b' replacement')
        listing = self.api('GET', '/libraryimport/ignored')
        self.assertEqual(listing.status_code, 200, listing.get_json())
        self.assertEqual(next(item for item in listing.get_json()['result']['items']
                              if item['path'] == paths[1])['state'], 'changed')
        rescanned = self.api('GET', '/libraryimport/files?max_unregistered=20')
        self.assertIn(paths[1], [row['filepath'] for row in rescanned.get_json()['result']['items']])

    def test_ignored_and_registered_paths_leave_next_hundred_candidates(self):
        paths = [self.comic_file(name=f'Civil War #{i:03}.cbz') for i in range(113)]
        ignored = self.api('POST', '/libraryimport/ignore', json={'files': paths[:11]})
        self.assertEqual([row['status'] for row in ignored.get_json()['result']], ['ignored'] * 11)
        self.db.execute('INSERT INTO files(filepath,size) VALUES(?,?)',
                        (paths[11], Path(paths[11]).stat().st_size))
        self.db.commit()
        self.disk_snapshot()
        scanned = self.api('GET', '/libraryimport/files?max_unregistered=100')
        self.assertEqual(scanned.status_code, 200, scanned.get_json())
        result = scanned.get_json()['result']
        self.assertEqual([item['filepath'] for item in result['items']], paths[12:112])
        self.assertEqual(result['counts']['ignored'], 11)
        self.assertEqual(result['counts']['already_managed'], 1)
        self.assertEqual(result['counts']['discovered'], 100)
        self.assertTrue(result['counts']['more_unregistered'])

    def test_edit_can_remove_only_unused_issue_slots(self):
        path = self.comic_file(name='Civil War Ultimate Edition #03.cbz')
        publication = self.create([path])
        keep = publication['issues'][:12]
        edit = self.api('PUT', f"/local-publications/{publication['volume_id']}", json=dict(
            title=publication['title'], year=2024, publisher='', description='',
            issues=[dict(id=row['id'], number=row['number'], title='') for row in keep],
            allow_remove=True))
        self.assertEqual(edit.status_code, 200, edit.get_json())
        self.assertEqual(edit.get_json()['result']['issue_count'], 12)
        self.assertTrue(Path(path).exists())

    def test_move_edit_cover_and_no_remote_refresh(self):
        from Tfrontend.manual_volume_cover import image_bytes

        from backend.implementations.volumes import refresh_and_scan
        path = self.comic_file(name='Civil War Ultimate Edition #04.cbz')
        publication = self.create([path])
        volume_id = publication['volume_id']
        chosen = publication['issues'][3]['provider_id']
        self.settings.file_naming = self.settings.file_naming_empty = \
            '{metadata_provider}-{issue_number}-{issue_provider_id}'
        submitted = self.api('POST', '/libraryimport/submit', json={'files':[dict(
            source=path, provider='local', provider_id=publication['provider_id'],
            issue_ids=[chosen], mode='move')]})
        self.assertEqual(submitted.status_code, 200, submitted.get_json())
        result = submitted.get_json()['result']['results'][0]
        self.assertEqual(result['status'], 'imported', result)
        self.assertTrue(Path(result['target']).exists())
        self.assertIn('local-', Path(result['target']).name)
        self.assertFalse(Path(path).exists())
        edit = self.api('PUT', f'/local-publications/{volume_id}', json=dict(
            title='Civil War Ultimate Edition', year=2025, publisher='Fixture', description='Manual',
            issues=[dict(id=row['id'], number=row['number'], title='Fourth' if row['number']=='4' else '')
                    for row in publication['issues']]))
        self.assertEqual(edit.status_code, 200, edit.get_json())
        self.assertEqual(edit.get_json()['result']['issues'][3]['title'], 'Fourth')
        unsafe = self.api('PUT', f'/local-publications/{volume_id}', json=dict(
            title='Civil War Ultimate Edition', year=2025, publisher='', description='',
            issues=[dict(id=row['id'], number=row['number'], title='') for row in publication['issues'][:3]],
            allow_remove=True))
        self.assertEqual(unsafe.status_code, 409)
        cover = self.client.post(f'/api/volumes/{volume_id}/manual-cover',
            data={'file':(BytesIO(image_bytes()), 'cover.png')}, content_type='multipart/form-data',
            query_string={'api_key':FAKE_APP_KEY})
        self.assertEqual(cover.status_code, 200, cover.get_json())
        detail = self.api('GET', f'/volumes/{volume_id}?metadata=true&issue_facts=1')
        self.assertEqual(detail.status_code, 200, detail.get_json())
        self.assertEqual(detail.get_json()['result']['cover_source'], 'manual')
        self.assertEqual(detail.get_json()['result']['issues'][3]['availability'], 'owned')
        listing = self.api('GET', '/volumes?limit=100&offset=0&metadata=true')
        self.assertEqual(listing.status_code, 200, listing.get_json())
        self.assertIn(volume_id, [row['id'] for row in listing.get_json()['result']])
        from backend.internals.provider_identity import ProviderIdentityDB
        self.assertEqual(ProviderIdentityDB.audit(('comicvine', 'metron', 'gcd')), [])
        self.disk_snapshot()
        denied = self.api('POST', f'/volumes/{volume_id}/release-search', json={})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.get_json()['result']['code'], 'local_publication_manually_managed')
        with patch('backend.implementations.volumes._refresh_provider_group',
                   side_effect=AssertionError('remote refresh')):
            refresh_and_scan(volume_id=None)

    def test_ignore_import_race_and_replacement_are_safe(self):
        path = self.comic_file(name='Civil War Ultimate Edition #03.cbz')
        before = Path(path).read_bytes()
        publication = self.create([path])
        ignored = self.api('POST', '/libraryimport/ignore', json={'files':[path]})
        self.assertEqual(ignored.get_json()['result'][0]['status'], 'ignored')
        choice = dict(source=path, provider='local', provider_id=publication['provider_id'],
                      issue_ids=[publication['issues'][2]['provider_id']], mode='keep')
        denied = self.api('POST', '/libraryimport/submit', json={'files':[choice]})
        self.assertEqual(denied.get_json()['result']['results'][0]['reason'], 'source_is_ignored')
        self.assertEqual(Path(path).read_bytes(), before)
        Path(path).write_bytes(before + b' changed content')
        imported = self.api('POST', '/libraryimport/submit', json={'files':[choice]})
        self.assertEqual(imported.get_json()['result']['results'][0]['status'], 'imported')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ignored_import_files').fetchone()[0], 0)
        cannot_ignore_owned = self.api('POST', '/libraryimport/ignore', json={'files':[path]})
        self.assertEqual(cannot_ignore_owned.get_json()['result'][0]['reason'], 'already_in_library')
        self.assertEqual(Path(path).read_bytes(), before + b' changed content')

    def test_duplicate_titles_keep_distinct_local_authority(self):
        first = self.comic_file(name='Civil War Ultimate Edition #03.cbz')
        second = self.comic_file(folder='other', name='Civil War Ultimate Edition #04.cbz')
        one, two = self.create([first]), self.create([second])
        self.assertNotEqual(one['provider_id'], two['provider_id'])
        folders = self.db.execute("SELECT folder FROM volumes WHERE metadata_provider='local' ORDER BY id").fetchall()
        self.assertNotEqual(folders[0][0], folders[1][0])
        wrong = self.api('POST', '/libraryimport/submit', json={'files':[dict(
            source=first, provider='local', provider_id=two['provider_id'],
            issue_ids=[one['issues'][2]['provider_id']], mode='keep')]})
        self.assertEqual(wrong.get_json()['result']['results'][0]['reason'], 'issue_not_in_publication')

    def test_ignore_rejects_outside_root_and_failed_write_keeps_archive(self):
        path = self.comic_file()
        oversized = self.api('POST', '/libraryimport/ignore',
                             data='x' * (512 * 1024 + 1), content_type='application/json')
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(self.api('GET', '/libraryimport/ignored?offset=1000001').status_code, 400)
        outside = str(self.root.parent / 'not-a-comic.cbz')
        response = self.api('POST', '/libraryimport/ignore', json={'files':[outside]})
        self.assertEqual(response.get_json()['result'][0]['status'], 'failed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ignored_import_files').fetchone()[0], 0)
        before = Path(path).read_bytes()
        self.db.execute("""CREATE TRIGGER block_ignore BEFORE INSERT ON ignored_import_files
            BEGIN SELECT RAISE(FAIL,'fixture write failure'); END""")
        failed = self.api('POST', '/libraryimport/ignore', json={'files':[path]})
        self.assertGreaterEqual(failed.status_code, 400)
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ignored_import_files').fetchone()[0], 0)

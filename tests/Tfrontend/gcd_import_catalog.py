"""GCD snapshot identities in the local Library Import catalogue."""

import sqlite3
from contextlib import closing
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from fixtures.comicvine_search import FAKE_APP_KEY
from Tbackend.implementations.gcd_client import FakeGcd
from Tfrontend.library_import_contract import ImportAPIHarness

from backend.implementations.metadata.gcd import GcdMetadataProvider
from backend.implementations.metadata.gcd_client import GcdClient

ISSUE_IDS = ('91001', '91002', '91003')


class GcdImportCatalog(ImportAPIHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeGcd()
        self.addCleanup(self.fake.close)
        self.fake.series['name'] = 'Batman and Robin'
        self.fake.series['year_began'] = 2025
        self.fake.series['active_issues'] = [self.fake.base + f'issue/{i}/' for i in ISSUE_IDS]
        for issue_id, number in zip(ISSUE_IDS, ('1', '[nn]', '1A')):
            row = deepcopy(self.fake.issues['1'])
            row.update(api_url=self.fake.base + f'issue/{issue_id}/', number=number,
                       title=f'Fixture issue {issue_id}')
            self.fake.issues[str(issue_id)] = row
        self.provider = GcdMetadataProvider(lambda: GcdClient(base=self.fake.base,
            session=self.fake.session(), charge=lambda: None,
            preflight=lambda count: None, limited=lambda value: None))
        self.start_patch('backend.features.import_issue_catalog.get_volume_provider',
                         return_value=self.provider)
        self.start_patch('backend.features.direct_library_import.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.implementations.issue_transport.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.features.download_queue.DownloadHandler').return_value.download_for_volume_queued.return_value = False
        self.start_patch('backend.features.tasks.TaskHandler.task_for_volume_running', return_value=False)

    def test_unregistered_snapshot_exposes_real_issue_ids_and_reuses_cache(self):
        path = self.comic_file(name='Batman and Robin (2025) - v001.cbz')
        body = {'provider': 'gcd', 'provider_id': '1', 'files': [path]}
        with patch('backend.internals.db.DBConnection.default_file', str(self.root / 'catalog.db')):
            with closing(sqlite3.connect(self.root / 'catalog.db')) as disk:
                self.db.backup(disk)
            first = self.client.post('/api/libraryimport/catalog', json=body,
                                     query_string={'api_key': FAKE_APP_KEY})
            self.assertEqual(first.status_code, 200, first.get_json())
            result = first.get_json()['result']
            self.assertEqual((result['loaded_issue_count'], result['reported_issue_count'],
                              result['completeness']), (3, 3, 'complete'))
            self.assertEqual([(i['id'], i['number']) for i in result['issues']],
                             [('91001', '1'), ('91002', '[nn]'), ('91003', '1A')])
            self.assertTrue(all(i['date'] is None for i in result['issues']))
            self.assertEqual(result['proposals'][path]['issue_ids'], [])
            requests = len(self.fake.requests)
            again = self.client.post('/api/libraryimport/catalog', json=body,
                                     query_string={'api_key': FAKE_APP_KEY})
            self.assertEqual(again.status_code, 200)
            self.assertEqual(len(self.fake.requests), requests)

            # The direct importer sees the same database-scoped cached snapshot.
            self.db.db_file = str(self.root / 'catalog.db')
            imported = self.client.post('/api/libraryimport/submit', json={'files': [{
                'source': path, 'provider': 'gcd', 'provider_id': '1',
                'issue_ids': ['91002'], 'mode': 'keep'}]}, query_string={'api_key': FAKE_APP_KEY})
            self.assertEqual(imported.status_code, 200, imported.get_json())
            self.assertEqual(imported.get_json()['result']['results'][0]['status'], 'imported')
            self.assertEqual(len(self.fake.requests), requests)
            self.assertEqual(self.db.execute('''SELECT e.provider_id FROM issues_files b
                JOIN issue_external_ids e ON e.issue_id=b.issue_id''').fetchall(), [('91002',)])
            self.assertEqual(self.db.execute('''SELECT i.issue_number,i.calculated_issue_number
                FROM issues i JOIN issue_external_ids e ON e.issue_id=i.id
                WHERE e.provider='gcd' AND e.provider_id='91002' ''').fetchone(), ('[nn]', None))
            second = self.comic_file(folder='incoming', name='Batman and Robin - Issue 1.cbz')
            moved = self.client.post('/api/libraryimport/submit', json={'files': [{
                'source': second, 'provider': 'gcd', 'provider_id': '1',
                'issue_ids': ['91001'], 'mode': 'move', 'filename': 'Batman and Robin 001.cbz'}]},
                query_string={'api_key': FAKE_APP_KEY})
            self.assertEqual(moved.status_code, 200, moved.get_json())
            self.assertEqual(moved.get_json()['result']['results'][0]['status'], 'imported')
            self.assertEqual(len(self.fake.requests), requests)
            self.assertEqual(set(self.db.execute('''SELECT e.provider_id FROM issues_files b
                JOIN issue_external_ids e ON e.issue_id=b.issue_id''').fetchall()), {('91001',), ('91002',)})
            self.db.commit()
            with closing(sqlite3.connect(self.root / 'catalog.db')) as disk:
                self.db.backup(disk)
            registered = self.client.post('/api/libraryimport/catalog', json=body,
                                          query_string={'api_key': FAKE_APP_KEY})
            self.assertEqual(registered.status_code, 200, registered.get_json())
            local = registered.get_json()['result']
            self.assertIsNotNone(local['volume_id'])
            self.assertEqual({i['id'] for i in local['issues'] if i['owned']}, {'91001', '91002'})
            volume = self.client.get('/api/volumes/' + str(local['volume_id']),
                query_string={'api_key': FAKE_APP_KEY, 'metadata': 'true', 'issue_facts': '1'})
            self.assertEqual(volume.status_code, 200, volume.get_json())
            self.assertEqual({i['issue_number'] for i in volume.get_json()['result']['issues']
                              if i['availability'] == 'owned'}, {'1', '[nn]'})
            self.assertEqual(len(self.fake.requests), requests)

    def test_failed_snapshot_does_not_create_local_publication(self):
        path = self.comic_file(name='Batman and Robin (2025) - v001.cbz')
        database = self.root / 'failed-catalog.db'
        with closing(sqlite3.connect(database)) as disk:
            self.db.backup(disk)
        self.fake.issues.pop('91002')
        with patch('backend.internals.db.DBConnection.default_file', str(database)):
            response = self.client.post('/api/libraryimport/catalog', json={
                'provider': 'gcd', 'provider_id': '1', 'files': [path]},
                query_string={'api_key': FAKE_APP_KEY})
        self.assertEqual(response.status_code, 503)
        with closing(sqlite3.connect(database)) as disk:
            self.assertEqual(disk.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_unique_comicinfo_label_suggests_exact_gcd_issue(self):
        path = self.comic_file(name='Batman and Robin - Collected Edition.cbz')
        with ZipFile(path, 'w') as archive:
            archive.writestr('ComicInfo.xml',
                '<ComicInfo><Series>Batman and Robin</Series><Number>1A</Number></ComicInfo>')
        database = self.root / 'comicinfo-catalog.db'
        with closing(sqlite3.connect(database)) as disk:
            self.db.backup(disk)
        with patch('backend.internals.db.DBConnection.default_file', str(database)):
            response = self.client.post('/api/libraryimport/catalog', json={
                'provider': 'gcd', 'provider_id': '1', 'files': [path]},
                query_string={'api_key': FAKE_APP_KEY})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['result']['proposals'][path]['issue_ids'], ['91003'])

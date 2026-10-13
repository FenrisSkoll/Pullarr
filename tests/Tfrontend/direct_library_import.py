"""Real HTTP transport, SQLite links and disposable filesystem operations."""

import errno
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fixtures.comicvine_fetch import issue_response
from fixtures.comicvine_search import FAKE_APP_KEY, volume_response
from Tfrontend.library_import_contract import ImportAPIHarness

from backend.features.library_import import scan_library_import
from backend.internals.organization_reservations import path_key


class DirectLibraryImport(ImportAPIHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('backend.features.direct_library_import.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.implementations.issue_transport.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.features.download_queue.DownloadHandler').return_value.download_for_volume_queued.return_value = False
        self.start_patch('backend.features.tasks.TaskHandler.task_for_volume_running', return_value=False)

    def submit(self, path, mode='keep', issue='301', filename=None):
        choice = dict(source=path, provider='comicvine', provider_id='2127',
                      issue_ids=[issue], mode=mode)
        if filename is not None:
            choice['filename'] = filename
        return self.client.post('/api/libraryimport/submit',
                                json={'files': [choice]}, query_string={'api_key': FAKE_APP_KEY})

    def test_keep_in_place_registers_exact_issue_and_is_idempotent(self):
        path = self.comic_file(name='Brother Lono - The Deluxe Edition.cbz')
        first = self.submit(path)
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(first.get_json()['result']['results'][0]['status'], 'imported')
        self.assertEqual(self.bindings()[0][0], path)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        self.assertTrue(Path(path).is_file())
        again = self.submit(path)
        self.assertEqual(again.status_code, 200, again.get_json())
        self.assertEqual(again.get_json()['result']['results'][0]['status'], 'already_in_library')
        self.assertEqual(len(self.bindings()), 1)
        volume_id = first.get_json()['result']['results'][0]['volume_id']
        volume = self.client.get('/api/volumes/' + str(volume_id),
            query_string={'api_key': FAKE_APP_KEY, 'metadata':'true', 'issue_facts':'1'})
        self.assertEqual(volume.status_code, 200, volume.get_json())
        states = {row['issue_number']: row['availability'] for row in volume.get_json()['result']['issues']}
        self.assertEqual(states, {'1':'owned', '2':'unknown'})

    def test_permission_failure_reports_safe_code_and_diagnostic(self):
        from backend.base.organization_job import (ExecutionCode,
                                                   OrganizationError)

        source = self.comic_file()
        with patch('backend.features.direct_library_import.import_one',
                   side_effect=OrganizationError(ExecutionCode.PERMISSION, 'OperationalError')), \
                patch('backend.features.direct_library_import.LOGGER.warning') as warning:
            result = self.submit(source, 'move').get_json()['result']['results'][0]
        self.assertEqual(result['reason'], 'permission_denied')
        self.assertIn('OperationalError', str(warning.call_args))
        self.assertNotIn(source, str(warning.call_args))

    def test_future_issue_is_upcoming_and_not_automatically_wanted(self):
        from backend.internals.wanted import WantedStore

        source = self.comic_file()
        imported = self.submit(source).get_json()['result']['results'][0]
        volume_id = imported['volume_id']
        other = self.db.execute('SELECT id FROM issues WHERE volume_id=? AND issue_number=?',
                                (volume_id, '2')).fetchone()[0]
        self.db.execute('UPDATE issue_number_facts SET selected_date_field=? WHERE issue_id=?',
                        ('release_date', other))
        self.db.execute('''INSERT INTO issue_date_facts
            (issue_id,source_field,raw_value,kind,provenance,year,month,day,precision,
             zero_placeholders,uncertainty,policy)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (other, 'release_date', '2099-01-01', 'on_sale', 'fixture', 2099, 1, 1,
             'day', 0, None, 'kapowarr-bibliographic-date/v1'))
        self.db.commit()
        volume = self.client.get('/api/volumes/' + str(volume_id), query_string={
            'api_key': FAKE_APP_KEY, 'metadata':'true', 'issue_facts':'1'})
        states = {row['issue_number']: row['availability'] for row in volume.get_json()['result']['issues']}
        self.assertEqual(states, {'1':'owned', '2':'upcoming'})
        with TemporaryDirectory() as temporary:
            database = str(Path(temporary) / 'wanted.db')
            with closing(sqlite3.connect(database)) as snapshot:
                self.db.backup(snapshot)
            with closing(WantedStore(database)) as wanted:
                self.assertEqual([row['id'] for row in wanted.due(limit=10)], [])
                self.assertNotIn(other, wanted.missing_members(volume_id))
                wanted.db.execute('''UPDATE issue_date_facts SET raw_value='2000-01-01',year=2000
                    WHERE issue_id=? AND source_field='release_date' ''', (other,))
                self.assertEqual([row['id'] for row in wanted.due(limit=10)], [other])
        self.db.execute('''UPDATE issue_date_facts SET raw_value='2000-01-01',year=2000
            WHERE issue_id=? AND source_field='release_date' ''', (other,))
        self.db.commit()
        volume = self.client.get('/api/volumes/' + str(volume_id), query_string={
            'api_key': FAKE_APP_KEY, 'metadata':'true', 'issue_facts':'1'})
        states = {row['issue_number']: row['availability'] for row in volume.get_json()['result']['issues']}
        self.assertEqual(states, {'1':'owned', '2':'missing'})

    def test_move_exclusive_and_target_conflict(self):
        source = self.comic_file(name='Adventure Time Book 1.cbz')
        response = self.submit(source, 'move', filename='Adventure Time 001.cbz')
        self.assertEqual(response.status_code, 200, response.get_json())
        row = response.get_json()['result']['results'][0]
        self.assertEqual(row['status'], 'imported', row)
        self.assertTrue(Path(row['target']).is_file())
        self.assertFalse(Path(source).exists())
        self.assertEqual(self.bindings()[0][0], row['target'])

        second = self.comic_file(folder='elsewhere', name='Another.cbz')
        conflict = self.submit(second, 'move', filename='Adventure Time 001.cbz')
        self.assertEqual(conflict.status_code, 200)
        self.assertTrue(conflict.get_json()['result']['results'][0]['reason'].startswith('target_exists:'))
        self.assertTrue(Path(second).is_file())

    def test_brother_lono_exact_comicvine_issue_without_title_override(self):
        self.prepare_fetch(volume=volume_response(name='100 Bullets - Brother Lono - The Deluxe Edition',
            start_year='2024', count_of_issues='1'),
            issues=[issue_response(id=1068064, issue_number='1')])
        source = self.comic_file(name='100 Bullets - Brother Lono - The Deluxe Edition (2024) - Hard-Cover.cbz')
        kept = self.submit(source, issue='1068064').get_json()['result']['results'][0]
        self.assertEqual(kept['status'], 'imported', kept)
        self.assertEqual(self.bindings()[0][0], source)
        self.assertEqual(self.bindings()[0][2], 1068064)
        with patch('backend.features.direct_library_import.os.link',
                   side_effect=OSError(errno.EOPNOTSUPP, 'unsupported')):
            moved = self.submit(source, 'move', issue='1068064',
                filename='Brother Lono Deluxe 001.cbz').get_json()['result']['results'][0]
        self.assertEqual(moved['status'], 'imported', moved)
        self.assertEqual(self.bindings()[0][0], moved['target'])
        self.assertEqual(self.bindings()[0][2], 1068064)
        self.assertFalse(Path(source).exists())

    def test_adventure_time_volume_one_imports_with_one_submit(self):
        self.prepare_fetch(volume=volume_response(name='Adventure Time',
            start_year='2012', count_of_issues='1'),
            issues=[issue_response(id=998877, issue_number='1', name='Volume 1')])
        source = self.comic_file(name='Adventure Time (2012) Book 1.cbz')
        response = self.submit(source, issue='998877')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['result']['results'][0]['status'], 'imported')
        self.assertEqual(self.bindings()[0][2], 998877)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM library_import_drafts').fetchone()[0], 0)

    def test_unsupported_hardlink_uses_exclusive_verified_copy(self):
        source = self.comic_file(name='No link.cbz')
        with patch('backend.features.direct_library_import.os.link',
                   side_effect=OSError(errno.EOPNOTSUPP, 'unsupported')):
            response = self.submit(source, 'move', filename='Copied.cbz')
        row = response.get_json()['result']['results'][0]
        self.assertEqual(row['status'], 'imported', row)
        self.assertEqual(Path(row['target']).read_bytes(), b'deterministic disposable comic placeholder')
        self.assertFalse(Path(source).exists())
        again = self.submit(source, 'move', filename='Copied.cbz')
        self.assertEqual(again.get_json()['result']['results'][0]['status'], 'already_in_library')

    def test_interrupted_copy_keeps_source_and_retry_succeeds(self):
        source = self.comic_file(name='Disk full.cbz')
        original_link = os.link
        def cross_device(from_path, to_path, **kwargs):
            if from_path == source:
                raise OSError(errno.EXDEV, 'cross-device')
            return original_link(from_path, to_path, **kwargs)
        with patch('backend.features.direct_library_import.os.link', side_effect=cross_device), \
                patch('backend.features.direct_library_import.os.fsync',
                      side_effect=OSError(errno.ENOSPC, 'disk full')):
            failed = self.submit(source, 'move', filename='Retried.cbz')
        self.assertEqual(failed.get_json()['result']['results'][0]['status'], 'failed')
        self.assertTrue(Path(source).is_file())
        self.assertFalse(list(self.root.rglob('*.part')))
        with patch('backend.features.direct_library_import.os.link', side_effect=cross_device):
            retried = self.submit(source, 'move', filename='Retried.cbz')
        row = retried.get_json()['result']['results'][0]
        self.assertEqual(row['status'], 'imported', row)
        self.assertTrue(Path(row['target']).is_file())

    def test_one_invalid_file_does_not_block_next(self):
        path = self.comic_file()
        choice = dict(source=path, provider='comicvine', provider_id='2127',
                      issue_ids=['301'], mode='keep')
        response = self.client.post('/api/libraryimport/submit',
            json={'files':[dict(choice, source=str(self.root / 'missing.cbz')), choice]},
            query_string={'api_key':FAKE_APP_KEY})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual([r['status'] for r in response.get_json()['result']['results']],
                         ['failed', 'imported'])
        self.assertEqual(len(self.bindings()), 1)

    def test_one_failed_row_does_not_stop_ninety_nine(self):
        folder = self.root / 'batch'
        folder.mkdir()
        choices = [dict(source=str(folder / 'missing.cbz'), provider='comicvine',
                        provider_id='2127', issue_ids=['301'], mode='keep')]
        for number in range(99):
            path = folder / f'Issue {number:03d}.cbz'
            path.write_bytes(b'fixture')
            choices.append(dict(choices[0], source=str(path)))
        response = self.client.post('/api/libraryimport/submit',
            json={'files':choices}, query_string={'api_key':FAKE_APP_KEY})
        self.assertEqual(response.status_code, 200, response.get_json())
        statuses = [row['status'] for row in response.get_json()['result']['results']]
        self.assertEqual(statuses.count('failed'), 1)
        self.assertEqual(statuses.count('imported'), 99)
        self.assertEqual(len(self.bindings()), 99)

    def test_explicit_reassociation_in_mixed_folder(self):
        first = self.comic_file(folder='mixed', name='Book 1.cbz')
        second = self.comic_file(folder='mixed', name='Book 2.cbz')
        self.assertEqual(self.submit(first).get_json()['result']['results'][0]['status'], 'imported')
        self.assertEqual(self.submit(second, issue='302').get_json()['result']['results'][0]['status'], 'imported')
        self.assertEqual({row[0] for row in self.bindings()}, {first, second})
        self.assertEqual({row[2] for row in self.bindings()}, {301, 302})
        corrected = self.submit(first, issue='302')
        self.assertEqual(corrected.get_json()['result']['results'][0]['status'], 'imported')
        self.assertEqual({row[2] for row in self.bindings()}, {302})
        from backend.implementations.file_matching import scan_files

        scan_files(self.rows('volumes')[0]['id'])
        self.assertEqual({row[2] for row in self.bindings()}, {302})

    def test_mixed_folder_can_keep_files_for_distinct_publications(self):
        first = self.comic_file(folder='mixed-series', name='Batman Book 1.cbz')
        second = self.comic_file(folder='mixed-series', name='American Gods Book 1.cbz')
        original = self.submit(first).get_json()['result']['results'][0]
        self.prepare_fetch(volume=volume_response(id=9182, name='American Gods'),
            issues=[issue_response(id=8654, volume={'id':9182})])
        other = self.client.post('/api/libraryimport/submit', json={'files':[dict(
            source=second, provider='comicvine', provider_id='9182',
            issue_ids=['8654'], mode='keep')]},
            query_string={'api_key':FAKE_APP_KEY}).get_json()['result']['results'][0]
        self.assertEqual(other['status'], 'imported', other)
        self.assertNotEqual(original['volume_id'], other['volume_id'])
        self.assertEqual({row[0] for row in self.bindings()}, {first, second})
        self.assertEqual({row[2] for row in self.bindings()}, {301, 8654})

    def test_file_already_in_canonical_folder_registers_without_rename(self):
        from backend.implementations.volumes import Volume

        volume_id = self.add_volume()
        folder = Path(Volume(volume_id).get_data().folder)
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / 'Already Here.cbz'
        source.write_bytes(b'canonical fixture')
        response = self.submit(str(source), 'move', filename=source.name)
        row = response.get_json()['result']['results'][0]
        self.assertEqual(row['status'], 'imported', row)
        self.assertEqual(row['target'], str(source))
        self.assertEqual(source.read_bytes(), b'canonical fixture')
        self.assertEqual(self.bindings()[0][0], str(source))

    def test_metadata_only_delete_keeps_files(self):
        path = self.comic_file()
        imported = self.submit(path).get_json()['result']['results'][0]
        self.assertEqual(imported['status'], 'imported')
        response = self.client.delete('/api/volumes/' + str(imported['volume_id']),
                                      query_string={'api_key':FAKE_APP_KEY, 'delete_folder':'false'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.rows('volumes'), [])
        self.assertEqual(self.bindings(), [])

    def test_empty_and_failed_import_publications_can_be_removed(self):
        empty_id = self.add_volume()
        removed = self.client.delete('/api/volumes/' + str(empty_id),
            query_string={'api_key':FAKE_APP_KEY, 'delete_folder':'false'})
        self.assertEqual(removed.status_code, 200, removed.get_json())
        source = self.comic_file(name='Unmoved.cbz')
        self.prepare_fetch()
        self.add_volume()
        from backend.implementations.volumes import Volume
        volume_id = self.rows('volumes')[0]['id']
        folder = Path(Volume(volume_id).get_data().folder)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / 'Occupied.cbz'
        target.write_bytes(b'pre-existing')
        failed = self.submit(source, 'move', filename='Occupied.cbz')
        self.assertEqual(failed.get_json()['result']['results'][0]['status'], 'failed')
        removed = self.client.delete('/api/volumes/' + str(volume_id),
            query_string={'api_key':FAKE_APP_KEY, 'delete_folder':'false'})
        self.assertEqual(removed.status_code, 200, removed.get_json())
        self.assertEqual(target.read_bytes(), b'pre-existing')
        self.assertTrue(Path(source).is_file())

    def test_inactive_legacy_claim_can_be_retired_for_metadata_delete(self):
        path = self.comic_file()
        volume_id = self.add_volume()
        self._claim('old-inert', volume_id, path, 'recovery_required')
        response = self.client.delete('/api/volumes/' + str(volume_id),
            query_string={'api_key':FAKE_APP_KEY, 'delete_folder':'false'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.db.execute('SELECT state FROM organization_jobs WHERE id=?',
                                         ('old-inert',)).fetchone()[0], 'failed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0], 0)

    def test_active_legacy_claim_still_blocks_metadata_delete(self):
        path = self.comic_file()
        volume_id = self.add_volume()
        self._claim('old-active', volume_id, path, 'running')
        response = self.client.delete('/api/volumes/' + str(volume_id),
            query_string={'api_key':FAKE_APP_KEY, 'delete_folder':'false'})
        self.assertNotEqual(response.status_code, 200)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(len(self.rows('volumes')), 1)

    def test_failed_inert_legacy_claim_does_not_block_metadata_delete(self):
        path = self.comic_file()
        volume_id = self.add_volume()
        self._claim('old-failed', volume_id, path, 'failed')
        response = self.client.delete('/api/volumes/' + str(volume_id),
            query_string={'api_key':FAKE_APP_KEY, 'delete_folder':'false'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0], 0)

    def test_succeeded_legacy_file_effect_keeps_claim_ambiguous(self):
        path = self.comic_file()
        volume_id = self.add_volume()
        self._claim('old-effect', volume_id, path, 'recovery_required')
        self.db.execute('''INSERT INTO organization_steps(job_id,ordinal,kind,state)
            VALUES(?,?,?,?)''', ('old-effect', 1, 'register_or_update_file_record', 'succeeded'))
        self.db.commit()
        imported = self.submit(path).get_json()['result']['results'][0]
        self.assertEqual(imported['reason'], 'file_busy_or_recovery_ambiguous')
        deleted = self.client.delete('/api/volumes/' + str(volume_id),
            query_string={'api_key':FAKE_APP_KEY, 'delete_folder':'false'})
        self.assertEqual(deleted.status_code, 409)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.db.execute('SELECT state FROM organization_jobs WHERE id=?',
                                         ('old-effect',)).fetchone()[0], 'recovery_required')

    def test_direct_import_retires_only_proven_inactive_claim(self):
        source = self.comic_file()
        volume_id = self.add_volume()
        self._claim('old-inert-import', volume_id, source, 'recovery_required')
        imported = self.submit(source).get_json()['result']['results'][0]
        self.assertEqual(imported['status'], 'imported', imported)
        self.assertEqual(self.db.execute('SELECT state FROM organization_jobs WHERE id=?',
                                         ('old-inert-import',)).fetchone()[0], 'failed')
        second = self.comic_file(name='Second.cbz')
        self._claim('old-active-import', volume_id, second, 'running')
        blocked = self.submit(second).get_json()['result']['results'][0]
        self.assertEqual(blocked['status'], 'failed')
        self.assertEqual(blocked['reason'], 'file_busy_or_recovery_ambiguous')
        self.assertTrue(Path(second).is_file())

    def _claim(self, identifier, volume_id, source, state):
        target = source + '.pending'
        intent = json.dumps(dict(library_import=True, volume_id=volume_id,
                                 source=source, target=target))
        self.db.execute('''INSERT INTO organization_jobs(id,plan_digest,executor_version,
            intent,intent_digest,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)''',
            (identifier, identifier, 'test', intent, identifier, state, '2025-01-01', '2025-01-01'))
        self.db.execute('INSERT INTO organization_reservations(path_key,job_id) VALUES(?,?)',
                        (path_key(source), identifier))
        self.db.commit()


class LargeLocalDiscovery(TestCase):
    def test_all_904_folders_and_2924_archives_are_paged(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Comics'
            root.mkdir()
            database = str(Path(temporary) / 'library.db')
            with closing(sqlite3.connect(database)) as db:
                db.executescript('''CREATE TABLE root_folders(id INTEGER PRIMARY KEY,folder TEXT);
                    CREATE TABLE files(id INTEGER PRIMARY KEY,filepath TEXT);''')
                db.execute('INSERT INTO root_folders VALUES(1,?)', (str(root),))
                db.commit()
            for index in range(2924):
                folder = root / f'Folder {index % 904:04d}'
                folder.mkdir(exist_ok=True)
                (folder / f'Issue {index:04d}.cbz').write_bytes(b'')
            seen = set()
            offset = 0
            while offset is not None:
                page = scan_library_import(database, offset=offset)
                seen.update(item['filepath'] for item in page['items'])
                offset = page['next_offset']
            self.assertEqual(len(seen), 2924)
            self.assertEqual(page['counts']['scanned_folders'], 905)
            self.assertEqual(page['counts']['not_yet_scanned_folders'], 0)
            self.assertTrue(page['enumeration_complete'])
            partial = scan_library_import(database, folder_limit=500)
            self.assertEqual(partial['counts']['scanned_folders'], 500)
            self.assertEqual(partial['counts']['not_yet_scanned_folders'], 405)
            self.assertFalse(partial['enumeration_complete'])
            self.assertEqual(scan_library_import(database, folder_limit=1000)['counts']['discovered'], 2924)
            self.assertEqual(scan_library_import(database, folder_limit=50000)['counts']['discovered'], 2924)

    def test_depth_bound_does_not_claim_complete_scan(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Comics'
            root.mkdir()
            database = str(Path(temporary) / 'library.db')
            with closing(sqlite3.connect(database)) as db:
                db.executescript('''CREATE TABLE root_folders(id INTEGER PRIMARY KEY,folder TEXT);
                    CREATE TABLE files(id INTEGER PRIMARY KEY,filepath TEXT);''')
                db.execute('INSERT INTO root_folders VALUES(1,?)', (str(root),))
                db.commit()
            current = root
            for _ in range(33):
                current = current / 'd'
                current.mkdir()
            (current / 'hidden.cbz').write_bytes(b'fixture')
            result = scan_library_import(database, folder_limit=50000)
            self.assertTrue(result['counts']['limited'])
            self.assertFalse(result['enumeration_complete'])
            self.assertEqual(result['counts']['discovered'], 0)


class ScopedSuggestions(ImportAPIHarness, TestCase):
    def test_bounded_authenticated_proposal_never_registers(self):
        source = self.comic_file(name='Book 1.cbz')
        database = self.root / 'suggestions.db'
        with closing(sqlite3.connect(database)) as disk:
            self.db.backup(disk)
        async def matched(groups, only_english):
            self.assertTrue(only_english)
            return {number:dict(id=2127,title='Books',issue_count=3,link='https://comicvine.gamespot.com')
                    for number in groups}
        with patch('backend.internals.db.DBConnection.default_file', str(database)), \
                patch('backend.features.library_import.ComicVine') as comicvine:
            comicvine.return_value.filenames_to_cvs.side_effect = matched
            response = self.client.post('/api/libraryimport/suggestions',
                json={'files':[source]}, query_string={'api_key':FAKE_APP_KEY})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['result'][0]['filepath'], source)
        self.assertEqual(self.rows('volumes'), [])
        self.assertEqual(self.rows('files'), [])
        with patch('backend.internals.db.DBConnection.default_file', str(database)):
            for paths in ([source] * 11, [str(Path(source).parent.parent / 'outside.cbz')]):
                rejected = self.client.post('/api/libraryimport/suggestions',
                    json={'files':paths}, query_string={'api_key':FAKE_APP_KEY})
                self.assertEqual(rejected.status_code, 400, rejected.get_json())

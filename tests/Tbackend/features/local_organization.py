"""Canonical local scan/import preview and exact journaled application."""

from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fixtures.library_import import ImportHarness
from Tbackend.features import organization_execution as fixture_module

from backend.features.local_organization import (apply_preview,
                                                 import_preview, scan_preview)
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.volumes import Library


class LocalOrganizationTests(TestCase):
    def setUp(self):
        self.fixture = fixture_module.ExecutionTests('test_pending_survives_reopen_without_mutation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        from backend.features.local_organization import _sessions
        self.addCleanup(_sessions.clear)
        self.fixture.db.execute("UPDATE config SET value=57 WHERE key='database_version'")

    def manual(self, rename):
        fixture = self.fixture
        # Library Import only admits configured roots, not arbitrary paths.
        fixture.folder.mkdir(exist_ok=True)
        source = fixture.folder / fixture.source.name
        fixture.source.rename(source)
        with patch('backend.internals.provider_identity.ProviderIdentityDB.find_selected_volume', return_value=1), \
                patch('backend.implementations.volumes.Library.add_metadata', side_effect=AssertionError('No provider registration')):
            result = import_preview(fixture.dbpath,
                [{'filepath': str(source), 'provider': 'comicvine', 'provider_id': '101'}], rename)
        self.assertEqual(result['plans'][0]['status'], 'ready', result)
        self.assertTrue(source.exists())
        self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        applied = apply_preview(fixture.dbpath, result['id'])
        self.assertEqual(applied['jobs'][0]['state'], 'completed', applied)
        self.assertTrue(Path(result['plans'][0]['target']).exists())
        self.assertEqual(fixture.db.execute('SELECT issue_id FROM issues_files').fetchall(), [(1,)])
        self.assertTrue(apply_preview(fixture.dbpath, result['id'])['replay'])
        self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
        return source, result

    def test_existing_publication_import_preserves_basename(self):
        source, result = self.manual(False)
        self.assertEqual(Path(result['plans'][0]['target']).name, source.name)

    def test_batman_three_books_local_authority_and_partial_review(self):
        from zipfile import ZipFile
        fixture = self.fixture
        fixture.db.execute("UPDATE volumes SET title='Batman: Rebirth Deluxe Edition',year=2017")
        fixture.db.execute("UPDATE issues SET title='Book 1',date='2017-01-01'")
        for number in (2, 3):
            fixture.db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,title,date) VALUES(?,1,?,?,?,?,?)',
                (number, 200+number, str(number), number, f'Book {number}', '2017-01-01'))
        for number in (1, 2, 3):
            with ZipFile(fixture.folder / f'Batman - Rebirth Deluxe Edition (2017) - {number:03} - Book {number}.cbz', 'w') as archive:
                archive.writestr('page.jpg', b'unchanged synthetic page')
        with ZipFile(fixture.folder / 'Mystery.cbz', 'w') as archive:
            archive.writestr('page.jpg', b'unresolved page')
        preview = scan_preview(fixture.dbpath, 1)
        self.assertEqual([p['issue_ids'] for p in preview['plans'][:3]], [[1], [2], [3]], preview)
        self.assertTrue(all(p['volume_id'] == 1 for p in preview['plans']), preview)
        self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        applied = apply_preview(fixture.dbpath, preview['id'])
        self.assertEqual([j['state'] for j in applied['jobs']], ['completed']*3, applied)
        self.assertEqual(len(applied['review']), 1)
        self.assertEqual(fixture.db.execute('SELECT issue_id FROM issues_files ORDER BY issue_id').fetchall(), [(1,), (2,), (3,)])
        again = scan_preview(fixture.dbpath, 1)
        self.assertTrue(all(p['status'] != 'ready' for p in again['plans']), again)

    def test_local_scan_conflicting_title_keeps_local_volume_context(self):
        source = self.fixture.folder / 'Superman 001 (2020).cbz'
        self.fixture.source.rename(source)
        result = scan_preview(self.fixture.dbpath, 1)
        self.assertEqual(result['plans'][0]['volume_id'], 1, result)
        self.assertEqual(result['plans'][0]['status'], 'review_required', result)
        self.assertEqual(apply_preview(self.fixture.dbpath, result['id'])['jobs'], [])

    def test_import_different_immediate_parents_never_adopts_common_ancestor(self):
        first = self.fixture.folder / self.fixture.source.name
        self.fixture.source.rename(first)
        second = self.fixture.library / 'Another'
        second.mkdir()
        copy = second / first.name
        copy.write_bytes(first.read_bytes())
        with patch('backend.implementations.volumes.Library.add_metadata', side_effect=AssertionError('must not register')):
            result = import_preview(self.fixture.dbpath, [dict(filepath=str(p), provider='comicvine', provider_id='101') for p in (first,copy)], False)
        self.assertTrue(all(p['identification_reasons']==['folder_assignment_required'] for p in result['plans']), result)
        self.assertEqual(self.fixture.db.execute('SELECT folder FROM volumes').fetchone()[0], str(self.fixture.folder))

    def test_existing_publication_import_and_rename(self):
        self.manual(True)

    def test_conservative_scan_adds_only_through_job_preserves_missing(self):
        fixture = self.fixture
        fixture.folder.mkdir(exist_ok=True)
        local = fixture.folder / fixture.source.name
        fixture.source.rename(local)
        missing = str(fixture.folder / 'missing.cbz')
        fixture.db.execute('INSERT INTO files(id,filepath,size) VALUES(9,?,1)', (missing,))
        result = scan_preview(fixture.dbpath, 1)
        self.assertTrue(result['enumeration_complete'])
        self.assertEqual(result['plans'][0]['source'], result['plans'][0]['target'])
        applied = apply_preview(fixture.dbpath, result['id'])
        self.assertEqual(applied['jobs'][0]['state'], 'completed', applied)
        self.assertTrue(local.exists())
        self.assertEqual(fixture.db.execute('SELECT filepath FROM files WHERE id=9').fetchone()[0], missing)

    def test_wrong_local_artifact_not_forced_by_import_selection(self):
        fixture = self.fixture
        source = fixture.library / 'Other 006 (2020).cbz'
        fixture.source.rename(source)
        with patch('backend.internals.provider_identity.ProviderIdentityDB.find_selected_volume', return_value=1):
            result = import_preview(fixture.dbpath,
                [{'filepath': str(source), 'provider': 'comicvine', 'provider_id': '101'}], False)
        self.assertNotEqual(result['plans'][0]['status'], 'ready')
        self.assertEqual(apply_preview(fixture.dbpath, result['id'])['jobs'], [])
        self.assertTrue(source.exists())

    def test_explicit_new_registration_precedes_preview_without_file_effects(self):
        fixture = self.fixture
        fixture.folder.mkdir(exist_ok=True)
        source = fixture.folder / fixture.source.name
        fixture.source.rename(source)
        def registration(identity, root_id, monitored, **options):
            self.assertTrue(source.exists())
            self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
            self.assertTrue(options['organizer_registration'])
            self.assertEqual(identity.provider, 'comicvine')
            return 1
        with patch('backend.internals.provider_identity.ProviderIdentityDB.find_selected_volume', return_value=None), \
                patch('backend.implementations.volumes.Library.add_metadata', side_effect=registration), \
                patch('backend.internals.db.commit'):
            result = import_preview(fixture.dbpath,
                [{'filepath': str(source), 'provider': 'comicvine', 'provider_id': '101'}], True)
        self.assertTrue(source.exists())
        self.assertEqual(apply_preview(fixture.dbpath, result['id'])['jobs'][0]['state'], 'completed')

    def test_production_import_api_preview_then_explicit_apply(self):
        from flask import Flask

        from frontend.api import api
        fixture = self.fixture
        fixture.folder.mkdir(exist_ok=True)
        source = fixture.folder / fixture.source.name
        fixture.source.rename(source)
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.internals.db.DBConnection.default_file', fixture.dbpath), \
                patch('backend.internals.provider_identity.ProviderIdentityDB.find_selected_volume', return_value=1):
            settings.return_value.sv.api_key = 'test-local-import-key'
            client = app.test_client()
            suffix = '?api_key=test-local-import-key'
            response = client.post('/api/libraryimport/preview' + suffix, json=[
                {'filepath': str(source), 'provider': 'comicvine', 'provider_id': '101'}])
            self.assertEqual(response.status_code, 200, response.json)
            self.assertTrue(source.exists())
            preview = response.json['result']
            route = '/api/local-organization/' + preview['id'] + '/apply' + suffix
            self.assertEqual(client.get(route).status_code, 405)
            applied = client.post(route)
            self.assertEqual(applied.status_code, 200, applied.json)
            self.assertEqual(applied.json['result']['jobs'][0]['state'], 'completed')
            self.assertEqual(fixture.db.execute('SELECT issue_id FROM issues_files').fetchall(), [(1,)])

    def test_cross_reference_does_not_authorize_registration_or_authority_switch(self):
        from backend.base.acquisition_intake import IntakeFailure
        fixture = self.fixture
        fixture.folder.mkdir(exist_ok=True)
        source = fixture.folder / fixture.source.name
        fixture.source.rename(source)
        with patch('backend.internals.provider_identity.ProviderIdentityDB.find_selected_volume', side_effect=ValueError), \
                patch('backend.implementations.volumes.Library.add_metadata', side_effect=AssertionError('authority switch')):
            result = import_preview(fixture.dbpath, [{'filepath': str(source), 'provider': 'metron', 'provider_id': '101'}], False)
            self.assertEqual(result['plans'][0]['identification_reasons'], ['publication_registration_conflict'])
            self.assertEqual(apply_preview(fixture.dbpath, result['id'])['jobs'], [])
        self.assertTrue(source.exists())
        self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)

    def test_pending_registration_cannot_be_legacy_relative_destination(self):
        from types import SimpleNamespace

        from backend.features.post_processing import PostProcessingContext
        context = PostProcessingContext(SimpleNamespace(files=[str(self.fixture.source)], volume_id=1))
        with patch('backend.features.post_processing.Volume', return_value=SimpleNamespace(vd=SimpleNamespace(folder=''))), \
                patch('backend.features.post_processing.rename_file', side_effect=AssertionError('relative move')), \
                patch('backend.features.post_processing.copy_directory', side_effect=AssertionError('relative copy')):
            for action in (context.move_to_dest, context.copy_file_torrent):
                with self.assertRaises(ValueError):
                    action()
        self.assertTrue(self.fixture.source.exists())


class RegistrationBoundaryTests(ImportHarness, TestCase):
    def test_selected_comicvine_identity_cannot_be_substituted_by_fetch(self):
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            Library.add_metadata(ProviderVolumeIdentity('comicvine', '999'), 1, True,
                                 volume_folder=str(self.root), organizer_registration=True)
        self.assert_empty_library()

    def test_legacy_empty_folder_has_bounded_public_diagnostic(self):
        from backend.implementations.volumes import Volume
        identifier = Library.add_metadata(ProviderVolumeIdentity('comicvine', '2127'), 1, True,
                                          volume_folder=str(self.root), organizer_registration=True)
        self.db.execute("UPDATE volumes SET folder='' WHERE id=?", (identifier,))
        data = Volume(identifier).get_public_data()
        self.assertEqual(data['volume_folder'], '')
        self.assertEqual(data['folder_diagnostic'], 'folder_not_established')

    def test_registration_adopts_existing_folder_atomically(self):
        folder = self.root / 'Batman - Rebirth Deluxe Edition (2017)'
        folder.mkdir()
        identifier = Library.add_metadata(ProviderVolumeIdentity('comicvine', '2127'), 1, True,
            volume_folder=str(folder), organizer_registration=True)
        row = self.db.execute('SELECT folder,custom_folder FROM volumes WHERE id=?', (identifier,)).fetchone()
        self.assertEqual(row, (str(folder), True))
        self.scan.assert_not_called()
        self.process.assert_not_called()

    def test_failed_folder_establishment_rolls_back_all_registration_rows(self):
        with self.assertRaises(Exception):
            Library.add_metadata(ProviderVolumeIdentity('comicvine', '2127'), 1, True,
                volume_folder=str(self.root / 'missing'), organizer_registration=True)
        self.assert_empty_library()

    def test_real_provider_registration_does_not_choose_or_mutate_folder(self):
        self.settings.create_empty_volume_folders = True
        identifier = Library.add_metadata(ProviderVolumeIdentity('comicvine', '2127'), 1, True,
                                          organizer_registration=True)
        self.assertEqual(self.db.execute('SELECT folder FROM volumes WHERE id=?', (identifier,)).fetchone()[0], '')
        self.scan.assert_not_called()
        self.process.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def file_database(self):
        import sqlite3
        from dataclasses import fields
        from zipfile import ZipFile

        from Tbackend.features.organization_plan import NAMING

        from backend.internals.db import DBConnection
        path = str(self.root / 'registration.db')
        with sqlite3.connect(path) as copied:
            self.db.backup(copied)
        copied.close()
        self.db = DBConnection(db_file=path)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO config VALUES('database_version',57)")
        self.db.executemany('INSERT INTO config VALUES(?,?)', ((f.name, getattr(NAMING, f.name)) for f in fields(NAMING)))
        self.db.commit()
        self.db_lookup.side_effect = self.db.cursor
        for target in ('backend.implementations.volumes.get_db', 'backend.internals.provider_identity.get_db',
                       'backend.implementations.metadata.identity_enrichment.get_db',
                       'frontend.metadata.get_db', 'backend.internals.db_models.get_db'):
            self.start_patch(target, side_effect=self.db.cursor)
        self.start_patch('backend.internals.db.commit', side_effect=self.db.commit)
        return path

    def test_batman_import_establishes_folder_and_three_issue_bindings(self):
        from zipfile import ZipFile

        from fixtures.comicvine_fetch import issue_response
        from fixtures.comicvine_search import volume_response

        from backend.implementations.volumes import Volume
        path = self.file_database()
        self.prepare_fetch(volume_response(id='103802', name='Batman: Rebirth Deluxe Edition',
            start_year='2017', count_of_issues='3', aliases='', deck=''),
            [issue_response(id=str(301+n), volume={'id':'103802'}, issue_number=str(n), name=f'Book {n}', cover_date='2017-01-01') for n in (1,2,3)])
        folder = self.root / 'Batman - Rebirth Deluxe Edition (2017)'
        folder.mkdir()
        sources = []
        for number in (1,2,3):
            source = folder / f'Batman - Rebirth Deluxe Edition (2017) - {number:03} - Book {number}.cbz'
            with ZipFile(source, 'w') as archive:
                archive.writestr('page.jpg', b'unchanged synthetic page')
            sources.append(source)
        matches = [dict(filepath=str(p), provider='comicvine', provider_id='103802') for p in sources]
        preview = import_preview(path, matches, False)
        row = self.db.execute('SELECT id,folder,custom_folder FROM volumes').fetchone()
        self.assertEqual(row[1:], (str(folder), True))
        self.assertEqual([p['issue_ids'] for p in preview['plans']], [[1],[2],[3]], preview)
        self.assertTrue(all(p['status']=='ready' for p in preview['plans']), preview)
        result = apply_preview(path, preview['id'])
        self.assertEqual([j['state'] for j in result['jobs']], ['completed']*3, result)
        self.assertTrue(all(p.exists() for p in sources))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 3)
        self.assertEqual(Volume(row[0]).get_public_data()['folder'], str(folder))
        retry = import_preview(path, matches, False)
        apply_preview(path, retry['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 3)
        self.scan.assert_not_called()
        self.process.assert_not_called()

    def test_real_registration_then_shared_organizer_in_disposable_database(self):
        from zipfile import ZipFile
        path = self.file_database()
        source = Path(self.comic_file())
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'disposable image')
        preview = import_preview(path, [{'filepath': str(source), 'provider': 'comicvine', 'provider_id': '2127'}], True)
        self.assertTrue(source.exists())
        self.scan.assert_not_called()
        self.process.assert_not_called()
        self.assertEqual(preview['plans'][0]['status'], 'ready', preview)
        applied = apply_preview(path, preview['id'])
        self.assertEqual(applied['jobs'][0]['state'], 'completed', applied)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 1)

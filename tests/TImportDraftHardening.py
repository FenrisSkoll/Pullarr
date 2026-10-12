"""Library Import review persistence and terminal receipts on disposable media."""

import os
import sqlite3
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from fixtures.library_import import ImportHarness
from flask import Flask
from Tbackend.features import local_organization as old_fixture

from backend.features.import_draft import (apply_draft, get_draft,
                                           resume_draft, save_review_draft,
                                           stage_import)
from backend.features.library_import import import_library, scan_library_import
from backend.features.local_organization import import_preview
from frontend.api import api


class ImportDraftHardeningTests(ImportHarness, TestCase):
    file_database = old_fixture.RegistrationBoundaryTests.file_database

    def setUp(self):
        super().setUp()
        self.response.json.side_effect = list(self.response.json.side_effect) * 2
        self.database = self.file_database()
        self.db.execute("UPDATE config SET value=? WHERE key='volume_folder_naming'",
                        (self.settings.volume_folder_naming,))
        self.db.commit()

    def source(self, name='Example Hero v2 #1 (2021).cbz'):
        path = self.root / 'Incoming' / name
        path.parent.mkdir(exist_ok=True)
        with ZipFile(path, 'w') as archive:
            archive.writestr('page.jpg', b'synthetic review page')
        return path

    def match(self, path):
        return dict(filepath=str(path), provider='comicvine', provider_id='2127',
                    issue_ids=['301'], issue_override=True)

    def client(self):
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        return app.test_client()

    def test_legacy_routes_and_direct_callers_cannot_register_metadata(self):
        source = self.source()
        data = [dict(filepath=str(source), id=2127)]
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.internals.db.DBConnection.default_file', self.database):
            settings.return_value.sv.api_key = 'fixture-key'
            client = self.client()
            for route in ('/api/libraryimport', '/api/libraryimport/preview'):
                response = client.post(route, query_string={'api_key':'fixture-key'}, json=data)
                self.assertEqual(response.status_code, 410, response.json)
                self.assertEqual(response.json['result']['code'], 'legacy_import_requires_review')
            read_only = client.get('/api/libraryimport', query_string={
                'api_key':'fixture-key','auto_match':'false','limit':'100',
                'limit_parent_folder':'false','only_english':'false'})
            self.assertEqual(read_only.status_code, 200, read_only.json)
        with self.assertRaisesRegex(ValueError, 'legacy_import_requires_review'):
            import_library(data)
        with self.assertRaisesRegex(ValueError, 'legacy_import_requires_review'):
            import_preview(self.database, data, False)
        self.assertTrue(source.is_file())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)

    def test_completed_cbr_maintenance_action_only_queues_existing_review(self):
        source = self.root / 'Incoming' / 'Example Hero v2 #1 (2021).cbr'
        source.parent.mkdir()
        copyfile(Path(__file__).parent / 'fixtures' / 'archives' / 'synthetic-25.cbr', source)
        stage = stage_import(self.database, [self.match(source)], False)
        receipt = apply_draft(self.database, stage['id'], stage['revision'])['jobs'][0]
        self.assertEqual(receipt['state'], 'completed')
        self.assertEqual(Path(receipt['target']).suffix, '.cbr')
        from unittest.mock import Mock
        review = Mock(return_value={'id':'review-task','state':'queued'})
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        app.extensions['maintenance'] = SimpleNamespace(archives=SimpleNamespace(submit=review))
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.internals.db.DBConnection.default_file', self.database):
            settings.return_value.sv.api_key = 'fixture-key'
            response = app.test_client().post(
                f"/api/libraryimport/drafts/{stage['id']}/maintenance-review",
                query_string={'api_key':'fixture-key'}, json={'row_id':receipt['row_id']})
        self.assertEqual(response.status_code, 200, response.json)
        review.assert_called_once_with('preview', {'selected':[receipt['file_id']]})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM organization_jobs WHERE intent LIKE '%archive_effect%'").fetchone()[0], 0)
        self.assertTrue(Path(receipt['target']).is_file())

    def test_explicit_maintenance_keeps_import_receipt_linked_to_active_file(self):
        source = self.root / 'Incoming' / 'Example Hero v2 #1 (2021).cbr'
        source.parent.mkdir()
        copyfile(Path(__file__).parent / 'fixtures' / 'archives' / 'synthetic-25.cbr', source)
        stage = stage_import(self.database, [self.match(source)], False)
        imported = apply_draft(self.database, stage['id'], stage['revision'])['jobs'][0]
        self.assertEqual(imported['state'], 'completed')
        from backend.features.organization_archive import (register_archive,
                                                           review)
        from backend.features.organization_execution import \
            OrganizationExecutor
        executor = OrganizationExecutor(self.database, (str(self.root),))
        try:
            _, confirmation = review(executor, imported['file_id'])
            maintenance_job = register_archive(executor, imported['file_id'],
                                               confirmation, 'explicit-maintenance-test')
            self.assertEqual(executor.apply_job(maintenance_job).state.value, 'completed')
        finally:
            executor.close()
        receipt = get_draft(self.database, stage['id'])['rows'][0]['receipt']
        self.assertEqual(receipt['state'], 'completed', receipt)
        self.assertEqual(receipt['maintenance_job_id'], maintenance_job)
        self.assertEqual(Path(receipt['target']).suffix, '.cbz')
        self.assertEqual(receipt['import_target'], imported['target'])
        self.assertFalse(Path(imported['target']).exists())
        self.assertEqual(self.db.execute('SELECT filepath FROM files WHERE id=?',
                                         (imported['file_id'],)).fetchone()[0], receipt['target'])

    def test_review_survives_reload_and_exact_apply_is_idempotent(self):
        source = self.source()
        original = source.read_bytes()
        initial = save_review_draft(self.database, [dict(filepath=str(source))],
                                    scope={'folder_filter':'Incoming','folder_limit':500,'page_size':100})
        self.assertEqual(initial['state'], 'draft')
        with self.assertRaisesRegex(ValueError, 'stale_import_revision'):
            save_review_draft(self.database, [self.match(source)],
                              identifier=initial['id'], revision=0)
        saved = save_review_draft(self.database, [self.match(source)],
                                  identifier=initial['id'], revision=initial['revision'])
        from backend.features.import_draft import _drafts
        _drafts.clear()  # Simulate a process restart; SQLite is the authority.
        reopened = get_draft(self.database, initial['id'])
        self.assertEqual(reopened['scope']['folder_filter'], 'Incoming')
        self.assertEqual(reopened['rows'][0]['selection']['issue_ids'], ['301'])
        self.assertEqual(reopened['revision'], saved['revision'])
        stage = stage_import(self.database, [self.match(source)], False,
                             identifier=initial['id'], revision=saved['revision'])
        self.assertEqual(stage['counts']['ready'], 1)
        self.assertTrue(source.is_file())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)
        result = apply_draft(self.database, stage['id'], stage['revision'])
        receipt = result['jobs'][0]
        self.assertEqual(receipt['state'], 'completed', result)
        self.assertEqual(Path(receipt['target']).read_bytes(), original)
        self.assertEqual(Path(receipt['target']).suffix, '.cbz')
        self.assertEqual(self.db.execute('''SELECT e.provider_id FROM issues_files b
            JOIN issue_external_ids e ON e.issue_id=b.issue_id AND e.provider='comicvine'
            WHERE b.file_id=?''', (receipt['file_id'],)).fetchall(), [('301',)])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM organization_jobs WHERE intent LIKE '%archive_effect%'").fetchone()[0], 0)
        again = apply_draft(self.database, stage['id'], stage['revision'])
        self.assertEqual(again['jobs'][0]['state'], 'completed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 1)

    def test_changed_saved_source_reopens_with_targeted_revalidation_reason(self):
        source = self.source()
        draft = save_review_draft(self.database, [self.match(source)])
        source.write_bytes(source.read_bytes() + b'changed')
        reopened = get_draft(self.database, draft['id'])
        self.assertEqual(reopened['state'], 'needs_review')
        self.assertEqual(reopened['rows'][0]['stale_reason'],
                         'source_changed_revalidation_needed')
        self.assertEqual(reopened['rows'][0]['selection']['issue_ids'], ['301'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_registration_failure_counts_review_and_never_completes(self):
        source = self.source()
        stage = stage_import(self.database, [self.match(source)], False)
        with patch('backend.implementations.volumes.Library.add_metadata',
                   side_effect=ValueError('registration refused')):
            result = apply_draft(self.database, stage['id'], stage['revision'])
        self.assertEqual(result['jobs'], [])
        self.assertEqual(result['counts']['needs_review'], 1)
        self.assertTrue(source.is_file())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)

    def test_truncated_archive_is_blocked_before_publication_registration(self):
        source = self.root / 'Incoming' / 'Example Hero #1 (2021).cbz'
        source.parent.mkdir()
        source.write_bytes(b'PK\x03\x04incomplete disposable archive')
        stage = stage_import(self.database, [self.match(source)], False)
        self.assertEqual(stage['plans'][0]['status'], 'ready', stage)
        result = apply_draft(self.database, stage['id'], stage['revision'])
        self.assertEqual(result['counts']['completed'], 0)
        self.assertEqual(result['counts']['blocked'], 1)
        self.assertTrue(source.is_file())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_explicit_multi_issue_review_links_only_selected_catalogue_issues(self):
        source = self.source('Example Hero #1-2 (2021).cbz')
        choice = dict(self.match(source), issue_ids=['301','302'])
        stage = stage_import(self.database, [choice], False)
        self.assertEqual(stage['plans'][0]['status'], 'ready', stage)
        result = apply_draft(self.database, stage['id'], stage['revision'])
        self.assertEqual(result['jobs'][0]['state'], 'completed', result)
        file_id = result['jobs'][0]['file_id']
        self.assertEqual(self.db.execute('''SELECT e.provider_id FROM issues_files b
            JOIN issue_external_ids e ON e.issue_id=b.issue_id AND e.provider='comicvine'
            WHERE b.file_id=? ORDER BY e.provider_id''', (file_id,)).fetchall(),
            [('301',), ('302',)])

    def test_historical_completion_rechecks_bytes_and_exact_issue_links(self):
        source = self.source()
        stage = stage_import(self.database, [self.match(source)], False)
        receipt = apply_draft(self.database, stage['id'], stage['revision'])['jobs'][0]
        self.assertEqual(receipt['state'], 'completed')
        target = Path(receipt['target'])
        target.write_bytes(target.read_bytes() + b'changed')
        history = get_draft(self.database, stage['id'])['rows'][0]['receipt']
        self.assertEqual(history['state'], 'recovery_required')
        self.assertEqual(history['error'], 'archive_missing_or_changed')
        target.unlink()
        history = get_draft(self.database, stage['id'])['rows'][0]['receipt']
        self.assertNotEqual(history['state'], 'completed')
        self.db.execute('DELETE FROM issues_files WHERE file_id=?', (receipt['file_id'],))
        self.db.commit()
        self.assertNotEqual(get_draft(self.database, stage['id'])['rows'][0]['receipt']['state'],
                            'completed')

    def test_wrong_publication_or_extra_issue_never_remains_completed(self):
        source = self.source()
        stage = stage_import(self.database, [self.match(source)], False)
        receipt = apply_draft(self.database, stage['id'], stage['revision'])['jobs'][0]
        self.assertEqual(receipt['state'], 'completed')
        second = self.db.execute('SELECT id FROM issues WHERE comicvine_id=302').fetchone()[0]
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,?)',
                        (receipt['file_id'], second))
        self.db.commit()
        changed = get_draft(self.database, stage['id'])['rows'][0]['receipt']
        self.assertEqual(changed['error'], 'reviewed_issue_links_missing_or_changed')
        self.db.execute('DELETE FROM issues_files WHERE issue_id=?', (second,))
        self.db.execute('''UPDATE volume_external_ids SET provider_id='99999'
            WHERE volume_id=? AND provider='comicvine' ''', (receipt['volume_id'],))
        self.db.commit()
        changed = get_draft(self.database, stage['id'])['rows'][0]['receipt']
        self.assertNotEqual(changed['state'], 'completed')

    def test_430_folders_and_1001_files_save_in_bounded_pages(self):
        remaining = 1001
        for folder_number in range(430):
            folder = self.root / f'Source {folder_number:03}'
            folder.mkdir()
            count = min(3, remaining - (429 - folder_number))
            for book in range(count):
                with ZipFile(folder / f'Book {folder_number:03}-{book}.cbz', 'w') as archive:
                    archive.writestr('page.jpg', b'disposable page')
            remaining -= count
        self.assertEqual(remaining, 0)
        scan = scan_library_import(self.database, folder_limit=500, page_size=100)
        self.assertEqual(scan['counts']['discovered'], 1001)
        self.assertTrue(scan['enumeration_complete'])
        draft_id = None
        revision = None
        offset = 0
        while True:
            rows = [dict(filepath=item['filepath'], stamp=item['stamp'],
                         display={'file_title':item['file_title'],'folder':item['folder']})
                    for item in scan['items']]
            saved = save_review_draft(self.database, rows, identifier=draft_id,
                                      revision=revision)
            draft_id, revision = saved['id'], saved['revision']
            if scan['next_offset'] is None:
                break
            offset = scan['next_offset']
            scan = scan_library_import(self.database, folder_limit=500,
                                       page_size=100, offset=offset)
        from backend.features.import_draft import _drafts
        _drafts.clear()
        self.assertEqual(get_draft(self.database, draft_id)['total'], 1001)
        self.assertEqual(get_draft(self.database, draft_id, offset=1000)['rows'][0]
                         ['selection']['filepath'].endswith('.cbz'), True)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_http_saved_review_stage_and_apply_require_revision(self):
        source = self.source()
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.internals.db.DBConnection.default_file', self.database):
            settings.return_value.sv.api_key = 'fixture-key'
            client = self.client()
            q = {'api_key':'fixture-key'}
            created = client.post('/api/libraryimport/drafts', query_string=q,
                                  json={'rows':[dict(filepath=str(source))]})
            self.assertEqual(created.status_code, 200, created.json)
            draft_id = created.json['result']['id']
            reopened = client.get('/api/libraryimport/drafts/' + draft_id,
                                  query_string=q)
            self.assertEqual(reopened.json['result']['rows'][0]['selection']['filepath'], str(source))
            changed = client.patch('/api/libraryimport/drafts/' + draft_id,
                                   query_string=q, json={'revision':1,'rows':[self.match(source)]})
            self.assertEqual(changed.status_code, 200, changed.json)
            staged = client.post('/api/libraryimport/stage', query_string={**q,
                'draft_id':draft_id,'revision':2,'rename_files':'false'}, json=[self.match(source)])
            self.assertEqual(staged.status_code, 200, staged.json)
            blocked = client.post('/api/local-organization/' + draft_id + '/apply',
                                  query_string=q, json={'revision':2})
            self.assertEqual(blocked.status_code, 409, blocked.json)
            applied = client.post('/api/local-organization/' + draft_id + '/apply',
                                  query_string=q, json={'revision':3})
            self.assertEqual(applied.status_code, 200, applied.json)
            self.assertEqual(applied.json['result']['jobs'][0]['state'], 'completed')
            self.assertTrue(Path(applied.json['result']['jobs'][0]['target']).is_file())

    def test_pending_journal_resumes_after_simulated_process_death(self):
        source = self.source()
        payload = source.read_bytes()
        stage = stage_import(self.database, [self.match(source)], False)
        with patch('backend.features.organization_execution.OrganizationExecutor.apply_job',
                   side_effect=RuntimeError('simulated process stopped before first effect')):
            with self.assertRaisesRegex(RuntimeError, 'simulated process stopped'):
                apply_draft(self.database, stage['id'], stage['revision'])
        self.assertTrue(source.is_file())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 0)
        from backend.features.import_draft import _applying, _drafts
        from backend.features.local_organization import _sessions
        _applying.clear(); _drafts.clear(); _sessions.clear()
        reopened = get_draft(self.database, stage['id'])
        self.assertEqual(reopened['state'], 'recovery_required')
        self.assertEqual(reopened['rows'][0]['receipt']['state'], 'pending')
        self.assertTrue(reopened['rows'][0]['receipt']['job_id'])
        result = resume_draft(self.database, stage['id'], reopened['revision'])
        self.assertEqual(result['jobs'][0]['state'], 'completed', result)
        self.assertEqual(Path(result['jobs'][0]['target']).read_bytes(), payload)
        self.assertFalse(source.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)

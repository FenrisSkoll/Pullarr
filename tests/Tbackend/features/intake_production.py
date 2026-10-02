"""Production SAB observer→intake worker→organizer with local HTTP services."""

from dataclasses import fields
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from Tbackend.features.organization_plan import NAMING
from Tbackend.features.sab_downloads import NZB, SABHarness, fake_sab

from backend.base.definitions import SpecialVersion
from backend.base.release_search import ReleaseSearchRequest, SearchLimits
from backend.features.intake_runtime import IntakeRuntime
from backend.implementations.newznab import (BoundedHTTP, NewznabSource,
                                             SearchBudget)
from backend.implementations.nzb_resolution import SelectedSourceSession
from backend.implementations.release_scoring import evaluate_release
from backend.internals.acquisition_intakes import IntakeStore
from tests.Tbackend.features.release_search import (CAPS, config,
                                                    fake_http, item, rss)


class SABIntakeProduction(SABHarness, TestCase):
    def seed(self):
        self.root = Path(self.temp.name)
        self.incoming, self.library = self.root / 'downloads', self.root / 'library'
        self.incoming.mkdir()
        self.library.mkdir()
        db = self.store.db
        db.execute("UPDATE config SET value=57 WHERE key='database_version'")
        db.executemany('INSERT INTO config VALUES(?,?)', ((f.name, getattr(NAMING, f.name)) for f in fields(NAMING)))
        db.execute('INSERT INTO root_folders VALUES(1,?)', (str(self.library),))
        db.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,publisher,root_folder,folder,special_version) VALUES(1,101,'Batman',2016,2,'DC',1,?,?)",
                   (str(self.library / 'Batman'), SpecialVersion.NORMAL.value))
        db.executemany("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(?,1,?,?,?)",
                       ((5, 205, '5', 5), (6, 206, '6', 6)))

    def flow(self, wrong=False, interrupted=False):
        self.seed()
        actual = 6 if wrong else 5
        source_path = self.incoming / f'Batman {actual:03} (2016).cbz'
        with ZipFile(source_path, 'w') as archive:
            archive.writestr('page.jpg', b'disposable image')
            archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>Batman</Series><Number>{actual}</Number><Year>2016</Year></ComicInfo>')
        def reply(path, query):
            return (200, NZB, {}) if path == '/nzb' else (
                200, CAPS if query.get('t') == ['caps'] else rss(item(url=source_url + '/nzb')), {})
        with fake_http(reply) as (source_url, calls), fake_sab() as (url, remote):
            source = NewznabSource(config(url=source_url + '/api'), BoundedHTTP(SearchBudget(SearchLimits())))
            self.candidate = source.search(ReleaseSearchRequest('Batman')).candidates[0]
            self.evaluation = evaluate_release(self.target, self.candidate, self.policy)
            self.session = SelectedSourceSession(source)
            self.addCleanup(self.session.close)
            client = self.client(url)
            self.store.db.execute('INSERT INTO acquisition_path_mappings VALUES(?,?,?,?,?,?,?,?)',
                ('map', client.config.key, client.config.instance, '/complete', 'posix', str(self.incoming), 1, None))
            result = self.submit(client)
            self.poll(client)
            remote['queue'].clear()
            remote['history'][result['nzo_id']] = dict(nzo_id=result['nzo_id'], status='Completed',
                storage='/complete/' + source_path.name, completed=1790000000)
            if interrupted:
                # Model a previously committed completion with missing handoff;
                # restart repair uses the same exact durable receipt.
                with patch('backend.features.acquisition_completion.ensure_sab_completion'):
                    self.poll(client)
                self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM acquisition_intakes').fetchone()[0], 0)
            else:
                self.poll(client)
            self.assertTrue(source_path.exists())
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
            runtime = IntakeRuntime(self.path, clock=lambda: 100.)
            runtime.tick()
            runtime.clock = lambda: 111.
            runtime.tick()
            runtime.tick()
            intake = self.store.db.execute('SELECT id FROM acquisition_intakes').fetchall()
            self.assertEqual(len(intake), 1)
            store = IntakeStore(self.path)
            try:
                receipt = store.preview(intake[0][0])
            finally:
                store.close()
            self.assertEqual(receipt['state'], 'review' if wrong else 'completed', receipt)
            self.assertEqual(source_path.exists(), wrong)
            self.assertEqual([r[0] for r in self.store.db.execute('SELECT issue_id FROM issues_files')], [] if wrong else [5])
            self.assertEqual(len(remote['uploads']), 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.store.get(result['id'])['state'], 'completed')
            if not wrong:
                self.assertIsNotNone(receipt['artifacts'][0]['final_file_id'])

    def test_selected_nzb_through_actual_completion_hook_and_worker(self):
        self.flow()

    def test_selected_five_actual_six_stays_review_in_production(self):
        self.flow(wrong=True)

    def test_restart_repairs_completed_receipt_exactly_once(self):
        self.flow(interrupted=True)

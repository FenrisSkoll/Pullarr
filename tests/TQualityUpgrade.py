"""Real organizer journal replacement and rejected-upgrade byte preservation."""

from io import BytesIO
from types import SimpleNamespace
from unittest import TestCase
from zipfile import ZipFile

from PIL import Image
from Tbackend.features import organization_execution as execution_fixture

from backend.base.import_candidate import DiscoveryScope
from backend.base.organization_job import JobState
from backend.base.organization_plan import PlanningPolicy
from backend.base.quality import QualityError
from backend.features.local_artifact_planning import preview_local_artifacts
from backend.features.organization_upgrade import register_upgrade
from backend.implementations.file_quality import analyze
from backend.internals.quality import QualityStore
from tests.TQuality import policy


def comic(path, edge):
    data = BytesIO()
    Image.new('RGB', (edge,edge*2), 'white').save(data, format='PNG')
    with ZipFile(path, 'w') as archive:
        archive.writestr('1.png', data.getvalue())


class UpgradeTests(TestCase):
    def setUp(self):
        self.h = execution_fixture.ExecutionTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.db = self.h.db
        self.db.execute("UPDATE config SET value=70 WHERE key='database_version'")
        self.db.execute('UPDATE volumes SET monitored=1')
        self.db.execute('UPDATE issues SET monitored=1')
        self.old = self.h.folder/'Batman 001.cbz'
        comic(self.old,600)
        comic(self.h.source,1200)
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(self.old),self.old.stat().st_size))
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
        self.store = QualityStore(self.db.cursor())
        self.store.save('Fixture quality',policy(),identifier=1,revision=1)
        self.store.assessment(1,analyze(str(self.old)))
        candidate = SimpleNamespace(raw_title='Batman 001 (2020) (HD-Digital)',
            source=SimpleNamespace(key='fixture',kind=SimpleNamespace(value='newznab')),candidate_id='fixture-result')
        self.provenance = self.store.selected(1,candidate,reason='upgrade',decision={})

    def plan(self):
        import os
        return preview_local_artifacts(self.db,(str(self.h.source),),DiscoveryScope('test',str(self.h.root)),
            PlanningPolicy(windows=os.name=='nt',case_sensitive=os.name!='nt'),volume_id=1,issue_ids=(1,)).plans[0]

    def register(self):
        return register_upgrade(self.h.executor,self.plan(),self.provenance,'fixture-upgrade')

    def test_success_stable_file_lineage_cutoff(self):
        expected = self.h.source.read_bytes()
        job = self.register()
        result = self.h.executor.apply_job(job)
        self.assertEqual(result.state,JobState.COMPLETED,result.error)
        self.assertEqual(self.old.read_bytes(),expected)
        self.assertFalse(self.h.source.exists())
        self.assertFalse(list(self.h.folder.glob('.kapowarr-upgrade-*')))
        self.assertEqual(self.db.execute('SELECT * FROM issues_files').fetchall(),[(1,1,0)])
        self.assertTrue(self.store.issue_states([1])[0]['cutoff_satisfied'])
        self.assertFalse(self.store.issue_states([1])[0]['upgrade_eligible'])
        self.assertEqual(self.store.detail(self.provenance)['state'],'imported')
        self.assertFalse(self.h.executor.preview_undo(job).eligible)
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())

    def test_false_hd_rejected_before_old_bytes_touched(self):
        original = self.old.read_bytes()
        comic(self.h.source,900)
        with self.assertRaisesRegex(QualityError,'dimension_floor_failed'):
            self.register()
        self.assertEqual(self.old.read_bytes(),original)
        self.assertEqual(self.db.execute('SELECT count(*) FROM organization_jobs').fetchone()[0],0)

    def test_corrupt_rejected_before_old_bytes_touched(self):
        original = self.old.read_bytes()
        with ZipFile(self.h.source,'w') as archive:
            archive.writestr('1.png',b'corrupt')
        with self.assertRaises(QualityError):
            self.register()
        self.assertEqual(self.old.read_bytes(),original)

    def test_recovery_after_atomic_swap(self):
        job = self.register()
        def die(stage,identifier,ordinal):
            if stage == 'after_effect' and ordinal == 1:
                raise execution_fixture.Interrupted()
        self.h.executor.hook = die
        with self.assertRaises(execution_fixture.Interrupted):
            self.h.executor.apply_job(job)
        self.assertTrue(list(self.h.folder.glob('.kapowarr-upgrade-*')))
        self.h.executor.hook = lambda *_: None
        preview = self.h.executor.preview_recovery(job)
        self.assertTrue(preview['eligible'],preview['reasons'])
        result = self.h.executor.apply_job(job,approved_recovery=preview['digest'])
        self.assertEqual(result.state,JobState.COMPLETED,result.error)

    def test_every_effect_interruption_recovers_without_duplicate_acquisition(self):
        for ordinal in range(4):
            with self.subTest(ordinal=ordinal):
                fixture=UpgradeTests();fixture.setUp()
                try:
                    job=fixture.register()
                    def die(stage,identifier,index):
                        if stage=='after_effect' and index==ordinal:
                            raise execution_fixture.Interrupted()
                    fixture.h.executor.hook=die
                    with self.assertRaises(execution_fixture.Interrupted):fixture.h.executor.apply_job(job)
                    fixture.h.executor.hook=lambda *_:None
                    preview=fixture.h.executor.preview_recovery(job)
                    self.assertTrue(preview['eligible'],preview)
                    result=fixture.h.executor.apply_job(job,approved_recovery=preview['digest'])
                    self.assertEqual(result.state,JobState.COMPLETED,result.error)
                    self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM acquisition_provenance').fetchone()[0],1)
                finally:fixture.doCleanups()

    def test_owned_source_and_shared_target_blocked(self):
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,?)',(str(self.h.source),self.h.source.stat().st_size))
        with self.assertRaisesRegex(QualityError,'source_already_owned'):self.register()
        self.db.execute('DELETE FROM files WHERE id=2')
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,902,'2',2)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,2)')
        self.assertFalse(self.store.issue_states([1])[0]['upgrade_eligible'])
        with self.assertRaises(QualityError):self.register()

    def test_real_search_sab_completion_intake_upgrade(self):
        from unittest.mock import patch

        from fixtures.quality import configure, sources

        from backend.features.direct_downloads import load_target
        from backend.features.intake_runtime import IntakeRuntime
        from backend.features.sab_downloads import poll_downloads
        from backend.features.wanted_automation import WantedAutomation
        from backend.features.wanted_search import UnifiedReleaseSearch
        from backend.internals.download_jobs import DownloadStore
        from backend.internals.release_sources import load_sources
        with sources() as fixture, patch('backend.internals.release_sources.get_db',side_effect=self.db.cursor), \
                patch('backend.features.direct_downloads.get_db',side_effect=self.db.cursor), \
                patch('backend.internals.identification.get_db',side_effect=self.db.cursor):
            client=configure(self.db,fixture,self.h.incoming)
            searches=UnifiedReleaseSearch(target_loader=load_target,nzb_loader=load_sources,ddl_loader=lambda:{})
            self.addCleanup(searches.close_all)
            service=WantedAutomation(self.h.dbpath,searches=searches)
            self.addCleanup(service.close)
            result=service.run_target(1,1,allow_grab=True)
            self.assertEqual(result['state'],'no_acceptable_getcomics_release')
            preview=service.search_manual(1,1)
            row=preview['results'][0]
            session=searches.lookup(preview['search_id'],row['selection_id'])
            result=service.grab(session,session.selections[row['selection_id']],automatic=False)
            self.assertEqual(result['state'],'tracking',(result,self.db.execute('SELECT error FROM wanted_searches').fetchall()))
            record=self.db.execute('SELECT id,nzo_id FROM acquisition_downloads').fetchone()
            fixture['remote']['queue'].clear()
            fixture['remote']['history'][record[1]]=dict(nzo_id=record[1],status='Completed',storage='/complete/'+self.h.source.name,completed=1790000000)
            downloads=DownloadStore(self.h.dbpath)
            try:
                poll_downloads(downloads,[client])
            finally:
                downloads.close()
            runtime=IntakeRuntime(self.h.dbpath,clock=lambda:100.)
            runtime.tick();runtime.clock=lambda:111.;runtime.tick();runtime.tick()
            receipt=self.db.execute('SELECT state FROM acquisition_intakes').fetchone()[0]
            self.assertEqual(receipt,'completed')
            self.assertTrue(self.store.issue_states([1])[0]['cutoff_satisfied'])
            self.assertEqual(len(fixture['remote']['uploads']),1)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM acquisition_provenance WHERE state='imported' AND reason='upgrade'").fetchone()[0],1)
            import json
            receipt=json.loads(self.db.execute("SELECT decision FROM acquisition_provenance WHERE state='imported' AND reason='upgrade'").fetchone()[0])
            self.assertEqual(sum(c['points'] for c in receipt['components']),receipt['score'])
            self.assertTrue(receipt['scoring_fingerprint'])

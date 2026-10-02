"""Real 9A intake/source relationships and maintenance-aware cleanup."""
import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase

import TExpandedPipeline as pipeline
import TTorrentIntake as intake_fixture
from TArchiveMaintenance import comic

from backend.base.organization_job import JobState
from backend.features.organization_archive import register_archive, review
from backend.features.torrent_lifecycle import (cleanup, cleanup_preview,
                                                observe_torrents)
from backend.implementations.managed_clients import client_for
from backend.internals.acquisition_intakes import ensure_intake
from backend.internals.download_jobs import DownloadStore


class ArchiveSeedTests(TestCase):
    def test_real_seed_relationship_cbr_copy_on_write(self):
        fixture=intake_fixture.TorrentIntakeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        source=fixture.fixture.source.with_suffix('.cbr');comic(source,True)
        original=source.read_bytes()
        completion=replace(fixture.completion,download_id='cbr-torrent',reported_paths=('/remote/'+source.name,))
        identifier=ensure_intake(fixture.fixture.db,completion,rename=True,auto_apply=True)
        for _ in range(5):
            result=fixture.coordinator.process(identifier);fixture.clock+=11
        self.assertEqual(result['state'],'completed',result)
        fid,path=fixture.fixture.db.execute('SELECT id,filepath FROM active_files').fetchone()
        self.assertTrue(os.path.samefile(source,path))
        authority,confirmation=review(fixture.fixture.executor,fid)
        self.assertTrue(authority['sharing']['seeds'])
        job=register_archive(fixture.fixture.executor,fid,confirmation,'seeded-cbr-maintenance')
        self.assertEqual(fixture.fixture.executor.apply_job(job).state,JobState.COMPLETED)
        target=Path(path).with_suffix('.cbz')
        self.assertEqual(source.read_bytes(),original)
        self.assertFalse(os.path.samefile(source,target))
        self.assertFalse(Path(path).exists())
        self.assertEqual(fixture.fixture.db.execute('SELECT state FROM acquisition_intakes WHERE id=?',(identifier,)).fetchone()[0],'completed')
        before=target.read_bytes();source.unlink();self.assertEqual(target.read_bytes(),before)

    def test_real_qbittorrent_retention_survives_repack(self):
        harness=pipeline.ExpandedPipelineTests();self.addCleanup(harness.doCleanups)
        def after(fixture, config, remote):
            source_before=fixture.h.source.read_bytes()
            provenance=fixture.db.execute('SELECT * FROM acquisition_provenance').fetchall()
            policy=fixture.db.execute('SELECT policy,requirements FROM acquisition_torrents').fetchall()
            _,confirmation=review(fixture.h.executor,1)
            job=register_archive(fixture.h.executor,1,confirmation,'seeded-repack')
            self.assertEqual(fixture.h.executor.apply_job(job).state,JobState.COMPLETED)
            self.assertEqual(fixture.h.source.read_bytes(),source_before)
            self.assertEqual(fixture.db.execute('SELECT * FROM acquisition_provenance').fetchall(),provenance)
            self.assertEqual(fixture.db.execute('SELECT policy,requirements FROM acquisition_torrents').fetchall(),policy)
            self.assertTrue(fixture.store.issue_states([1])[0]['cutoff_satisfied'])
            store=DownloadStore(fixture.h.dbpath)
            try:
                identifier=fixture.db.execute('SELECT download_id FROM acquisition_torrents').fetchone()[0]
                remote['ratio']=1.
                observe_torrents(store,[config],client_for,clock=lambda:100.)
                preview=cleanup_preview(store,identifier,delete_data=True,manual=True,clock=lambda:100.)
                self.assertTrue(preview['eligible'],preview)
                cleanup(store,identifier,config,client_for(config),preview['confirmation'],delete_data=True,manual=True,clock=lambda:100.)
                self.assertTrue(remote['removed'])
                expected=fixture.old.read_bytes();fixture.h.source.unlink()
                self.assertEqual(fixture.old.read_bytes(),expected)
            finally:store.close()
        harness.workflow(after=after)

"""Torrent completion reuses canonical matching and journaled ownership."""
import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from Tbackend.features import organization_execution as execution_fixture

from backend.base.acquisition_intake import (AcquisitionCompletion,
                                             AcquisitionKind)
from backend.features.acquisition_intake import IntakeCoordinator
from backend.internals.acquisition_intakes import ensure_intake


class TorrentIntakeTests(TestCase):
    def setUp(self):
        self.fixture = execution_fixture.ExecutionTests('test_pending_survives_reopen_without_mutation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.db.execute("UPDATE config SET value=72 WHERE key='database_version'")
        self.clock = 100.
        self.completion = AcquisitionCompletion(AcquisitionKind.QBITTORRENT, 'torrent-job', 'candidate',
            1, (1,), ('/remote/' + self.fixture.source.name,), '2026-10-01T12:00:00+00:00',
            client_id='qbit', client_instance='instance', remote_job_id='a' * 40, mechanism='torrent')
        self.identifier = ensure_intake(self.fixture.db, self.completion, rename=True, auto_apply=True)
        self.fixture.db.execute('''INSERT INTO acquisition_path_mappings
            (id,client_id,client_instance,remote_prefix,remote_style,local_root) VALUES('map','qbit','instance','/remote','posix',?)''',
            (str(self.fixture.incoming),))
        self.coordinator = IntakeCoordinator(self.fixture.dbpath, clock=lambda: self.clock)
        self.addCleanup(self.coordinator.close)

    def test_single_file_preserved_and_owned(self):
        original = self.fixture.source.read_bytes()
        for _ in range(5):
            result = self.coordinator.process(self.identifier)
            self.clock += 11
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(self.fixture.source.read_bytes(), original)
        target = self.fixture.db.execute('SELECT filepath FROM active_files').fetchone()[0]
        self.assertEqual(Path(target).read_bytes(), original)
        self.assertTrue(os.path.samefile(self.fixture.source, target))
        relationship = self.fixture.db.execute('SELECT import_method FROM acquisition_seed_artifacts').fetchone()
        self.assertEqual(relationship[0], 'hardlink')
        count = self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0]
        self.coordinator.process(self.identifier)
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], count)

    def test_copy_fallback_intake(self):
        import errno
        with patch('backend.features.organization_seed_copy.os.link', side_effect=OSError(errno.EXDEV,'fixture')):
            for _ in range(5):
                result = self.coordinator.process(self.identifier)
                self.clock += 11
        self.assertEqual(result['state'],'completed',result)
        target = self.fixture.db.execute('SELECT filepath FROM active_files').fetchone()[0]
        self.assertFalse(os.path.samefile(self.fixture.source,target))
        self.assertEqual(Path(target).read_bytes(),self.fixture.source.read_bytes())

    def test_pack_partial_import_keeps_unresolved_payload(self):
        other = self.fixture.incoming / 'Unrelated 099 (1990).cbz'
        other.write_bytes(self.fixture.source.read_bytes())
        completion = replace(self.completion,download_id='pack-job',reported_paths=self.completion.reported_paths + ('/remote/' + other.name,))
        identifier = ensure_intake(self.fixture.db,completion,rename=True,auto_apply=True)
        for _ in range(5):
            result = self.coordinator.process(identifier)
            self.clock += 11
        self.assertEqual(result['state'],'partial',result)
        self.assertTrue(other.exists())
        self.assertTrue(self.fixture.source.exists())
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM active_files').fetchone()[0],1)
        self.assertTrue(any(a['state']=='review' for a in result['artifacts']))

    def test_wrong_identity_never_imports(self):
        other = self.fixture.incoming / 'Unrelated 099 (1990).cbz'
        other.write_bytes(self.fixture.source.read_bytes())
        completion = replace(self.completion,download_id='wrong-job',reported_paths=('/remote/' + other.name,))
        identifier = ensure_intake(self.fixture.db,completion,rename=True,auto_apply=True)
        for _ in range(5):
            result = self.coordinator.process(identifier)
            self.clock += 11
        self.assertEqual(result['state'],'review',result)
        self.assertTrue(other.exists())
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM active_files').fetchone()[0],0)

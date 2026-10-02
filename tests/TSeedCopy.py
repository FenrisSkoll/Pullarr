"""Disposable source-preserving organizer effects on both supported hosts."""
import errno
import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from backend.base.organization_job import JobState
from backend.features.organization_execution import OrganizationExecutor
from backend.features.organization_seed_copy import register_seed_copy
from backend.internals.db import DB_SCHEMA


class SeedCopyTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / 'source.cbz', self.root / 'staged.cbz'
        self.source.write_bytes(b'synthetic-disposable-payload')
        self.database = self.root / 'fixture.db'
        with sqlite3.connect(self.database) as connection:
            connection.executescript(DB_SCHEMA)
            connection.execute("INSERT INTO config VALUES('database_version',71)")
        connection.close()
        self.executor = OrganizationExecutor(str(self.database), (str(self.root),))
        self.addCleanup(self.executor.close)

    def register(self):
        return register_seed_copy(self.executor, str(self.source), str(self.target), 'seed-fixture')

    def test_hardlink_and_source_cleanup_survival(self):
        job = self.executor.apply_job(self.register())
        self.assertEqual(job.state, JobState.COMPLETED, job.error)
        self.assertTrue(os.path.samefile(self.source, self.target))
        self.assertEqual(json.loads(job.steps[0].evidence)['import_method'], 'hardlink')
        self.source.unlink()  # Only the exact disposable test payload.
        self.assertEqual(self.target.read_bytes(), b'synthetic-disposable-payload')

    def test_copy_fallback(self):
        with patch('backend.features.organization_seed_copy.os.link', side_effect=OSError(errno.EXDEV, 'fixture')):
            job = self.executor.apply_job(self.register())
        self.assertEqual(job.state, JobState.COMPLETED, job.error)
        self.assertFalse(os.path.samefile(self.source, self.target))
        self.assertEqual(self.source.read_bytes(), self.target.read_bytes())
        self.assertEqual(json.loads(job.steps[0].evidence)['import_method'], 'copy')

    def test_restart_after_effect(self):
        class Interrupted(BaseException):
            pass
        def checkpoint(name, job, ordinal):
            if name == 'after_effect':
                raise Interrupted
        job = self.register()
        self.executor.hook = checkpoint
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        self.executor.hook = lambda *args: None
        self.assertTrue(self.executor.preview_recovery(job)['eligible'])
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertTrue(self.source.exists())

    def test_partial_copy_is_not_overwritten(self):
        job = self.register()
        self.target.write_bytes(b'occupied')
        result = self.executor.apply_job(job)
        self.assertNotEqual(result.state, JobState.COMPLETED)
        self.assertEqual(self.target.read_bytes(), b'occupied')
        self.assertEqual(self.source.read_bytes(), b'synthetic-disposable-payload')

    def test_later_library_replacement_preserves_seed_bytes(self):
        self.assertEqual(self.executor.apply_job(self.register()).state, JobState.COMPLETED)
        replacement = self.root / 'replacement.cbz'
        replacement.write_bytes(b'new-library-bytes')
        os.replace(replacement, self.target)
        self.assertEqual(self.source.read_bytes(), b'synthetic-disposable-payload')
        self.assertEqual(self.target.read_bytes(), b'new-library-bytes')

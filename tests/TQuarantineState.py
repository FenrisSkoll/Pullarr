"""Inactive identity/schema prerequisite; fixtures do not authorize quarantine.

Rows are seeded directly to test the persistence contract before any movement
service exists. These are NOT journal/recovery/runtime quarantine acceptance.
"""

import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TLibraryHealth as health_fixture

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.custom_exceptions import FileNotFound
from backend.base.folder_monitor import MonitorLimits
from backend.base.library_health import HealthScope
from backend.base.organization_job import OrganizationError
from backend.features.folder_monitor import walk
from backend.features.organization_execution import OrganizationExecutor
from backend.features.wanted_status import wanted_rows
from backend.implementations.file_matching import scan_files
from backend.implementations.quarantine_location import reject_quarantine_root
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.db import SCHEMA_65, SCHEMA_66
from backend.internals.db_migration import _migrate_file_quarantine
from backend.internals.db_models import FilesDB, GeneralFilesDB
from backend.internals.folder_ownership import load_ownership
from backend.internals.import_identity import load_existing_import_identities
from backend.internals.library_health import read_snapshot
from backend.internals.organization_jobs import JobStore
from backend.internals.organization_plan import load_planning_records
from backend.internals.provider_authority import capture
from backend.internals.quarantine_schema import STATEMENTS


class CursorAdapter:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.cursor()

    def execute(self, sql, parameters=()):
        self.cursor.execute(sql, parameters)
        return self

    def fetchalldict(self):
        names = [c[0] for c in self.cursor.description]
        return [dict(zip(names, row)) for row in self.cursor.fetchall()]


class QuarantineStateTests(TestCase):
    def setUp(self):
        self.fixture = health_fixture.LibraryHealthTests('test_inventory_never_opens_archive_or_hashes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.path = self.fixture.comic()
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',66)")
        self.db.execute('UPDATE volumes SET monitored=1 WHERE id=1')
        self.db.execute("INSERT INTO volume_files(file_id,volume_id,file_type,forced) VALUES(1,1,'metadata',1)")
        self.db.commit()
        store = JobStore(str(self.fixture.database))
        try:
            self.job = store.create(dict(effects=[], volume_id=1, source=str(self.path), target='fixture'),
                                    'inactive-state-fixture', ())
        finally:
            store.close()
        self.target = str(self.fixture.base / 'retained' / self.path.name)

    def mark(self, *, remove_links=True):
        # No filesystem effect: this seeds the narrow relation for DB tests.
        self.db.execute('UPDATE files SET filepath=? WHERE id=1', (self.target,))
        self.db.execute('''INSERT INTO quarantined_files VALUES(1,?,1,?,?,?)''',
                        (self.job, str(self.path), self.target, '2026-01-01T00:00:00+00:00'))
        if remove_links:
            self.db.execute('DELETE FROM issues_files WHERE file_id=1')
            self.db.execute('DELETE FROM volume_files WHERE file_id=1')
        self.db.commit()

    def c2(self):
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(2,1,102,'2',2,1)")
        ref = PublicationRef('comicvine', '102')
        preview = claim_preview(self.db.cursor(), 1, ref, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(self.db.cursor(), 1, ref, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(self.db.cursor(), 1, 1, [claim])
        apply_coverage(self.db.cursor(), 1, 1, [claim], preview['preview_token'])
        self.db.commit()

    def test_live_direct_and_collected_ownership_excluded_even_before_links_removed(self):
        self.c2()
        history = self.db.execute('SELECT * FROM file_content_coverage').fetchall()
        claims = self.db.execute('SELECT * FROM bibliographic_content_claims').fetchall()
        self.assertEqual(len(self.db.execute('SELECT * FROM canonical_issue_files').fetchall()), 2)
        self.mark(remove_links=False)
        self.assertEqual(self.db.execute('SELECT * FROM canonical_issue_files').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT * FROM valid_file_content_coverage').fetchall(), [])
        self.assertEqual(history, self.db.execute('SELECT * FROM file_content_coverage').fetchall())
        self.assertEqual(claims, self.db.execute('SELECT * FROM bibliographic_content_claims').fetchall())
        # Monitoring and historical references survive; Wanted remains derived.
        self.assertEqual(self.db.execute('SELECT monitored FROM issues').fetchall(), [(1,), (1,)])
        self.db.row_factory = sqlite3.Row
        try:
            result = wanted_rows(SimpleNamespace(db=self.db, clock=lambda: 0))
            self.assertEqual({r['id'] for r in result if r['wanted']}, {1, 2})
        finally:
            self.db.row_factory = None

    def test_orphan_prune_retains_identity_and_new_file_cannot_reuse_it(self):
        self.c2()
        history = self.db.execute('SELECT * FROM file_content_coverage').fetchall()
        self.mark()
        self.db.execute("INSERT INTO files(filepath,size) VALUES('ordinary-orphan',1)")
        with patch('backend.internals.db_models.get_db', side_effect=self.db.cursor):
            FilesDB.delete_unmatched_files()
        self.assertEqual(self.db.execute('SELECT id FROM files').fetchall(), [(1,)])
        new_id = self.db.execute("INSERT INTO files(filepath,size) VALUES('new-active',1)").lastrowid
        self.assertNotEqual(new_id, 1)
        self.assertEqual(history, self.db.execute('SELECT * FROM file_content_coverage').fetchall())
        self.db.commit()
        with closing(sqlite3.connect(self.fixture.database)) as reopened:
            self.assertEqual(reopened.execute('SELECT file_id FROM quarantined_files').fetchall(), [(1,)])
            self.assertEqual(reopened.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            self.assertEqual(reopened.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_ordinary_delete_update_and_reassociation_fail_closed(self):
        self.mark()
        for sql in ('DELETE FROM files WHERE id=1', "UPDATE files SET filepath='other' WHERE id=1",
                    'INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)',
                    "INSERT INTO volume_files(file_id,volume_id,file_type) VALUES(1,1,'metadata')"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
        self.assertEqual(self.db.execute('SELECT id,filepath FROM files').fetchall(), [(1, self.target)])

    def test_active_lists_hide_retained_row(self):
        self.mark(remove_links=False)
        with patch('backend.internals.db_models.get_db', side_effect=lambda: CursorAdapter(self.db)):
            self.assertEqual(FilesDB.fetch(), [])
            self.assertEqual(FilesDB.fetch(volume_id=1), [])
            self.assertEqual(FilesDB.fetch(issue_id=1), [])
            self.assertEqual(GeneralFilesDB.fetch(1), [])
            with self.assertRaises(FileNotFound):
                FilesDB.fetch(file_id=1)

    def test_planning_health_and_folder_inventory_exclude_inactive_identity(self):
        self.mark()
        providers = ('comicvine', 'metron', 'gcd')
        self.assertEqual(load_planning_records(providers, self.db.cursor())[3], ())
        self.assertEqual(load_existing_import_identities((self.target,), providers, self.db.cursor()), {})
        self.assertEqual(load_ownership(self.db.cursor(), (1,)).all_files, ())
        snapshot = read_snapshot(str(self.fixture.database), HealthScope('volumes', (1,)), 20000)
        self.assertEqual(snapshot['files'], [])
        self.assertEqual(snapshot['direct'], [])
        self.assertEqual(snapshot['general'], [])

    def test_ordinary_executor_cannot_consume_inactive_identity(self):
        self.mark()
        executor = OrganizationExecutor(str(self.fixture.database), (str(self.fixture.root),))
        self.addCleanup(executor.close)
        with self.assertRaises(OrganizationError) as error:
            executor._file(1, self.target, str(self.path))
        self.assertEqual(error.exception.detail, 'quarantined_file_inactive')

    def test_marker_identity_constraints(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'path_mismatch'):
            self.db.execute('INSERT INTO quarantined_files VALUES(1,?,1,?,?,?)',
                            (self.job, str(self.path), self.target, 'now'))
        self.db.rollback()
        self.mark()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE quarantined_files SET version=2')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM organization_jobs WHERE id=?', (self.job,))

    def test_db_reconciliation_rollback_preserves_links_history_and_marker_absence(self):
        before = tuple(self.db.iterdump())
        for stage in range(4):
            with self.subTest(stage=stage):
                try:
                    self.db.execute('BEGIN IMMEDIATE')
                    self.db.execute('UPDATE files SET filepath=? WHERE id=1', (self.target,))
                    self.db.execute('INSERT INTO quarantined_files VALUES(1,?,1,?,?,?)',
                                    (self.job, str(self.path), self.target, 'now'))
                    if stage >= 1:
                        self.db.execute('DELETE FROM issues_files WHERE file_id=1')
                    if stage >= 2:
                        self.db.execute('DELETE FROM volume_files WHERE file_id=1')
                    raise RuntimeError('injected')
                except RuntimeError:
                    self.db.rollback()
                self.assertEqual(before, tuple(self.db.iterdump()))

    def test_seeded_inactive_artifact_survives_actual_scan_and_is_not_discovered(self):
        # Seed physical/database after-state. This is a scan/filter regression,
        # explicitly not proof of a journaled quarantine operation.
        target = Path(self.target)
        target.parent.mkdir()
        self.path.rename(target)
        self.mark()
        token = capture(self.db.cursor(), (1,))[1]
        before = tuple(self.db.iterdump())
        with ExitStack() as stack:
            for module in ('backend.implementations.file_matching', 'backend.internals.db_models'):
                stack.enter_context(patch(module + '.get_db', side_effect=self.db.cursor))
            settings = stack.enter_context(patch('backend.implementations.file_matching.Settings'))
            settings.return_value.get_settings.return_value = SimpleNamespace(
                create_empty_volume_folders=True, delete_empty_folders=False, unmonitor_deleted_issues=True)
            volume = stack.enter_context(patch('backend.implementations.volumes.Volume')).return_value
            volume.get_data.return_value = SimpleNamespace(folder=str(self.fixture.volume), root_folder=1)
            volume.get_issues.return_value = [SimpleNamespace(id=1, calculated_issue_number=1, date='2020-01-01')]
            volume.get_all_files.side_effect = lambda: [dict(id=r[0], filepath=r[1]) for r in
                self.db.execute('SELECT id,filepath FROM active_files')]
            volume.get_general_files.return_value = []
            self.assertEqual(scan_files(1), 'completed')
            self.assertEqual(scan_files(1, expected_authority=token), 'completed')
        self.assertEqual(before, tuple(self.db.iterdump()))
        report = self.fixture.scan()
        self.assertFalse(any(f.code in ('missing_file', 'untracked_file') for f in report.findings))
        self.assertNotIn(self.target, [s.path for s in walk(str(self.fixture.root), MonitorLimits()) if s is not None])
        self.assertTrue(target.is_file())
        self.assertFalse(self.path.exists())

    def test_retained_storage_ancestor_cannot_become_library_root(self):
        self.mark()
        with self.assertRaisesRegex(ValueError, 'not_library_root'):
            reject_quarantine_root(self.db.cursor(), str(self.fixture.base))

    def test_db_only_exact_link_reactivation_restores_live_coverage_without_history_changes(self):
        self.c2()
        direct = self.db.execute('SELECT * FROM issues_files').fetchall()
        general = self.db.execute('SELECT * FROM volume_files').fetchall()
        history = self.db.execute('SELECT * FROM file_content_coverage').fetchall()
        canonical = self.db.execute('SELECT * FROM canonical_issue_files').fetchall()
        self.mark()
        # Persistence capability only: production restore must additionally
        # validate artifact/path/domain identity through a separate inverse job.
        self.db.execute('BEGIN IMMEDIATE')
        self.db.execute('DELETE FROM quarantined_files WHERE file_id=1')
        self.db.execute('UPDATE files SET filepath=? WHERE id=1', (str(self.path),))
        self.db.executemany('INSERT INTO issues_files VALUES(?,?,?)', direct)
        self.db.executemany('INSERT INTO volume_files VALUES(?,?,?,?)', general)
        self.db.commit()
        self.assertEqual(canonical, self.db.execute('SELECT * FROM canonical_issue_files').fetchall())
        self.assertEqual(history, self.db.execute('SELECT * FROM file_content_coverage').fetchall())


class QuarantineMigrationTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_65)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute("INSERT INTO config VALUES('database_version',65)")
        self.db.execute("INSERT INTO root_folders VALUES(1,'fixture')")
        self.db.execute("INSERT INTO volumes(id,title,root_folder,metadata_provider,comicvine_id) VALUES(1,'Fixture',1,'comicvine',100)")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(1,1,101,'1',1)")
        self.db.execute("INSERT INTO files VALUES(42,'fixture/file.cbz',17)")
        self.db.execute('INSERT INTO issues_files VALUES(42,1,1)')
        self.db.commit()

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_file_quarantine()

    def test_fresh_upgrade_repeat_reopen_preserve_existing_domain(self):
        tables = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='config'")]
        before = {name: self.db.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables}
        self.migrate()
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone(), (66,))
        for name in tables:
            self.assertEqual(before[name], self.db.execute(f'SELECT * FROM "{name}"').fetchall())
        after = tuple(self.db.iterdump())
        self.migrate()
        self.assertEqual(after, tuple(self.db.iterdump()))
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(SCHEMA_66)
            sql = "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            self.assertEqual(fresh.execute(sql).fetchall(), self.db.execute(sql).fetchall())
        with TemporaryDirectory(prefix='kapowarr-quarantine-migration-') as directory:
            path = Path(directory) / 'database.sqlite'
            with closing(sqlite3.connect(path)) as disk:
                self.db.backup(disk)
            with closing(sqlite3.connect(path)) as reopened:
                self.assertEqual(reopened.execute('SELECT * FROM active_files').fetchall(), [(42, 'fixture/file.cbz', 17)])
                self.assertEqual(reopened.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                self.assertEqual(reopened.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_each_migration_statement_failure_rolls_back_views_and_version(self):
        before = tuple(self.db.iterdump())
        for index in range(len(STATEMENTS) + 1):
            with self.subTest(index=index), patch('backend.internals.quarantine_schema.STATEMENTS',
                    STATEMENTS[:index] + ('INVALID SQL',) + STATEMENTS[index:]):
                with self.assertRaises(sqlite3.OperationalError):
                    self.migrate()
                self.assertEqual(before, tuple(self.db.iterdump()))

    def test_wrong_source_version_rejected(self):
        self.db.execute("UPDATE config SET value=64 WHERE key='database_version'")
        with self.assertRaisesRegex(RuntimeError, 'requires schema 65'):
            self.migrate()

    def test_historical_orphan_cleanup_before_schema_66_still_works(self):
        self.db.execute("INSERT INTO files VALUES(43,'ordinary-orphan',1)")
        with patch('backend.internals.db_models.get_db', side_effect=self.db.cursor):
            FilesDB.delete_unmatched_files()
        self.assertEqual(self.db.execute('SELECT id FROM files').fetchall(), [(42,)])

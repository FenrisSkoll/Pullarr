"""Deterministic disposable filesystem tests; no sleeps, network or providers."""

import os
import sqlite3
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from Tbackend.features.organization_plan import NAMING

from backend.base.folder_monitor import (ChangeKind, MonitorLimits,
                                         MonitorObservation, ObservationSource,
                                         ReevaluationResult)
from backend.features.folder_monitor import (FolderMonitor,
                                             monitoring_status, relevant)
from backend.features.library_reconciliation import LibraryReconciler
from backend.features.monitor_runtime import MonitorRuntime
from backend.internals.db import DB_SCHEMA
from backend.internals.db_migration import _migrate_folder_monitor
from backend.internals.monitor_schema import SCHEMA


class MonitoringTests(TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[3] / '.devdata'
        parent.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(prefix='monitor-test-', dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'library'
        self.root.mkdir()
        self.database = str(self.base / 'test.db')
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        self.db.executescript(DB_SCHEMA)
        self.db.execute("INSERT INTO config VALUES('database_version',55)")
        self.db.execute('INSERT INTO root_folders VALUES(1,?)', (str(self.root),))
        self.db.commit()
        self.processed = []
        self.limits = MonitorLimits(scan_interval=1, stability_interval=2)
        self.monitor = FolderMonitor(self.database, self.handle, self.limits)
        self.addCleanup(lambda: self.monitor.close())

    def handle(self, root, observed):
        self.processed.append(observed)
        return ReevaluationResult('reconciled', 'test_domain_result')

    def create(self, name='comic.cbz', data=b'archive'):
        path = self.root / name
        path.write_bytes(data)
        return path

    def row(self):
        return self.db.execute('SELECT health,error FROM monitor_roots').fetchone()

    def status(self, path):
        return self.db.execute('SELECT status FROM monitor_paths WHERE path=?', (str(path),)).fetchone()[0]

    def managed_library(self):
        from backend.base.definitions import SpecialVersion
        self.folder = self.root / 'Batman'
        self.folder.mkdir()
        self.db.executemany('INSERT INTO config VALUES(?,?)', ((f.name, getattr(NAMING, f.name)) for f in fields(NAMING)))
        self.db.execute('''INSERT INTO volumes(id,comicvine_id,title,year,volume_number,publisher,root_folder,folder,custom_folder,special_version)
            VALUES(1,101,'Batman',2020,1,'DC',1,?,0,?)''', (str(self.folder), SpecialVersion.NORMAL.value))
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,title,date) VALUES(1,1,201,'1',1,'Story','2020-01-02')")
        self.db.commit()
        self.monitor.reevaluate = LibraryReconciler(self.database, self.db)

    def comic(self, name='Batman 001 (2020).cbz'):
        path = self.folder / name
        with ZipFile(path, 'w') as archive:
            archive.writestr('001.jpg', b'disposable page fixture')
        return path

    def test_real_cbz_addition_uses_durable_db_only_organizer_job(self):
        self.managed_library()
        path = self.comic()
        original = path.read_bytes()
        self.monitor.tick(0)
        self.monitor.tick(3)
        self.assertEqual(self.status(path), 'reconciled')
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchall(), [(str(path),)])
        self.assertEqual(self.db.execute('SELECT issue_id FROM issues_files').fetchall(), [(1,)])
        self.assertEqual(self.db.execute('SELECT state FROM organization_jobs').fetchall(), [('completed',)])
        kinds = {r[0] for r in self.db.execute('SELECT kind FROM organization_steps')}
        self.assertEqual(kinds, {'register_or_update_file_record', 'update_issue_associations'})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_real_cbz_repeat_events_do_not_create_organization_loop(self):
        self.managed_library()
        self.comic()
        self.monitor.tick(0)
        self.monitor.tick(3)
        for n in range(4, 10):
            self.monitor.hint(MonitorObservation(1, '', ChangeKind.MODIFIED, n, ObservationSource.NOTIFICATION))
            self.monitor.tick(n)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)

    def test_external_rename_is_review_not_duplicate_registration(self):
        self.managed_library()
        path = self.comic()
        self.monitor.tick(0)
        self.monitor.tick(3)
        new = path.with_name('Batman 1 (2020).cbz')
        path.rename(new)
        self.monitor.tick(5)
        self.monitor.tick(7)
        self.assertEqual(self.status(path), 'missing')
        self.assertEqual(self.status(new), 'review')
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchall(), [(str(path),)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 1)

    def test_real_external_delete_keeps_authoritative_links(self):
        self.managed_library()
        path = self.comic()
        self.monitor.tick(0)
        self.monitor.tick(3)
        path.unlink()
        self.monitor.tick(5)
        self.assertEqual(self.status(path), 'missing')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 1)

    def test_unowned_addition_never_creates_volume_or_file(self):
        self.managed_library()
        self.create('Batman 001 (2020).cbz')
        self.monitor.tick(0)
        self.monitor.tick(3)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)

    def test_broken_archive_is_not_admitted(self):
        self.managed_library()
        path = self.folder / 'Batman 001 (2020).cbz'
        path.write_bytes(b'not yet an archive')
        self.monitor.tick(0)
        self.monitor.tick(3)
        self.assertEqual(self.status(path), 'review')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)

    def test_external_comicinfo_conflict_does_not_switch_authority(self):
        self.managed_library()
        path = self.comic()
        with ZipFile(path, 'a') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>Superman</Series><Number>99</Number></ComicInfo>')
        self.monitor.tick(0)
        self.monitor.tick(3)
        self.assertEqual(self.status(path), 'review')
        self.assertEqual(self.db.execute('SELECT metadata_provider FROM volumes').fetchone()[0], 'comicvine')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)

    def test_external_metadata_edit_after_internal_job_is_not_suppressed(self):
        self.managed_library()
        path = self.comic()
        self.monitor.tick(0)
        self.monitor.tick(3)
        with ZipFile(path, 'a') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>Superman</Series><Number>99</Number></ComicInfo>')
        self.monitor.tick(5)
        self.monitor.tick(8)
        self.assertEqual(self.status(path), 'review')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT metadata_provider FROM volumes').fetchone()[0], 'comicvine')

    def test_pending_organization_reservation_defers_to_recovery(self):
        self.managed_library()
        path = self.comic()
        # A real durable pending job is simpler than mocking SQLite internals.
        self.db.execute('''INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at)
            VALUES('pending','fixture','fixture','{}','fixture','pending','fixture','fixture')''')
        self.db.execute('INSERT INTO organization_reservations VALUES(?,?)', (os.path.normpath(str(path)).casefold(), 'pending'))
        self.db.commit()
        self.monitor.tick(0)
        self.monitor.tick(3)
        self.assertEqual(self.status(path), 'pending')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)

    def test_db_failure_retains_job_for_explicit_recovery(self):
        self.managed_library()
        path = self.comic()
        self.monitor.tick(0)
        with patch('backend.features.organization_execution.OrganizationExecutor._database_effect', side_effect=sqlite3.OperationalError()):
            self.monitor.tick(3)
        self.assertEqual(self.status(path), 'review')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT state FROM organization_jobs').fetchone()[0], 'recovery_required')

    def test_runtime_disabled_default_no_root_traversal_and_clean_stop(self):
        self.monitor.close()
        runtime = MonitorRuntime(self.database)
        with patch('backend.features.monitor_runtime.FolderMonitor', side_effect=AssertionError('disabled scan')):
            runtime.start()
            runtime.stop()
        self.assertFalse(runtime.thread.is_alive())
        self.monitor = FolderMonitor(self.database, self.handle, self.limits)

    def test_real_phase4h_move_and_comicinfo_does_not_loop(self):
        from Tbackend.features.organization_execution import ExecutionTests

        from backend.base.organization_job import ExecutionCode, JobState
        from backend.features.organization_execution import \
            OrganizationExecutor
        self.managed_library()
        self.source = self.root / 'Batman 001 (2020).cbz'
        with ZipFile(self.source, 'w') as archive:
            archive.writestr('001.jpg', b'disposable page')
        self.monitor.tick(0)
        original = self.source
        # Use the established exact-plan fixture, real writer and executor.
        plan = ExecutionTests.plan(self, metadata=True)
        executor = OrganizationExecutor(self.database, (str(self.root),))
        try:
            job = executor.create_job(plan)
            outcome = executor.apply_job(job)
            if outcome.error == ExecutionCode.UNSUPPORTED.value:
                # Some Windows-backed Docker mounts lack RENAME_NOREPLACE.
                # Verify fail-closed behavior before declaring the acceptance
                # scenario unavailable on this filesystem; never weaken 4H.
                self.assertEqual(outcome.state, JobState.RECOVERY)
                self.assertTrue(original.exists())
                self.assertFalse(Path(plan.target_path).exists())
                self.monitor.tick(3)
                self.monitor.tick(6)
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
                self.assertEqual(self.status(original), 'pending')
                self.skipTest('Filesystem lacks Phase 4H exclusive rename; recovery deferral verified')
            self.assertEqual(outcome.state, JobState.COMPLETED, outcome.error)
        finally:
            executor.close()
        self.monitor.reevaluate = LibraryReconciler(self.database, self.db)
        self.monitor.tick(3)
        self.monitor.tick(6)
        self.assertFalse(original.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], plan.target_path)
        self.assertEqual(self.status(plan.target_path), 'reconciled')

    def test_runtime_enabled_initializes_and_stops_without_sleep(self):
        from threading import Event
        self.monitor.close()
        self.db.execute("INSERT INTO config VALUES('folder_monitoring',1)")
        self.db.commit()
        ready = Event()
        runtime = MonitorRuntime(self.database)
        original = FolderMonitor.tick

        def tick(instance, now):
            original(instance, now)
            ready.set()
            runtime.stop_event.set()

        with patch.object(FolderMonitor, 'tick', tick):
            runtime.start()
            observed = ready.wait(5)
            runtime.stop()
        self.assertTrue(observed)
        self.assertFalse(runtime.thread.is_alive())
        self.monitor = FolderMonitor(self.database, self.handle, self.limits)

    def test_runtime_backend_initialization_failure_is_visible_and_stops(self):
        self.db.execute("INSERT INTO config VALUES('folder_monitoring',1)")
        self.db.commit()
        runtime = MonitorRuntime(self.database)
        with patch('backend.features.monitor_runtime.FolderMonitor', side_effect=OSError()):
            with patch.object(runtime.stop_event, 'wait', side_effect=lambda seconds: runtime.stop_event.set()):
                runtime.run()
        self.assertEqual(runtime.error, 'monitoring_unavailable')
        self.assertFalse(runtime.thread.is_alive())

    def test_active_work_does_not_starve_other_pending_files(self):
        self.monitor.limits = replace(self.limits, work_per_tick=1)
        self.create('a.cbz')
        self.create('b.cbz')
        calls = []

        def defer(root, observed):
            calls.append(observed.path)
            return ReevaluationResult('pending', 'active_job')

        self.monitor.reevaluate = defer
        self.monitor.tick(0)
        self.monitor.tick(3)
        self.monitor.tick(4)
        self.assertEqual(len(set(calls)), 2)

    def test_hint_memory_overflow_is_bounded_and_requests_all_roots(self):
        self.monitor.tick(0)
        for n in range(10000):
            self.monitor.hint(MonitorObservation(n, '', ChangeKind.OVERFLOW, n, ObservationSource.NOTIFICATION))
        self.assertLessEqual(len(self.monitor.hinted), 1024)
        self.assertTrue(self.monitor.hint_overflow)
        self.monitor._roots()
        self.assertEqual(self.db.execute('SELECT requested FROM monitor_roots').fetchone()[0], 1)

    def test_failed_migration_rolls_back_version_and_tables(self):
        from backend.internals.monitor_schema import STATEMENTS
        self.monitor.close()
        self.db.executescript('DROP TABLE monitor_staging; DROP TABLE monitor_paths; DROP TABLE monitor_roots;')
        self.db.execute("UPDATE config SET value=54 WHERE key='database_version'")
        self.db.commit()
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            with patch('backend.internals.monitor_schema.STATEMENTS', (*STATEMENTS[:1], 'INVALID SQL')):
                with self.assertRaises(sqlite3.Error):
                    _migrate_folder_monitor()
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 54)
        self.assertEqual(self.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'monitor_%'").fetchall(), [])
        self.db.executescript(SCHEMA)
        self.monitor = FolderMonitor(self.database, self.handle, self.limits)

    def test_queue_publication_failure_is_atomic_and_restart_converges(self):
        self.create()
        self.db.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON monitor_paths BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            self.monitor.tick(0)
        self.assertEqual(self.db.execute('SELECT generation FROM monitor_roots').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_paths').fetchone()[0], 0)
        self.monitor.close()
        self.db.execute('DROP TRIGGER injected_failure')
        self.db.commit()
        self.monitor = FolderMonitor(self.database, self.handle, self.limits)
        self.monitor.tick(3)
        self.monitor.tick(5)
        self.assertEqual(len(self.processed), 1)

    def test_disappearing_path_during_processing_is_not_admitted(self):
        path = self.create()
        self.monitor.tick(0)
        original = self.monitor._process

        def disappear(now):
            path.unlink()
            original(now)

        with patch.object(self.monitor, '_process', disappear):
            self.monitor.tick(3)
        self.assertEqual(self.processed, [])
        self.monitor.tick(5)
        self.assertEqual(self.status(path), 'missing')

    def test_cancellation_keeps_incomplete_generation_unpublished(self):
        self.create()
        self.monitor.cancelled = lambda: True
        self.monitor.tick(0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_paths').fetchone()[0], 0)
        self.assertEqual(self.processed, [])

    def test_synthetic_10000_entry_scan_has_bounded_slices_and_transactions(self):
        from time import perf_counter
        from tracemalloc import get_traced_memory, start, stop

        from backend.base.folder_monitor import PathStamp
        records = (PathStamp(str(self.root / f'{n}.cbz'), 100, 1, 1, n) for n in range(10000))
        statements = []
        self.monitor.db.set_trace_callback(lambda sql: statements.append(sql) if sql.startswith('BEGIN') else None)
        started = perf_counter()
        start()
        try:
            with patch('backend.features.folder_monitor.walk', return_value=iter(records)):
                for n in range(20):
                    before = self.monitor.entries_observed
                    self.monitor.tick(n)
                    self.assertLessEqual(self.monitor.entries_observed - before, self.limits.entries_per_tick)
            peak = get_traced_memory()[1]
        finally:
            stop()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_paths').fetchone()[0], 10000)
        self.assertLessEqual(len(statements), 24)
        self.assertEqual(self.processed, [])
        print(f'Monitor 10000-entry synthetic generation: {perf_counter() - started:.3f}s; '
              f'{len(statements)} transactions; Python peak {peak} bytes')

    def test_stability_requires_distinct_complete_observations(self):
        self.create()
        self.monitor.tick(0)
        self.assertEqual(self.processed, [])
        self.monitor.tick(1)
        self.assertEqual(self.processed, [])
        self.monitor.tick(2)
        self.assertEqual(len(self.processed), 1)
        self.monitor.tick(5)
        self.assertEqual(len(self.processed), 1)

    def test_growing_archive_resets_stability(self):
        path = self.create()
        self.monitor.tick(0)
        path.write_bytes(b'growing archive')
        self.monitor.tick(2)
        self.assertEqual(self.processed, [])
        self.monitor.tick(4)
        self.assertEqual(len(self.processed), 1)

    def test_modification_requeues_only_affected_file(self):
        path = self.create()
        self.create('second.cbz')
        self.monitor.tick(0)
        self.monitor.tick(2)
        path.write_bytes(b'changed content')
        self.monitor.tick(4)
        self.monitor.tick(6)
        self.assertEqual([p.path for p in self.processed].count(str(path)), 2)

    def test_delete_is_review_evidence_never_library_cleanup(self):
        path = self.create()
        self.db.execute('INSERT INTO files(filepath,size) VALUES(?,?)', (str(path), path.stat().st_size))
        self.db.commit()
        self.monitor.tick(0)
        path.unlink()
        self.monitor.tick(3)
        self.assertEqual(self.status(path), 'missing')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)
        self.assertEqual(self.processed, [])

    def test_unavailable_root_keeps_complete_baseline(self):
        path = self.create()
        self.monitor.tick(0)
        hidden = self.base / 'temporarily-unmounted'
        self.root.rename(hidden)
        self.monitor.tick(3)
        self.assertEqual(self.row()[0], 'unavailable')
        self.assertEqual(self.status(path), 'pending')
        self.assertEqual(self.processed, [])
        hidden.rename(self.root)
        self.monitor.tick(5)
        self.assertEqual(self.row()[0], 'healthy')
        self.assertEqual(len(self.processed), 1)

    def test_replaced_root_is_not_empty_baseline(self):
        path = self.create()
        self.monitor.tick(0)
        self.root.rename(self.base / 'original')
        self.root.mkdir()
        self.monitor.tick(3)
        self.assertEqual(self.row(), ('review', 'root_identity_changed'))
        self.assertNotEqual(self.status(path), 'missing')

    def test_permission_error_not_absence(self):
        path = self.create()
        self.monitor.tick(0)
        with patch('backend.features.folder_monitor.os.scandir', side_effect=PermissionError()):
            self.monitor.tick(3)
        self.assertEqual(self.row(), ('unavailable', 'permission_denied'))
        self.assertEqual(self.status(path), 'pending')

    def test_unsafe_link_is_rejected_without_following(self):
        self.create()
        with patch('backend.features.folder_monitor.safe_path', side_effect=ValueError('unsafe')):
            self.monitor.tick(0)
        self.assertNotEqual(self.row()[0], 'healthy')
        self.assertEqual(self.processed, [])

    def test_real_symlink_cannot_escape_configured_root(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'comic.cbz').write_bytes(b'disposable')
        try:
            (self.root / 'link').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit creating test symlinks')
        self.monitor.tick(0)
        self.assertNotEqual(self.row()[0], 'healthy')
        self.assertEqual(self.processed, [])
        self.assertTrue((outside / 'comic.cbz').exists())

    def test_root_disappearing_before_publication_preserves_prior_baseline(self):
        from backend.features.folder_monitor import stamp
        path = self.create()
        self.monitor.tick(0)
        self.monitor.limits = replace(self.limits, entries_per_tick=1)
        self.monitor.tick(3)

        def unavailable(value):
            if value == str(self.root):
                raise FileNotFoundError()
            return stamp(value)

        with patch('backend.features.folder_monitor.stamp', unavailable):
            self.monitor.tick(4)
        self.assertEqual(self.row()[0], 'unavailable')
        self.assertEqual(self.status(path), 'pending')
        self.assertEqual(self.processed, [])

    def test_db_only_admission_cannot_call_filesystem_mutators(self):
        from contextlib import ExitStack
        self.managed_library()
        self.comic()
        self.monitor.tick(0)
        with ExitStack() as guards:
            for name in ('os.rename', 'os.unlink', 'os.mkdir', 'shutil.move', 'shutil.copy2',
                         'backend.features.organization_execution.write_comicinfo'):
                guards.enter_context(patch(name, side_effect=AssertionError('Unauthorized mutation: ' + name)))
            self.monitor.tick(3)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 1)

    def test_relevant_types_use_canonical_extension_lists(self):
        for name in ('a.CBZ', 'a.cbr', 'a.zip', 'a.rar', 'ComicInfo.xml', 'a.pdf', 'a.tar.gz'):
            self.assertTrue(relevant(name), name)
        for name in ('.hidden.cbz', 'a.cbz.part', 'a.jpg', 'a.txt', 'other.xml'):
            self.assertFalse(relevant(name), name)

    def test_irrelevant_files_do_not_generate_work(self):
        self.create('notes.txt')
        self.create('.internal.cbz')
        self.monitor.tick(0)
        self.monitor.tick(3)
        self.assertEqual(self.processed, [])

    def test_directory_creation_and_removal_are_advisory(self):
        directory = self.root / 'series'
        directory.mkdir()
        self.monitor.tick(0)
        self.assertEqual(self.status(directory), 'observed')
        directory.rmdir()
        self.monitor.tick(3)
        self.assertEqual(self.status(directory), 'missing')
        self.assertEqual(self.processed, [])

    def test_event_storm_collapses_to_one_dirty_root(self):
        self.monitor.tick(0)
        for n in range(1000):
            self.monitor.hint(MonitorObservation(1, '../../untrusted', ChangeKind.MODIFIED, n,
                                                ObservationSource.NOTIFICATION))
        self.assertEqual(len(self.monitor.hinted), 1)
        self.monitor._roots()
        self.assertEqual(self.db.execute('SELECT COUNT(*),SUM(requested) FROM monitor_roots').fetchone(), (1, 1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_paths').fetchone()[0], 0)

    def test_overflow_requests_reconciliation_not_raw_replay(self):
        self.monitor.tick(0)
        self.create()
        self.monitor.hint(MonitorObservation(1, '', ChangeKind.OVERFLOW, 1, ObservationSource.NOTIFICATION))
        self.monitor.tick(3)
        self.monitor.tick(5)
        self.assertEqual(len(self.processed), 1)

    def test_shutdown_mid_scan_does_not_publish_missing(self):
        self.monitor.limits = replace(self.limits, entries_per_tick=1)
        for name in ('a.cbz', 'b.cbz', 'c.cbz'):
            self.create(name)
        self.monitor.tick(0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_paths').fetchone()[0], 0)
        self.monitor.close()
        self.monitor = FolderMonitor(self.database, self.handle, self.limits)
        self.monitor.tick(3)
        self.monitor.tick(5)
        self.assertEqual(len(self.processed), 3)

    def test_downtime_changes_found_and_pending_work_survives(self):
        self.create()
        self.monitor.tick(0)
        self.monitor.close()
        self.create('offline.cbz')
        self.monitor = FolderMonitor(self.database, self.handle, self.limits)
        self.monitor.tick(3)
        self.monitor.tick(5)
        self.assertEqual(len(self.processed), 2)

    def test_scan_failure_preserves_published_generation(self):
        self.create()
        self.monitor.tick(0)
        with patch('backend.features.folder_monitor.walk', side_effect=OSError('backend failure')):
            self.monitor.tick(3)
        self.assertEqual(self.db.execute('SELECT generation FROM monitor_roots').fetchone()[0], 1)
        self.assertEqual(self.processed, [])

    def test_scan_limit_never_silently_truncates(self):
        self.monitor.limits = replace(self.limits, max_entries=1)
        self.create('a.cbz')
        self.create('b.cbz')
        self.monitor.tick(0)
        self.assertEqual(self.row()[0], 'review')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_paths').fetchone()[0], 0)

    def test_work_batch_and_scan_slice_are_bounded(self):
        self.monitor.limits = replace(self.limits, scan_interval=30, entries_per_tick=8, work_per_tick=3)
        for number in range(100):
            self.create(f'{number}.cbz')
        self.monitor.tick(0)
        self.assertEqual(self.monitor.entries_observed, 8)
        for n in range(1, 200):
            before = len(self.processed)
            self.monitor.tick(n)
            self.assertLessEqual(len(self.processed) - before, 3)
        self.assertEqual(len(self.processed), 100)

    def test_status_is_bounded_read_only(self):
        self.create()
        self.monitor.tick(0)
        before = self.db.total_changes
        self.assertEqual(monitoring_status(self.database)['backend'], 'bounded_polling')
        self.assertEqual(self.db.total_changes, before)

    def test_additive_migration_and_fresh_schema_parity(self):
        self.monitor.close()
        self.db.executescript('DROP TABLE monitor_staging; DROP TABLE monitor_paths; DROP TABLE monitor_roots;')
        self.db.execute("UPDATE config SET value=54 WHERE key='database_version'")
        self.db.commit()
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_folder_monitor()
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 55)
        old = self.db.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'monitor_%' ORDER BY name").fetchall()
        self.db.executescript(SCHEMA)
        self.assertEqual(old, self.db.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'monitor_%' ORDER BY name").fetchall())
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.monitor = FolderMonitor(self.database, self.handle, self.limits)

"""Real quarantine/restore effects on disposable trees and domain fixtures."""

import json
import os
import sqlite3
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

import TDuplicateReview as duplicate_fixture

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.folder_monitor import MonitorLimits
from backend.base.library_health import HealthLevel
from backend.base.organization_job import (EXECUTOR_POLICY, JobState,
                                           OrganizationError)
from backend.base.switch_review import SwitchReviewError
from backend.features.folder_monitor import walk
from backend.features.organization_execution import OrganizationExecutor
from backend.features.organization_quarantine import MOVE, RECORD, VERSION
from backend.features.wanted_status import wanted_rows
from backend.implementations.duplicate_evidence import HashBudget
from backend.implementations.file_matching import scan_files
from backend.implementations.quarantine_location import observe_location
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.db_models import FilesDB
from backend.internals.organization_jobs import canonical, digest
from backend.internals.organization_reservations import path_key
from backend.internals.provider_authority import capture
from backend.internals.quarantine_state import snapshot


class QuarantineExecutionTests(TestCase):
    def setUp(self):
        self.fixture = duplicate_fixture.DuplicateReviewTests('test_three_copies_one_equivalence_class')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.health = self.fixture.db, self.fixture.health
        self.source = self.fixture.paths[0]
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',66)")
        self.db.execute("INSERT INTO volume_files(file_id,volume_id,file_type,forced) VALUES(1,1,'metadata',1)")
        self.db.execute('UPDATE issues_files SET forced=1')
        self.db.commit()
        self.executor = OrganizationExecutor(str(self.health.database), (str(self.health.root),))
        self.addCleanup(self.executor.close)

    def register_fixture(self):
        # Trusted test intent, not a public replacement for owned 8C/8G review.
        location = observe_location(self.db.cursor(), 1, 'b' * 32)
        self.target = Path(location['target'])
        budget = HashBudget()
        intent = dict(version=EXECUTOR_POLICY, quarantine_effect=VERSION, effects=[MOVE, RECORD],
            source=str(self.source), target=str(self.target), original=str(self.source),
            quarantine_target=str(self.target), root=location['root'], root_id=location['root_id'],
            volume_id=1, file_id=1, inverse=False, location_id='b' * 32,
            hash=budget.inspect(str(self.source)), retained_ids=[2], batch_removed=[1],
            retained_hashes={'2': budget.inspect(str(self.fixture.paths[1]))},
            quarantine_before=snapshot(self.db.cursor(), (1, 2)))
        self.intent = json.loads(canonical(intent))
        self.executor._validate_intent(self.intent)
        self.job = self.executor.store.create(self.intent, digest(canonical(intent)),
            (path_key(str(self.source)), path_key(str(self.target))), 'quarantine-test-batch')
        self.assertFalse(self.target.parent.exists())
        return self.job

    def assert_inactive(self):
        self.assertFalse(self.source.exists())
        self.assertEqual(self.db.execute('SELECT id,filepath FROM files WHERE id=1').fetchone(), (1, str(self.target)))
        self.assertEqual(self.db.execute('SELECT file_id FROM quarantined_files').fetchall(), [(1,)])
        self.assertEqual(self.db.execute('SELECT * FROM issues_files WHERE file_id=1').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT * FROM volume_files WHERE file_id=1').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT DISTINCT file_id FROM canonical_issue_files').fetchall(), [(2,)])
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_quarantine_and_separately_confirmed_restore(self):
        content, before_stat = self.source.read_bytes(), self.source.stat()
        direct = self.db.execute('SELECT * FROM issues_files ORDER BY file_id').fetchall()
        general = self.db.execute('SELECT * FROM volume_files').fetchall()
        metadata = self.db.execute('SELECT * FROM volumes').fetchall()
        self.register_fixture()
        timings = {}
        def observe(stage, job, ordinal):
            if stage in ('before_effect', 'after_effect', 'before_db_commit', 'after_db_commit'):
                timings[(stage, ordinal)] = perf_counter()
        self.executor.hook = observe
        started = perf_counter()
        result = self.executor.apply_job(self.job)
        apply_seconds = perf_counter() - started
        self.assertEqual(result.state, JobState.COMPLETED)
        self.assert_inactive()
        self.assertEqual(sha256(content).digest(), sha256(self.target.read_bytes()).digest())
        self.assertEqual(before_stat.st_mtime_ns, self.target.stat().st_mtime_ns)
        self.assertEqual(before_stat.st_ino, self.target.stat().st_ino)
        with ZipFile(self.target) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(b'<Series>Example</Series>', archive.read('ComicInfo.xml'))
        self.assertEqual(metadata, self.db.execute('SELECT * FROM volumes').fetchall())
        preview = self.executor.preview_undo(self.job)
        self.assertTrue(preview.eligible, preview.reasons)
        inverse = self.executor.create_undo_job(self.job, preview.intent_digest)
        self.assertEqual(self.executor.create_undo_job(self.job, preview.intent_digest), inverse)
        namespace_seconds = timings[('after_effect', 0)] - timings[('before_effect', 0)]
        db_seconds = timings[('after_db_commit', 1)] - timings[('before_db_commit', 1)]
        started = perf_counter()
        self.assertEqual(self.executor.apply_job(inverse).state, JobState.COMPLETED)
        print('8G quarantine/restore runtime', dict(platform=os.name, apply_seconds=apply_seconds,
            namespace_guarded_seconds=namespace_seconds, db_reconcile_seconds=db_seconds,
            restore_seconds=perf_counter() - started, bytes=len(content)))
        self.assertEqual(self.source.read_bytes(), content)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.db.execute('SELECT * FROM quarantined_files').fetchall(), [])
        self.assertEqual(direct, self.db.execute('SELECT * FROM issues_files ORDER BY file_id').fetchall())
        self.assertEqual(general, self.db.execute('SELECT * FROM volume_files').fetchall())
        self.assertEqual(self.db.execute('SELECT id,filepath FROM files WHERE id=1').fetchone(), (1, str(self.source)))

    def test_domain_changes_block_conditional_restore(self):
        changes = (
            ("UPDATE volumes SET folder=folder || '-moved' WHERE id=1",),
            ("UPDATE issue_external_ids SET provider_id='changed' WHERE issue_id=1 AND provider='comicvine'",),
            ('DELETE FROM issues_files', 'DELETE FROM issues'),
            ('DELETE FROM issues_files', 'DELETE FROM issues', 'DELETE FROM volumes', 'DELETE FROM root_folders'),
        )
        for statements in changes:
            with self.subTest(statements=statements):
                child = QuarantineExecutionTests()
                child.setUp()
                try:
                    child.register_fixture()
                    self.assertEqual(child.executor.apply_job(child.job).state, JobState.COMPLETED)
                    for sql in statements:
                        child.db.execute(sql)
                    child.db.commit()
                    self.assertFalse(child.executor.preview_undo(child.job).eligible)
                    self.assertTrue(child.target.exists())
                    self.assertFalse(child.source.exists())
                finally:
                    child.doCleanups()

    def interrupt(self, stage, ordinal=None):
        def hook(current, job, number):
            if current == stage and (ordinal is None or ordinal == number):
                raise SystemExit('simulated process exit')
        self.executor.hook = hook
        with self.assertRaises(SystemExit):
            self.executor.apply_job(self.job)
        self.executor.hook = lambda *_: None

    def test_restart_after_namespace_move_forward_reconciles(self):
        self.register_fixture()
        self.interrupt('after_effect', 0)
        self.assertTrue(self.target.exists())
        self.assertEqual(self.db.execute('SELECT filepath FROM files WHERE id=1').fetchone(), (str(self.source),))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone(), (2,))
        reopened = OrganizationExecutor(str(self.health.database), (str(self.health.root),))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.reconcile_job(self.job).state, JobState.RECOVERY)
        self.assertEqual(reopened.apply_job(self.job).state, JobState.COMPLETED)
        self.assertEqual(reopened.apply_job(self.job).state, JobState.COMPLETED)
        self.assert_inactive()

    def test_post_move_unrelated_metadata_does_not_retarget_recovery(self):
        self.register_fixture()
        self.interrupt('after_effect', 0)
        self.db.execute("UPDATE volumes SET title='later metadata' WHERE id=1")
        self.db.execute("INSERT OR REPLACE INTO config VALUES('file_naming','later setting')")
        self.db.commit()
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        self.assert_inactive()
        self.assertEqual(self.db.execute('SELECT title FROM volumes WHERE id=1').fetchone(), ('later metadata',))

    def test_both_paths_requires_inspection_without_deleting_either(self):
        self.register_fixture()
        self.interrupt('after_effect', 0)
        self.source.write_bytes(b'new unrelated artifact')
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.RECOVERY)
        self.assertEqual(self.source.read_bytes(), b'new unrelated artifact')
        self.assertTrue(self.target.exists())
        self.assertEqual(self.db.execute('SELECT * FROM quarantined_files').fetchall(), [])

    def test_neither_path_requires_inspection(self):
        self.register_fixture()
        self.interrupt('after_effect', 0)
        self.target.rename(self.target.with_suffix('.externally-moved'))
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.RECOVERY)
        self.assertEqual(self.db.execute('SELECT * FROM quarantined_files').fetchall(), [])

    def test_target_mismatch_cannot_reconcile(self):
        self.register_fixture()
        self.interrupt('after_effect', 0)
        self.target.write_bytes(b'modified retained artifact')
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.RECOVERY)
        self.assertEqual(self.db.execute('SELECT * FROM quarantined_files').fetchall(), [])

    def test_source_only_interruption_can_retry(self):
        self.register_fixture()
        self.interrupt('before_effect', 0)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        self.assert_inactive()

    def test_changed_retained_copy_blocks_move(self):
        self.register_fixture()
        self.fixture.paths[1].write_bytes(b'not the reviewed copy')
        self.assertNotEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.target.exists())

    def test_new_active_path_in_quarantine_storage_blocks_namespace(self):
        self.register_fixture()
        foreign = self.target.parent.parent / 'foreign.cbz'
        self.db.execute('INSERT INTO files(filepath,size) VALUES(?,1)', (str(foreign),))
        self.db.commit()
        self.assertNotEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.target.exists())

    def test_unknown_effect_version_fails_before_mutation(self):
        self.register_fixture()
        bad = dict(self.intent, quarantine_effect='retained-artifact/v99')
        with self.assertRaises(OrganizationError):
            self.executor._validate_intent(bad)
        self.assertTrue(self.source.exists())

    def test_namespace_failure_preserves_source_and_reservations(self):
        self.register_fixture()
        with patch('backend.features.organization_quarantine.rename_no_replace', side_effect=PermissionError):
            self.assertEqual(self.executor.apply_job(self.job).state, JobState.RECOVERY)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.target.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone(), (2,))
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)

    def test_post_commit_completion_gap_and_restore_failures_recover(self):
        cases = ((False, 'after_db_commit', 1), (False, 'before_quarantine_setup', 0),
                 (True, 'before_effect', 0), (True, 'after_effect', 0),
                 (True, 'quarantine_direct_reconciled', 1),
                 (True, 'quarantine_general_reconciled', 1),
                 (True, 'quarantine_before_commit', 1), (True, 'after_db_commit', 1))
        for inverse, stage, ordinal in cases:
            with self.subTest(inverse=inverse, stage=stage):
                child = QuarantineExecutionTests()
                child.setUp()
                try:
                    child.register_fixture()
                    content = child.source.read_bytes()
                    direct = child.db.execute('SELECT * FROM issues_files').fetchall()
                    general = child.db.execute('SELECT * FROM volume_files').fetchall()
                    if inverse:
                        self.assertEqual(child.executor.apply_job(child.job).state, JobState.COMPLETED)
                        preview = child.executor.preview_undo(child.job)
                        child.job = child.executor.create_undo_job(child.job, preview.intent_digest)
                    child.interrupt(stage, ordinal)
                    self.assertEqual(child.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone(), (2,))
                    reopened = OrganizationExecutor(str(child.health.database), (str(child.health.root),))
                    try:
                        self.assertEqual(reopened.apply_job(child.job).state, JobState.COMPLETED)
                        self.assertEqual(reopened.apply_job(child.job).state, JobState.COMPLETED)
                    finally:
                        reopened.close()
                    if inverse:
                        self.assertEqual(child.source.read_bytes(), content)
                        self.assertEqual(child.db.execute('SELECT * FROM quarantined_files').fetchall(), [])
                        self.assertCountEqual(direct, child.db.execute('SELECT * FROM issues_files').fetchall())
                        self.assertCountEqual(general, child.db.execute('SELECT * FROM volume_files').fetchall())
                    else:
                        child.assert_inactive()
                    self.assertEqual(child.db.execute('PRAGMA foreign_key_check').fetchall(), [])
                finally:
                    child.doCleanups()

    def test_restore_blocks_occupied_original_and_changed_authority(self):
        self.register_fixture()
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        self.source.write_bytes(b'unrelated')
        self.assertFalse(self.executor.preview_undo(self.job).eligible)
        self.source.rename(self.source.with_suffix('.unrelated'))
        self.db.execute('UPDATE volumes SET authority_generation=authority_generation+1 WHERE id=1')
        self.db.commit()
        self.assertFalse(self.executor.preview_undo(self.job).eligible)

    def test_db_failure_rollback_and_forward_recovery(self):
        # Every meaningful write boundary is exercised against a fresh fixture.
        for stage in ('quarantine_before_filepath', 'quarantine_before_marker', 'quarantine_marker_inserted',
                      'quarantine_direct_reconciled', 'quarantine_general_reconciled', 'quarantine_before_commit'):
            with self.subTest(stage=stage):
                child = QuarantineExecutionTests('test_restart_after_namespace_move_forward_reconciles')
                child.setUp()
                try:
                    child.register_fixture()
                    direct = child.db.execute('SELECT * FROM issues_files').fetchall()
                    general = child.db.execute('SELECT * FROM volume_files').fetchall()
                    child.interrupt(stage, 1)
                    self.assertTrue(child.target.exists())
                    self.assertEqual(child.db.execute('SELECT * FROM quarantined_files').fetchall(), [])
                    self.assertEqual(direct, child.db.execute('SELECT * FROM issues_files').fetchall())
                    self.assertEqual(general, child.db.execute('SELECT * FROM volume_files').fetchall())
                    self.assertEqual(child.executor.apply_job(child.job).state, JobState.COMPLETED)
                    child.assert_inactive()
                finally:
                    child.doCleanups()

    def scan_context(self, stack):
        for module in ('backend.implementations.file_matching', 'backend.internals.db_models'):
            stack.enter_context(patch(module + '.get_db', side_effect=self.db.cursor))
        settings = stack.enter_context(patch('backend.implementations.file_matching.Settings'))
        settings.return_value.get_settings.return_value = SimpleNamespace(
            create_empty_volume_folders=True, delete_empty_folders=False, unmonitor_deleted_issues=True)
        volume = stack.enter_context(patch('backend.implementations.volumes.Volume')).return_value
        volume.get_data.return_value = SimpleNamespace(folder=str(self.health.volume), root_folder=1)
        volume.get_issues.return_value = [SimpleNamespace(id=1, calculated_issue_number=1, date='2020-01-01')]
        volume.get_all_files.side_effect = lambda: [dict(id=r[0], filepath=r[1]) for r in
            self.db.execute('SELECT id,filepath FROM active_files')]
        volume.get_general_files.return_value = []

    def test_manual_and_token_scan_excluded_during_namespace_database_gap(self):
        self.register_fixture()
        token = capture(self.db.cursor(), (1,))[1]
        self.interrupt('after_effect', 0)
        before = tuple(self.db.iterdump())
        with ExitStack() as stack:
            self.scan_context(stack)
            with self.assertRaises((OrganizationError, SwitchReviewError)):
                scan_files(1)
            self.assertEqual(scan_files(1, expected_authority=token), 'deferred_organization_reservation')
        self.assertEqual(before, tuple(self.db.iterdump()))
        self.assertFalse(self.source.exists())
        self.assertTrue(self.target.exists())
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        # Remove the remaining fixture artifact from scan's input without
        # mutating library state; this characterizes pruning retained identity.
        with patch('backend.internals.db_models.get_db', side_effect=self.db.cursor):
            FilesDB.delete_unmatched_files()
        self.db.commit()
        self.assert_inactive()

    def test_legacy_volume_delete_excluded_during_namespace_database_gap(self):
        from backend.implementations.volumes import (Issue, Volume,
                                                     delete_issue_file)

        self.register_fixture()
        self.interrupt('after_effect', 0)
        before = tuple(self.db.iterdump())
        volume = Volume(1)
        with ExitStack() as stack:
            for module in ('backend.implementations.volumes', 'backend.internals.db_models'):
                stack.enter_context(patch(module + '.get_db', side_effect=self.db.cursor))
            stack.enter_context(patch('backend.features.tasks.TaskHandler.task_for_volume_running', return_value=False))
            downloads = stack.enter_context(patch('backend.features.download_queue.DownloadHandler'))
            downloads.return_value.download_for_volume_queued.return_value = False
            stack.enter_context(patch.object(volume, 'get_data', return_value=SimpleNamespace(
                folder=str(self.health.volume), root_folder=1)))
            for action in (lambda: volume.delete(False), lambda: volume.delete(True),
                           Issue(1).delete, lambda: delete_issue_file(1)):
                with self.subTest(action=action), self.assertRaises(OrganizationError):
                    action()
        self.assertEqual(before, tuple(self.db.iterdump()))
        self.assertTrue(self.target.exists())
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)

    def test_unreserved_legacy_delete_preserves_caller_transaction(self):
        from backend.implementations.volumes import Volume

        original = self.db.execute('SELECT * FROM volumes').fetchall()
        self.db.execute("UPDATE volumes SET title='caller work' WHERE id=1")
        volume = Volume(1)
        with ExitStack() as stack:
            for module in ('backend.implementations.volumes', 'backend.internals.db_models'):
                stack.enter_context(patch(module + '.get_db', side_effect=self.db.cursor))
            stack.enter_context(patch('backend.features.tasks.TaskHandler.task_for_volume_running', return_value=False))
            downloads = stack.enter_context(patch('backend.features.download_queue.DownloadHandler'))
            downloads.return_value.download_for_volume_queued.return_value = False
            stack.enter_context(patch.object(volume, 'get_data', return_value=SimpleNamespace(
                folder=str(self.health.volume), root_folder=1)))
            volume.delete(False)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT * FROM volumes').fetchall(), [])
        self.assertTrue(self.source.exists())
        self.db.rollback()
        self.assertEqual(original, self.db.execute('SELECT * FROM volumes').fetchall())

    def test_populated_c2_history_and_canonical_ownership_survive(self):
        self.db.execute('UPDATE volumes SET monitored=1 WHERE id=1')
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(2,1,102,'2',2,1)")
        ref = PublicationRef('comicvine', '102')
        preview = claim_preview(self.db.cursor(), 1, ref, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(self.db.cursor(), 1, ref, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        for fid in (1, 2):
            preview = coverage_preview(self.db.cursor(), 1, fid, [claim])
            apply_coverage(self.db.cursor(), 1, fid, [claim], preview['preview_token'])
        self.db.commit()
        claims = self.db.execute('SELECT * FROM bibliographic_content_claims').fetchall()
        coverage = self.db.execute('SELECT * FROM file_content_coverage').fetchall()
        owned = self.db.execute('SELECT DISTINCT issue_id FROM canonical_issue_files ORDER BY issue_id').fetchall()
        def wanted():
            self.db.row_factory = sqlite3.Row
            try:
                return {r['id']: (r['owned'], r['wanted']) for r in wanted_rows(SimpleNamespace(db=self.db, clock=lambda: 0))}
            finally:
                self.db.row_factory = None
        wanted_before = wanted()
        self.register_fixture()
        self.interrupt('after_effect', 0)
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        self.assertEqual(claims, self.db.execute('SELECT * FROM bibliographic_content_claims').fetchall())
        self.assertEqual(coverage, self.db.execute('SELECT * FROM file_content_coverage').fetchall())
        self.assertEqual(owned, self.db.execute('SELECT DISTINCT issue_id FROM canonical_issue_files ORDER BY issue_id').fetchall())
        self.assertEqual(wanted_before, wanted())
        self.assertEqual(self.db.execute('SELECT DISTINCT file_id FROM valid_file_content_coverage').fetchall(), [(2,)])
        preview = self.executor.preview_undo(self.job)
        self.assertTrue(preview.eligible, preview.reasons)
        inverse = self.executor.create_undo_job(self.job, preview.intent_digest)
        self.assertEqual(self.executor.apply_job(inverse).state, JobState.COMPLETED)
        self.assertEqual(coverage, self.db.execute('SELECT * FROM file_content_coverage').fetchall())
        self.assertEqual(self.db.execute('SELECT DISTINCT file_id FROM valid_file_content_coverage ORDER BY file_id').fetchall(), [(1,), (2,)])

    def test_completed_quarantine_survives_scans_health_monitor_and_rediscovery(self):
        self.register_fixture()
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        token = capture(self.db.cursor(), (1,))[1]
        with ExitStack() as stack:
            self.scan_context(stack)
            self.assertEqual(scan_files(1), 'completed')
            self.assertEqual(scan_files(1, expected_authority=token), 'completed')
        self.assert_inactive()
        report = self.health.scan(HealthLevel.DEEP)
        self.assertFalse(any(f.code in ('missing_file', 'untracked_file', 'exact_byte_duplicate') for f in report.findings))
        observed = [s.path for s in walk(str(self.health.root), MonitorLimits()) if s is not None]
        self.assertNotIn(str(self.target), observed)
        self.assertFalse(self.source.exists())
        preview = self.executor.preview_undo(self.job)
        self.assertTrue(preview.eligible, preview.reasons)
        inverse = self.executor.create_undo_job(self.job, preview.intent_digest)
        self.assertEqual(self.executor.apply_job(inverse).state, JobState.COMPLETED)
        report = self.health.scan(HealthLevel.DEEP)
        self.assertTrue(any(f.code == 'exact_byte_duplicate' for f in report.findings))

    def test_inverse_interruption_and_transaction_rollback_recover(self):
        self.register_fixture()
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        preview = self.executor.preview_undo(self.job)
        self.job = self.executor.create_undo_job(self.job, preview.intent_digest)
        self.interrupt('quarantine_marker_removed', 1)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.target.exists())
        self.assertEqual(self.db.execute('SELECT file_id FROM quarantined_files').fetchall(), [(1,)])
        self.assertEqual(self.db.execute('SELECT * FROM issues_files WHERE file_id=1').fetchall(), [])
        self.assertEqual(self.executor.apply_job(self.job).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT * FROM quarantined_files').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT file_id FROM issues_files WHERE file_id=1').fetchall(), [(1,)])

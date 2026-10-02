"""Disposable, file-backed crash/restart acceptance. No provider or live library."""

import errno
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from zipfile import ZipFile

from Tbackend.features.organization_plan import NAMING

from backend.base.definitions import SpecialVersion
from backend.base.import_candidate import DiscoveryScope
from backend.base.organization_job import (ExecutionCode, JobState,
                                           OrganizationError, StepState)
from backend.base.organization_plan import (EffectKind, MetadataMode,
                                            PlanningPolicy, PlanStatus)
from backend.features.organization_execution import OrganizationExecutor
from backend.features.organization_plan import observe_plan_paths
from backend.implementations.comicinfo_archive import inspect_comicinfo
from backend.implementations.comicinfo_candidate import enrich_comicinfo
from backend.implementations.identification import MatchingSnapshot, identify
from backend.implementations.import_candidates import observe_import_candidate
from backend.implementations.organization_filesystem import execution_gate
from backend.implementations.organization_plan import PlanningContext, plan_one
from backend.internals.db import DB_SCHEMA
from backend.internals.import_identity import load_existing_import_identities
from backend.internals.organization_plan import load_planning_records


class Interrupted(BaseException):
    """Simulated process death bypasses operational-error handling."""


class ExecutionTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kapowarr-execution-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.folder = self.library / 'Batman'
        self.folder.mkdir()
        self.incoming = self.root / 'incoming'
        self.incoming.mkdir()
        self.source = self.incoming / 'Batman 001 (2020).cbz'
        with ZipFile(self.source, 'w') as archive:
            archive.writestr('001.jpg', b'disposable synthetic page')
        self.dbpath = str(self.root / 'test.db')
        self.db = sqlite3.connect(self.dbpath, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA)
        self.db.execute("INSERT INTO config VALUES('database_version',54)")
        self.db.executemany('INSERT INTO config VALUES(?,?)', ((f.name, getattr(NAMING, f.name)) for f in fields(NAMING)))
        self.db.execute('INSERT INTO root_folders VALUES(1,?)', (str(self.library),))
        self.db.execute('''INSERT INTO volumes(id,comicvine_id,title,year,volume_number,publisher,root_folder,folder,custom_folder,special_version)
            VALUES(1,101,'Batman',2020,1,'DC',1,?,0,?)''', (str(self.folder), SpecialVersion.NORMAL.value))
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,title,date) VALUES(1,1,201,'1',1,'Story','2020-01-02')")
        self.executors = []
        self.addCleanup(self.close_executors)
        self.executor = self.open_executor()

    def close_executors(self):
        for executor in self.executors:
            executor.close()
        self.executors.clear()

    def open_executor(self, hook=None):
        executor = OrganizationExecutor(self.dbpath, (str(self.root),), checkpoint=hook)
        self.executors.append(executor)
        return executor

    def plan(self, metadata=False, rename=True, move=True):
        vs, issues, roots, files, naming = load_planning_records(('comicvine', 'metron'), self.db.cursor())
        with patch('backend.internals.import_identity.get_db', side_effect=self.db.cursor):
            known = load_existing_import_identities([str(self.source)], ('comicvine', 'metron')).get(str(self.source))
        c = observe_import_candidate(str(self.source), DiscoveryScope('disposable', str(self.root)), existing=known)
        if metadata:
            c = enrich_comicinfo(c)
        selected = identify(c, MatchingSnapshot.build((v.identity for v in vs), (i.identity for i in issues)))
        policy = PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt', rename=rename, move=move,
                                metadata=MetadataMode.REQUIRED if metadata else MetadataMode.OFF)
        context = PlanningContext.build(vs, issues, roots, files, naming, policy)
        provisional = plan_one(selected, context)
        paths = [str(self.source), provisional.target_path, provisional.target_folder, provisional.target_root]
        context = PlanningContext.build(vs, issues, roots, files, naming, policy, observe_plan_paths((p for p in paths if p), policy))
        result = plan_one(selected, context)
        self.assertIn(result.status, (PlanStatus.READY, PlanStatus.NO_CHANGES), result.diagnostics)
        return result

    def register(self, forced=False):
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(self.source), self.source.stat().st_size))
        self.db.execute('INSERT INTO issues_files(file_id,issue_id,forced) VALUES(1,1,?)', (int(forced),))

    def interrupt(self, phase, effect):
        def hook(stage, job, ordinal):
            current = self.executor.store.get(job)
            if stage == phase and current.steps[ordinal].kind == effect.value:
                raise Interrupted()
        self.executor.hook = hook

    def test_end_to_end_apply_history_and_undo(self):
        self.register(True)
        original = self.source.read_bytes()
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.assertTrue(self.source.exists())
        applied = self.executor.apply_job(job)
        self.assertEqual(applied.state, JobState.COMPLETED, applied.error)
        self.assertFalse(self.source.exists())
        self.assertEqual(Path(plan.target_path).read_bytes(), original)
        self.assertEqual(self.db.execute('SELECT filepath FROM files WHERE id=1').fetchone()[0], plan.target_path)
        self.assertEqual(self.db.execute('SELECT * FROM issues_files').fetchall(), [(1, 1, 1)])
        preview = self.executor.preview_undo(job)
        self.assertTrue(preview.eligible, preview.reasons)
        undo = self.executor.create_undo_job(job, preview.intent_digest)
        result = self.executor.apply_job(undo)
        self.assertEqual(result.state, JobState.COMPLETED, result.error)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.db.execute('SELECT filepath FROM files WHERE id=1').fetchone()[0], str(self.source))
        self.assertEqual(len(self.executor.history()), 2)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_new_file_association_undo_removes_only_created_record(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        undo = self.executor.create_undo_job(job, self.executor.preview_undo(job).intent_digest)
        self.assertEqual(self.executor.apply_job(undo).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT * FROM files').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 1)

    def test_pending_survives_reopen_without_mutation(self):
        job = self.executor.create_job(self.plan())
        self.close_executors()
        self.executor = self.open_executor()
        self.assertEqual(self.executor.reconcile_job(job).state, JobState.PENDING)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)

    def test_corrupt_validation_evidence_cannot_resume(self):
        job = self.executor.create_job(self.plan())
        self.interrupt('after_started', EffectKind.RELOCATE)
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        self.executor.hook = lambda *args: None
        self.db.execute("UPDATE organization_events SET detail='{}' WHERE job_id=? AND event='validated'", (job,))
        result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.RECOVERY)
        self.assertEqual(result.error, ExecutionCode.CORRUPT.value)
        self.assertTrue(self.source.exists())

    @skipUnless(os.name == 'nt', 'Windows case-equivalent database ownership')
    def test_case_equivalent_database_owner_introduced_after_preview(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.db.execute('INSERT INTO files(filepath,size) VALUES(?,0)', (plan.target_path.upper(),))
        result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.FAILED)
        self.assertEqual(result.error, ExecutionCode.CONFLICT.value)
        self.assertTrue(self.source.exists())

    def test_invalid_current_settings_fail_as_stale_before_mutation(self):
        job = self.executor.create_job(self.plan())
        self.db.execute("UPDATE config SET value='invalid' WHERE key='issue_padding'")
        result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.FAILED)
        self.assertEqual(result.error, ExecutionCode.STALE.value)
        self.assertTrue(self.source.exists())

    def test_crash_after_move_before_receipt_recovers_without_second_move(self):
        self.register()
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.interrupt('after_effect', EffectKind.RELOCATE)
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], str(self.source))
        self.close_executors()
        self.executor = self.open_executor()
        with patch('backend.features.organization_execution.rename_no_replace', side_effect=AssertionError('duplicate move')):
            self.assertEqual(self.executor.reconcile_job(job).state, JobState.RECOVERY)
            self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], plan.target_path)

    def test_db_failure_after_move_retry_from_fresh_executor(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        def failure(stage, identity, ordinal):
            if stage == 'before_db_commit':
                raise sqlite3.OperationalError('injected database failure')
        self.executor.hook = failure
        result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.RECOVERY)
        self.assertTrue(Path(plan.target_path).exists())
        self.assertEqual(self.db.execute('SELECT * FROM files').fetchall(), [])
        self.close_executors()
        self.executor = self.open_executor()
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)

    def test_after_db_commit_crash_has_atomic_receipt(self):
        job = self.executor.create_job(self.plan())
        self.interrupt('after_db_commit', EffectKind.FILE_RECORD)
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        self.executor.hook = lambda *args: None
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)

    def test_duplicate_job_and_apply_are_idempotent(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.assertEqual(job, self.executor.create_job(plan))
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        with patch('backend.features.organization_execution.rename_no_replace', side_effect=AssertionError):
            self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(job, self.executor.create_job(plan))

    def test_stale_source_prevents_first_effect(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.source.write_bytes(b'changed source')
        result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.FAILED)
        self.assertTrue(all(s.state == StepState.PENDING for s in result.steps))

    def test_settings_changed_fails_without_replanning(self):
        job = self.executor.create_job(self.plan())
        self.db.execute("UPDATE config SET value=7 WHERE key='issue_padding'")
        result = self.executor.apply_job(job)
        self.assertEqual(result.error, ExecutionCode.STALE.value)
        self.assertTrue(self.source.exists())

    def test_target_occupied_after_preview_never_overwritten(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        Path(plan.target_path).write_bytes(b'precious unrelated target')
        result = self.executor.apply_job(job)
        self.assertEqual(result.error, ExecutionCode.OCCUPIED.value)
        self.assertEqual(Path(plan.target_path).read_bytes(), b'precious unrelated target')
        self.assertTrue(self.source.exists())

    def test_permission_failure_retry_does_not_repeat_completed_effects(self):
        job = self.executor.create_job(self.plan())
        with patch('backend.features.organization_execution.rename_no_replace', side_effect=PermissionError):
            self.assertEqual(self.executor.apply_job(job).error, ExecutionCode.PERMISSION.value)
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)

    def test_metadata_actual_writer_and_missing_receipt_recovery(self):
        plan = self.plan(metadata=True)
        job = self.executor.create_job(plan)
        self.interrupt('after_effect', EffectKind.COMICINFO)
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        self.assertEqual(inspect_comicinfo(plan.target_path).document.number, '1')
        self.close_executors()
        self.executor = self.open_executor()
        with patch('backend.features.organization_execution.write_comicinfo', side_effect=AssertionError('duplicate write')):
            self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertFalse(self.executor.preview_undo(job).eligible)
        self.assertNotIn('ComicInfo>', json.dumps(self.executor.history()))

    def test_directory_creation_and_safe_undo_cleanup(self):
        self.folder.rmdir()
        job = self.executor.create_job(self.plan())
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        undo = self.executor.create_undo_job(job, self.executor.preview_undo(job).intent_digest)
        self.assertEqual(self.executor.apply_job(undo).state, JobState.COMPLETED)
        self.assertFalse(self.folder.exists())
        self.assertTrue(self.library.exists())

    def test_unknown_directory_creation_receipt_never_claims_ownership(self):
        self.folder.rmdir()
        job = self.executor.create_job(self.plan())
        self.interrupt('after_started', EffectKind.DIRECTORY)
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        self.folder.mkdir()  # External/crash-ambiguous creation, no ownership proof.
        self.executor.hook = lambda *args: None
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        undo = self.executor.create_undo_job(job, self.executor.preview_undo(job).intent_digest)
        self.assertEqual(self.executor.apply_job(undo).state, JobState.COMPLETED)
        self.assertTrue(self.folder.exists())

    def test_undo_refuses_changed_file(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.executor.apply_job(job)
        Path(plan.target_path).write_bytes(b'later edit')
        self.assertFalse(self.executor.preview_undo(job).eligible)

    def test_undo_refuses_occupied_original_path(self):
        job = self.executor.create_job(self.plan())
        self.executor.apply_job(job)
        self.source.write_bytes(b'new arrival')
        self.assertFalse(self.executor.preview_undo(job).eligible)

    def test_undo_refuses_changed_database(self):
        job = self.executor.create_job(self.plan())
        self.executor.apply_job(job)
        self.db.execute('UPDATE issues_files SET forced=1')
        self.assertFalse(self.executor.preview_undo(job).eligible)

    def test_review_blocked_unresolved_not_authorized(self):
        for state in (PlanStatus.REVIEW, PlanStatus.BLOCKED, PlanStatus.UNRESOLVED):
            with self.assertRaises(OrganizationError):
                self.executor.create_job(replace(self.plan(), status=state))
        self.assertEqual(self.executor.history(), ())

    def test_double_execution_gate_is_cross_connection(self):
        job = self.executor.create_job(self.plan())
        second = self.open_executor()
        with execution_gate(self.executor.store.path), self.assertRaises(OrganizationError) as error:
            second.apply_job(job)
        self.assertEqual(error.exception.code, ExecutionCode.BUSY)
        program = '''
import sys
from backend.features.organization_execution import OrganizationExecutor
from backend.base.organization_job import ExecutionCode, OrganizationError
executor = OrganizationExecutor(sys.argv[1], (sys.argv[2],))
try:
    executor.apply_job(sys.argv[3])
except OrganizationError as error:
    sys.exit(19 if error.code == ExecutionCode.BUSY else 20)
finally:
    executor.close()
'''
        with execution_gate(self.executor.store.path):
            other_process = subprocess.run([sys.executable, '-c', program, self.dbpath, str(self.root), job],
                                           capture_output=True, timeout=30)
        self.assertEqual(other_process.returncode, 19)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.executor.store.get(job).state, JobState.PENDING)

    def test_conflicting_jobs_cannot_reserve_same_path(self):
        plan = self.plan()
        self.executor.create_job(plan)
        with self.assertRaises(OrganizationError) as error:
            self.executor.create_job(replace(plan, context_id='different authorization'))
        self.assertEqual(error.exception.code, ExecutionCode.BUSY)

    def test_unknown_executor_version_never_executes(self):
        job = self.executor.create_job(self.plan())
        self.db.execute("UPDATE organization_jobs SET executor_version='future'")
        with self.assertRaises(OrganizationError):
            self.executor.apply_job(job)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.executor.history()[0]['state'], 'unsupported_history')

    def test_corrupt_intent_never_executes(self):
        job = self.executor.create_job(self.plan())
        self.db.execute("UPDATE organization_jobs SET intent='{}'")
        with self.assertRaises(OrganizationError):
            self.executor.apply_job(job)
        self.assertTrue(self.source.exists())

    def test_no_changes_has_history_but_no_library_mutation(self):
        self.register()
        plan = self.plan(rename=False, move=False)
        self.assertEqual(plan.status, PlanStatus.NO_CHANGES)
        job = self.executor.create_job(plan)
        with patch('os.rename', side_effect=AssertionError), patch('os.mkdir', side_effect=AssertionError):
            self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(self.executor.store.get(job).steps, ())

    def test_phantom_db_success_rejected(self):
        self.register()
        job = self.executor.create_job(self.plan())
        self.db.execute('CREATE TRIGGER ignore_path_update BEFORE UPDATE ON files BEGIN SELECT RAISE(IGNORE); END')
        result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.RECOVERY)
        self.assertEqual(result.error, ExecutionCode.CONFLICT.value)

    def test_no_network_or_provider_requirement(self):
        job = self.executor.create_job(self.plan())
        with patch('socket.socket', side_effect=AssertionError('network')):
            self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)

    def test_failure_events_survive_successful_retry(self):
        job = self.executor.create_job(self.plan())
        with patch('backend.features.organization_execution.rename_no_replace', side_effect=PermissionError):
            self.executor.apply_job(job)
        self.executor.apply_job(job)
        events = [r[0] for r in self.db.execute('SELECT event FROM organization_events WHERE job_id=? ORDER BY id', (job,))]
        self.assertIn('recovery_required', events)
        self.assertEqual(events[-1], 'completed')

    def test_undo_approval_binds_exact_inverse(self):
        job = self.executor.create_job(self.plan())
        self.executor.apply_job(job)
        with self.assertRaises(OrganizationError):
            self.executor.create_undo_job(job, 'not-the-previewed-digest')

    def test_actual_process_exit_after_move_is_recoverable(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        program = '''
import os, sys
from backend.features.organization_execution import OrganizationExecutor
from backend.base.organization_plan import EffectKind
executor = OrganizationExecutor(sys.argv[1], (sys.argv[2],))
def interrupt(stage, job, ordinal):
    if stage == 'after_effect' and executor.store.get(job).steps[ordinal].kind == EffectKind.RELOCATE.value:
        os._exit(73)
executor.hook = interrupt
executor.apply_job(sys.argv[3])
'''
        result = subprocess.run([sys.executable, '-c', program, self.dbpath, str(self.root), job],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(self.executor.store.get(job).state, JobState.RUNNING)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.executor.inspect_incomplete()[0].state, JobState.RECOVERY)
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)

    def test_receipt_database_failure_after_move_does_not_report_success(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        original = self.executor.store.checkpoint
        def fail(identity, ordinal, state, evidence):
            if state == StepState.SUCCEEDED and self.executor.store.get(identity).steps[ordinal].kind == EffectKind.RELOCATE.value:
                raise sqlite3.OperationalError('receipt failure')
            return original(identity, ordinal, state, evidence)
        with patch.object(self.executor.store, 'checkpoint', side_effect=fail):
            result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.RECOVERY)
        self.assertEqual(result.error, ExecutionCode.RECEIPT.value)
        self.assertTrue(Path(plan.target_path).exists())
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)

    def test_zero_byte_target_is_not_vacant(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        Path(plan.target_path).touch()
        self.assertEqual(self.executor.apply_job(job).error, ExecutionCode.OCCUPIED.value)
        self.assertEqual(Path(plan.target_path).stat().st_size, 0)

    def test_target_appearing_immediately_before_move_not_overwritten(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        def interfere(stage, identity, ordinal):
            if stage == 'before_effect' and self.executor.store.get(identity).steps[ordinal].kind == EffectKind.RELOCATE.value:
                Path(plan.target_path).write_bytes(b'interference')
        self.executor.hook = interfere
        self.assertEqual(self.executor.apply_job(job).state, JobState.RECOVERY)
        self.assertEqual(Path(plan.target_path).read_bytes(), b'interference')
        self.assertTrue(self.source.exists())

    def test_changed_source_after_started_is_not_applied(self):
        job = self.executor.create_job(self.plan())
        self.interrupt('after_started', EffectKind.RELOCATE)
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        self.source.write_bytes(b'changed after start')
        self.executor.hook = lambda *args: None
        self.assertEqual(self.executor.apply_job(job).state, JobState.RECOVERY)
        self.assertTrue(self.source.exists())

    def test_changed_artifact_before_started_db_retry_stops(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.interrupt('after_started', EffectKind.FILE_RECORD)
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        Path(plan.target_path).write_bytes(b'external replacement')
        self.executor.hook = lambda *args: None
        self.assertEqual(self.executor.apply_job(job).state, JobState.RECOVERY)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)

    def test_metadata_changed_after_preview_is_stale(self):
        plan = self.plan(metadata=True)
        job = self.executor.create_job(plan)
        with ZipFile(self.source, 'a') as archive:
            archive.writestr('ComicInfo.xml', b'<ComicInfo><Notes>Later user edit</Notes></ComicInfo>')
        self.assertEqual(self.executor.apply_job(job).state, JobState.FAILED)
        self.assertTrue(self.source.exists())

    def test_metadata_success_then_db_failure_visible_and_recoverable(self):
        plan = self.plan(metadata=True)
        job = self.executor.create_job(plan)
        def fail(stage, identity, ordinal):
            if stage == 'before_db_commit':
                raise sqlite3.OperationalError('injected')
        self.executor.hook = fail
        self.assertEqual(self.executor.apply_job(job).state, JobState.RECOVERY)
        self.assertEqual(inspect_comicinfo(plan.target_path).document.title, 'Story')
        self.executor.hook = lambda *args: None
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)

    def test_job_owned_metadata_temp_never_overwrites_unexpected_file(self):
        plan = self.plan(metadata=True)
        job = self.executor.create_job(plan)
        unexpected = self.folder / ('.kapowarr-' + job + '.tmp')
        unexpected.write_bytes(b'unrelated file')
        self.assertEqual(self.executor.apply_job(job).state, JobState.RECOVERY)
        self.assertEqual(unexpected.read_bytes(), b'unrelated file')

    def test_multi_issue_forced_associations_and_undo(self):
        self.register(True)
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,202,'2',2)")
        self.db.execute('INSERT INTO issues_files VALUES(1,2,0)')
        job = self.executor.create_job(self.plan())
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT * FROM issues_files ORDER BY issue_id').fetchall(), [(1, 1, 1), (1, 2, 0)])
        undo = self.executor.create_undo_job(job, self.executor.preview_undo(job).intent_digest)
        self.assertEqual(self.executor.apply_job(undo).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT * FROM issues_files ORDER BY issue_id').fetchall(), [(1, 1, 1), (1, 2, 0)])

    def test_metron_authority_and_cross_references_remain_unchanged(self):
        self.db.execute("UPDATE volumes SET metadata_provider='metron' WHERE id=1")
        self.db.execute("INSERT INTO volume_external_ids(volume_id,provider,provider_id,provenance) VALUES(1,'metron','opaque','fixture')")
        self.db.execute("INSERT INTO issue_external_ids(issue_id,provider,provider_id,provenance) VALUES(1,'metron','opaque','fixture')")
        before = self.db.execute('SELECT * FROM volume_external_ids ORDER BY provider').fetchall()
        job = self.executor.create_job(self.plan(metadata=True))
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(before, self.db.execute('SELECT * FROM volume_external_ids ORDER BY provider').fetchall())
        self.assertEqual(self.db.execute('SELECT metadata_provider FROM volumes').fetchone()[0], 'metron')

    def test_general_binding_retained(self):
        self.register()
        self.db.execute("INSERT INTO volume_files VALUES(1,1,'metadata',1)")
        job = self.executor.create_job(self.plan())
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT * FROM volume_files').fetchall(), [(1, 1, 'metadata', 1)])

    def test_missing_volume_folder_recorded_and_restored(self):
        self.db.execute("UPDATE volumes SET folder='' WHERE id=1")
        job = self.executor.create_job(self.plan())
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT folder FROM volumes').fetchone()[0], str(self.folder))
        undo = self.executor.create_undo_job(job, self.executor.preview_undo(job).intent_digest)
        self.assertEqual(self.executor.apply_job(undo).state, JobState.COMPLETED)
        self.assertEqual(self.db.execute('SELECT folder FROM volumes').fetchone()[0], '')

    def test_undo_keeps_created_directory_when_unrelated_content_remains(self):
        self.folder.rmdir()
        job = self.executor.create_job(self.plan())
        self.executor.apply_job(job)
        extra = self.folder / 'user-note.txt'
        extra.write_bytes(b'do not delete')
        undo = self.executor.create_undo_job(job, self.executor.preview_undo(job).intent_digest)
        self.assertEqual(self.executor.apply_job(undo).state, JobState.COMPLETED)
        self.assertEqual(extra.read_bytes(), b'do not delete')

    def test_unknown_precondition_not_ignored(self):
        from backend.base.organization_plan import Precondition
        plan = self.plan()
        with self.assertRaises(OrganizationError):
            self.executor.create_job(replace(plan, preconditions=plan.preconditions + (Precondition('future', (), False),)))

    def test_corrupt_receipt_never_executes(self):
        job = self.executor.create_job(self.plan())
        self.db.execute("UPDATE organization_steps SET evidence='invalid'")
        with self.assertRaises(OrganizationError):
            self.executor.apply_job(job)
        self.assertTrue(self.source.exists())

    def test_scope_rejection_precedes_filesystem_inspection(self):
        outside = str(self.root.parent / 'outside-execution-scope' / 'file.cbz')
        with patch('backend.features.organization_execution.safe_path', side_effect=AssertionError('outside access')):
            with self.assertRaises(OrganizationError):
                self.executor._scope(outside)

    def test_symlink_ancestor_rejected(self):
        link = self.root / 'linked'
        try:
            link.symlink_to(self.incoming, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit unprivileged symlink creation')
        with self.assertRaises(OrganizationError) as error:
            self.executor._scope(str(link / self.source.name))
        self.assertEqual(error.exception.code, ExecutionCode.UNSAFE_PATH)

    def test_rejected_stale_job_cannot_be_reactivated_by_inspection(self):
        job = self.executor.create_job(self.plan())
        self.db.execute("UPDATE config SET value=8 WHERE key='issue_padding'")
        self.assertEqual(self.executor.apply_job(job).state, JobState.FAILED)
        self.db.execute("UPDATE config SET value=3 WHERE key='issue_padding'")
        self.assertEqual(self.executor.reconcile_job(job).state, JobState.FAILED)
        self.assertEqual(self.executor.apply_job(job).state, JobState.FAILED)

    def test_ten_independent_jobs_shared_folder_and_failure_isolation(self):
        jobs = []
        for number in range(1, 11):
            if number > 1:
                self.db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(?,1,?,?,?)',
                                (number, 200 + number, str(number), number))
        for number in range(1, 11):
            self.source = self.incoming / ('Batman %03d (2020).cbz' % number)
            if not self.source.exists():
                with ZipFile(self.source, 'w') as archive:
                    archive.writestr('page.jpg', b'page')
            jobs.append(self.executor.create_job(self.plan()))
        collision = self.executor.store.get(jobs[4]).target
        Path(collision).write_bytes(b'external')
        results = self.executor.apply_many(tuple(jobs))
        self.assertEqual(sum(j.state == JobState.COMPLETED for j in results), 9)
        self.assertEqual(results[4].state, JobState.FAILED)
        self.assertEqual(len(self.executor.history()), 10)

    def test_cross_filesystem_primitive_explicitly_blocks(self):
        from backend.implementations.organization_filesystem import \
            rename_no_replace
        target = str(self.folder / 'other.cbz')
        with patch('backend.implementations.organization_filesystem.os.stat',
                   side_effect=[SimpleNamespace(st_dev=1), SimpleNamespace(st_dev=2)]):
            with self.assertRaises(OrganizationError) as error:
                rename_no_replace(str(self.source), target)
        self.assertEqual(error.exception.code, ExecutionCode.UNSUPPORTED)
        self.assertTrue(self.source.exists())
        self.assertFalse(Path(target).exists())

    def test_disk_full_is_typed_and_preserves_source(self):
        job = self.executor.create_job(self.plan())
        with patch('backend.features.organization_execution.rename_no_replace', side_effect=OSError(errno.ENOSPC, 'injected')):
            result = self.executor.apply_job(job)
        self.assertEqual(result.error, ExecutionCode.DISK_FULL.value)
        self.assertTrue(self.source.exists())

    def test_comicinfo_disk_full_has_safe_error_category(self):
        from backend.base.comicinfo import ComicInfoCode, ComicInfoError
        plan = self.plan(metadata=True)
        job = self.executor.create_job(plan)
        original = self.source.read_bytes()
        with patch('backend.features.organization_execution.write_comicinfo',
                   side_effect=ComicInfoError(ComicInfoCode.WRITE_FAILED, os_error=errno.ENOSPC)):
            result = self.executor.apply_job(job)
        self.assertEqual(result.error, ExecutionCode.DISK_FULL.value)
        self.assertEqual(Path(plan.target_path).read_bytes(), original)

    def test_detailed_recovery_inspection_exposes_observed_state_not_xml(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        self.interrupt('after_effect', EffectKind.RELOCATE)
        with self.assertRaises(Interrupted):
            self.executor.apply_job(job)
        detail = self.executor.inspect_job(job)
        self.assertIsNone(detail['observations']['source'])
        self.assertEqual(detail['observations']['target']['sha256'], detail['steps'][0]['evidence']['artifact_before']['sha256'])
        self.assertNotIn('ComicInfo>', json.dumps(detail))

    def test_db_only_association_then_inverse(self):
        plan = self.plan(rename=False, move=False)
        self.assertNotIn(EffectKind.RELOCATE, [e.kind for e in plan.effects])
        job = self.executor.create_job(plan)
        original = self.source.read_bytes()
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        inverse = self.executor.create_undo_job(job, self.executor.preview_undo(job).intent_digest)
        self.assertEqual(self.executor.apply_job(inverse).state, JobState.COMPLETED)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.db.execute('SELECT * FROM files').fetchall(), [])

    def test_metadata_only_plan_uses_same_archive_location(self):
        self.register()
        plan = self.plan(metadata=True, rename=False, move=False)
        self.assertEqual(plan.source_path, plan.target_path)
        job = self.executor.create_job(plan)
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(inspect_comicinfo(str(self.source)).document.title, 'Story')

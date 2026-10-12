"""Bounded import copy, exclusive publication and interruption evidence."""
import ctypes
import errno
import json
import os
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

from Tbackend.features import organization_execution as fixture

from backend.base.identification import (MatchContribution, MatchReason,
                                         PublicationAuthority)
from backend.base.organization_job import (ExecutionCode, JobState,
                                           OrganizationError, StepState)
from backend.base.organization_plan import EffectKind
from backend.features.organization_import_move import (execute, reconcile,
                                                       requires_copy)
from backend.implementations.organization_filesystem import (artifact,
                                                             rename_no_replace)


class ImportTransferTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.cbz'
        self.target = self.root/'target.cbz'
        self.source.write_bytes(b'unchanged payload' * 150000)
        self.intent = dict(source=str(self.source), target=str(self.target))
        self.value = dict(artifact_before=artifact(str(self.source)))
        self.executor = Mock()
        self.executor.store.transaction.side_effect = nullcontext
        self.job = SimpleNamespace(id='synthetic')

    def run_copy(self):
        execute(self.executor, self.job, self.intent, 0, self.value)

    def test_linux_unsupported_rename_errno_is_preserved_for_review(self):
        class UnsupportedRename:
            def __call__(self, *_args):
                ctypes.set_errno(errno.ENOTSUP)
                return -1
        class PosixCalls:
            name = 'posix'
            def __getattr__(self, name):
                return getattr(os, name)
        with patch('backend.implementations.organization_filesystem.os', PosixCalls()), \
             patch('backend.implementations.organization_filesystem.sys.platform', 'linux'), \
             patch('backend.implementations.organization_filesystem.ctypes.CDLL',
                   return_value=SimpleNamespace(renameat2=UnsupportedRename())):
            with self.assertRaises(OrganizationError) as error:
                rename_no_replace(str(self.source), str(self.target))
        self.assertEqual(error.exception.code, ExecutionCode.UNSUPPORTED)
        self.assertIn(f'errno={errno.ENOTSUP}', error.exception.detail)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.target.exists())

    def test_success_shared_inode_is_not_rewritten(self):
        seed = self.root/'seed.cbz'
        os.link(self.source, seed)
        self.run_copy()
        self.assertFalse(self.source.exists())
        self.assertEqual(seed.read_bytes(), self.target.read_bytes())
        self.assertNotEqual(seed.stat().st_ino, self.target.stat().st_ino)
        self.assertTrue(reconcile(self.intent, self.value))
        self.executor._check_database.assert_called_once()

    def test_collision_preserves_both(self):
        self.target.write_bytes(b'unrelated')
        with self.assertRaises(OrganizationError): self.run_copy()
        self.assertTrue(self.source.exists())
        self.assertEqual(self.target.read_bytes(), b'unrelated')

    def test_verified_copy_interruption_is_observational_then_resumable(self):
        self.executor.hook.side_effect = RuntimeError('interruption')
        with self.assertRaises(RuntimeError): self.run_copy()
        self.assertFalse(reconcile(self.intent, self.value))
        self.assertTrue(self.source.exists())
        inode = self.target.stat().st_ino
        self.executor.hook.side_effect = None
        self.run_copy()
        self.assertEqual(self.target.stat().st_ino, inode)
        self.assertTrue(reconcile(self.intent, self.value))

    def test_disk_full_retains_source_and_owned_partial_for_review(self):
        real_open = open
        def opened(path, mode):
            if mode != 'xb': return real_open(path, mode)
            output = real_open(path, mode)
            wrapper = Mock(wraps=output)
            wrapper.write.side_effect = OSError(errno.ENOSPC, 'fixture full')
            return SimpleWriter(wrapper, output)
        with patch('backend.features.organization_import_move.open', opened):
            with self.assertRaises(OSError): self.run_copy()
        self.assertTrue(self.source.exists())
        with self.assertRaises(OrganizationError): reconcile(self.intent, self.value)
        self.target.unlink()  # handle closed even on write failure

    def test_authority_change_preserves_authoritative_source(self):
        self.executor._check_database.side_effect = OrganizationError(ExecutionCode.CONFLICT)
        with self.assertRaises(OrganizationError): self.run_copy()
        self.assertTrue(self.source.exists())
        self.assertEqual(self.source.read_bytes(), self.target.read_bytes())

    @skipUnless(sys.platform.startswith('linux') and Path('/dev/shm').is_dir(), 'Linux independent tmpfs fixture')
    def test_actual_cross_filesystem_transfer(self):
        with TemporaryDirectory(dir='/dev/shm',prefix='pullarr-import-') as temporary:
            other=Path(temporary)/'source.cbz';other.write_bytes(self.source.read_bytes())
            if not requires_copy(str(other),str(self.root)):self.skipTest('Fixture filesystems share a device')
            self.intent['source']=str(other)
            self.value['artifact_before']=artifact(str(other))
            self.run_copy()
            self.assertFalse(other.exists())
            self.assertEqual(self.target.read_bytes(),self.source.read_bytes())

    def test_source_change_after_copy_is_not_retired(self):
        def changed(stage,*args):
            if stage=='after_import_copy':self.source.write_bytes(b'changed authority')
        self.executor.hook.side_effect=changed
        with self.assertRaises(OrganizationError):self.run_copy()
        self.assertEqual(self.source.read_bytes(),b'changed authority')
        self.assertNotEqual(self.target.read_bytes(),self.source.read_bytes())


class SimpleWriter:
    def __init__(self, wrapped, output): self.wrapped, self.output = wrapped, output
    def __enter__(self): return self.wrapped
    def __exit__(self, *args): self.output.close()


class ImportJournalTests(TestCase):
    setUp=fixture.ExecutionTests.setUp
    close_executors=fixture.ExecutionTests.close_executors
    open_executor=fixture.ExecutionTests.open_executor
    plan=fixture.ExecutionTests.plan

    def selected_plan(self, *, same_folder=False):
        self.db.execute('UPDATE issues SET comicvine_id=1068064 WHERE id=1')
        if same_folder:
            source = self.folder/'Batman 001 (2020) original.cbz'
            source.write_bytes(self.source.read_bytes())
            self.source = source
        plan = self.plan(rename=same_folder)
        selected = replace(plan.identification.selected, contributions=(
            MatchContribution(MatchReason.FORCED, PublicationAuthority.IMPORT_SELECTION.value),))
        return replace(plan, identification=replace(plan.identification, selected=selected))

    def assert_imported(self, job, plan, original):
        self.assertEqual(self.executor.store.get(job).state, JobState.COMPLETED)
        self.assertFalse(self.source.exists())
        self.assertEqual(Path(plan.target_path).read_bytes(), original)
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchall(), [(plan.target_path,)])
        self.assertEqual(self.db.execute('SELECT issue_id FROM issues_files').fetchall(), [(1,)])
        self.assertEqual(self.db.execute('''SELECT i.comicvine_id FROM issues i
            JOIN issues_files b ON b.issue_id=i.id''').fetchall(), [(1068064,)])

    def check_unsupported_move(self, *, same_folder):
        plan = self.selected_plan(same_folder=same_folder)
        self.assertEqual(Path(plan.source_path).parent == Path(plan.target_path).parent, same_folder)
        self.assertFalse(requires_copy(plan.source_path, plan.target_folder))
        original = self.source.read_bytes()
        job = self.executor.create_job(plan)
        with patch('backend.features.organization_execution.rename_no_replace',
                   side_effect=OrganizationError(ExecutionCode.UNSUPPORTED)):
            result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.COMPLETED, result.error)
        move = next(step for step in result.steps if step.kind == EffectKind.RELOCATE.value)
        self.assertEqual(json.loads(move.evidence)['artifact_before']['sha256'], artifact(plan.target_path)['sha256'])
        self.assertIn('import_copy_identity', json.loads(move.evidence))
        self.assert_imported(job, plan, original)
        self.assertEqual(self.executor.apply_job(job).state, JobState.COMPLETED)

    def test_unsupported_exclusive_rename_uses_journalled_copy_across_directories(self):
        self.check_unsupported_move(same_folder=False)

    def test_unsupported_exclusive_rename_uses_journalled_copy_in_same_directory(self):
        self.check_unsupported_move(same_folder=True)

    def test_unsupported_rename_is_explicitly_recoverable(self):
        from backend.features.maintenance_recovery import MaintenanceRecovery

        plan = self.selected_plan(same_folder=True)
        original = self.source.read_bytes()
        job = self.executor.create_job(plan)
        def old_failure(stage, identifier, ordinal):
            if stage == 'before_effect' and self.executor.store.get(identifier).steps[ordinal].kind == EffectKind.RELOCATE.value:
                raise OrganizationError(ExecutionCode.UNSUPPORTED)
        self.executor.hook = old_failure
        failed = self.executor.apply_job(job)
        self.executor.hook = lambda *_: None
        self.assertEqual(failed.state, JobState.RECOVERY)
        self.assertEqual(failed.steps[0].state, StepState.STARTED)
        self.assertTrue(self.source.exists())
        self.assertFalse(Path(plan.target_path).exists())
        service = MaintenanceRecovery(self.dbpath, (str(self.root),))
        review = service.preview_recovery('organization', job)
        self.assertTrue(review['eligible'], review)
        with patch('backend.features.organization_execution.rename_no_replace',
                   side_effect=OrganizationError(ExecutionCode.UNSUPPORTED)):
            service.recover(job, review['digest'], confirmed=True)
        self.assert_imported(job, plan, original)
        self.assertEqual(service.recover(job, review['digest'], confirmed=True)['state'], 'complete')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)

    def test_unsupported_rename_copy_interruption_has_reviewed_recovery(self):
        from backend.features.maintenance_recovery import MaintenanceRecovery

        plan = self.selected_plan()
        original = self.source.read_bytes()
        job = self.executor.create_job(plan)
        def interrupt(stage, identifier, ordinal):
            if stage == 'after_import_copy':
                raise fixture.Interrupted()
        self.executor.hook = interrupt
        with patch('backend.features.organization_execution.rename_no_replace',
                   side_effect=OrganizationError(ExecutionCode.UNSUPPORTED)):
            with self.assertRaises(fixture.Interrupted):
                self.executor.apply_job(job)
        self.assertTrue(self.source.exists())
        self.assertEqual(Path(plan.target_path).read_bytes(), original)
        self.executor.hook = lambda *_: None
        service = MaintenanceRecovery(self.dbpath, (str(self.root),))
        review = service.preview_recovery('organization', job)
        self.assertTrue(review['eligible'], review)
        service.recover(job, review['digest'], confirmed=True)
        self.assert_imported(job, plan, original)

    def test_unsupported_rename_after_source_retirement_reconciles_exact_job(self):
        from backend.features.maintenance_recovery import MaintenanceRecovery

        plan = self.selected_plan()
        original = self.source.read_bytes()
        job = self.executor.create_job(plan)
        def interrupt(stage, identifier, ordinal):
            if stage == 'after_effect' and self.executor.store.get(identifier).steps[ordinal].kind == EffectKind.RELOCATE.value:
                raise fixture.Interrupted()
        self.executor.hook = interrupt
        with patch('backend.features.organization_execution.rename_no_replace',
                   side_effect=OrganizationError(ExecutionCode.UNSUPPORTED)):
            with self.assertRaises(fixture.Interrupted):
                self.executor.apply_job(job)
        self.assertFalse(self.source.exists())
        self.assertEqual(Path(plan.target_path).read_bytes(), original)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        self.executor.hook = lambda *_: None
        service = MaintenanceRecovery(self.dbpath, (str(self.root),))
        review = service.preview_recovery('organization', job)
        self.assertTrue(review['eligible'], review)
        self.assertEqual(review['steps'][0]['action'], 'acknowledge_recorded_effect')
        service.recover(job, review['digest'], confirmed=True)
        self.assert_imported(job, plan, original)

    def test_unsupported_rename_cannot_overwrite_destination_created_during_attempt(self):
        plan = self.selected_plan()
        job = self.executor.create_job(plan)
        def occupied(source, target):
            Path(target).write_bytes(b'unrelated destination')
            raise OrganizationError(ExecutionCode.UNSUPPORTED)
        with patch('backend.features.organization_execution.rename_no_replace', side_effect=occupied):
            result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.RECOVERY)
        self.assertTrue(self.source.exists())
        self.assertEqual(Path(plan.target_path).read_bytes(), b'unrelated destination')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        self.assertFalse(self.executor.preview_recovery(job)['eligible'])

    def test_unsupported_rename_rejects_destination_symlink(self):
        plan = self.selected_plan()
        job = self.executor.create_job(plan)
        external = self.root/'unrelated.cbz'
        external.write_bytes(b'unrelated bytes')
        def symlinked(source, target):
            try:
                Path(target).symlink_to(external)
            except OSError:
                self.skipTest('Host does not permit unprivileged symlink creation')
            raise OrganizationError(ExecutionCode.UNSUPPORTED)
        with patch('backend.features.organization_execution.rename_no_replace', side_effect=symlinked):
            result = self.executor.apply_job(job)
        self.assertEqual(result.error, ExecutionCode.UNSAFE_PATH.value)
        self.assertTrue(self.source.exists())
        self.assertEqual(external.read_bytes(), b'unrelated bytes')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)

    def test_unsupported_rename_copy_failure_retains_source_and_issue_unowned(self):
        plan = self.selected_plan()
        job = self.executor.create_job(plan)
        original = self.source.read_bytes()
        real_open = open
        def opened(path, mode):
            if mode != 'xb':
                return real_open(path, mode)
            output = real_open(path, mode)
            wrapper = Mock(wraps=output)
            wrapper.write.side_effect = OSError(errno.ENOSPC, 'fixture full')
            return SimpleWriter(wrapper, output)
        with patch('backend.features.organization_execution.rename_no_replace',
                   side_effect=OrganizationError(ExecutionCode.UNSUPPORTED)), \
             patch('backend.features.organization_import_move.open', opened):
            result = self.executor.apply_job(job)
        self.assertEqual(result.error, ExecutionCode.DISK_FULL.value)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 0)
        self.assertFalse(self.executor.preview_recovery(job)['eligible'])

    def test_unsupported_rename_preserves_other_hardlink_bytes(self):
        seed = self.root/'seed.cbz'
        os.link(self.source, seed)
        self.check_unsupported_move(same_folder=False)
        self.assertTrue(seed.exists())
        self.assertNotEqual(seed.stat().st_ino, Path(self.folder/'Batman 001 (2020).cbz').stat().st_ino)

    def test_unmanaged_rename_keeps_global_unsupported_error(self):
        plan = self.plan()
        job = self.executor.create_job(plan)
        with patch('backend.features.organization_execution.rename_no_replace',
                   side_effect=OrganizationError(ExecutionCode.UNSUPPORTED)):
            result = self.executor.apply_job(job)
        self.assertEqual(result.error, ExecutionCode.UNSUPPORTED.value)
        self.assertTrue(self.source.exists())
        self.assertFalse(Path(plan.target_path).exists())

    def test_already_canonical_import_registers_without_relocation(self):
        source = self.folder/'Batman 001 (2020).cbz'
        source.write_bytes(self.source.read_bytes())
        self.source = source
        plan = self.plan(rename=False)
        selected = replace(plan.identification.selected, contributions=(
            MatchContribution(MatchReason.FORCED, PublicationAuthority.IMPORT_SELECTION.value),))
        plan = replace(plan, identification=replace(plan.identification, selected=selected))
        self.assertEqual(plan.source_path, plan.target_path)
        self.assertNotIn(EffectKind.RELOCATE, [effect.kind for effect in plan.effects])
        job = self.executor.create_job(plan)
        with patch('backend.features.organization_execution.rename_no_replace',
                   side_effect=AssertionError('unexpected move')):
            result = self.executor.apply_job(job)
        self.assertEqual(result.state, JobState.COMPLETED, result.error)
        self.assertTrue(source.exists())
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchall(), [(str(source),)])
        self.assertEqual(self.db.execute('SELECT issue_id FROM issues_files').fetchall(), [(1,)])

    def test_copy_interruptions_recover_original_journal(self):
        for stage in ('after_import_copy','after_effect'):
            with self.subTest(stage=stage):
                # Recreate an independent file and use a unique target per case.
                source=self.incoming/f'Batman 001 (2020) {stage}.cbz'
                source.write_bytes(self.source.read_bytes())
                original_source=self.source;self.source=source
                plan=self.plan(rename=False)
                selected=replace(plan.identification.selected, contributions=(MatchContribution(MatchReason.FORCED,PublicationAuthority.IMPORT_SELECTION.value),))
                plan=replace(plan,identification=replace(plan.identification,selected=selected))
                job=self.executor.create_job(plan)
                # Effect names are versioned domain values, not UI labels.
                from backend.base.organization_plan import EffectKind
                def hook(event, job_id, ordinal):
                    if event==stage and self.executor.store.get(job_id).steps[ordinal].kind==EffectKind.RELOCATE.value:
                        raise fixture.Interrupted()
                self.executor.hook=hook
                with patch('backend.features.organization_import_move.requires_copy',return_value=True):
                    with self.assertRaises(fixture.Interrupted):self.executor.apply_job(job)
                    self.executor.hook=lambda *args:None
                    result=self.executor.apply_job(job)
                self.assertEqual(result.state.value,'completed',result.error)
                self.assertFalse(source.exists())
                self.assertEqual(Path(plan.target_path).read_bytes(),original_source.read_bytes())
                self.assertEqual(self.executor.apply_job(job).state.value,'completed')
                self.source=original_source

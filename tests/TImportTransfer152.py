"""Bounded import copy, exclusive publication and interruption evidence."""
import errno
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
from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.features.organization_import_move import (execute, reconcile,
                                                       requires_copy)
from backend.implementations.organization_filesystem import artifact


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

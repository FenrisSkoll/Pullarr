"""Single-operation 8H coordination over real domain journals and fixtures."""

from unittest import TestCase
from unittest.mock import patch

import TBulkFolder as folder_fixture
import TBulkRename as rename_fixture
import TComicInfoRepair as comicinfo_fixture
import TQuarantineExecution as quarantine_fixture

from backend.base.maintenance_history import HistoryError
from backend.base.organization_job import JobState, OrganizationError
from backend.features.maintenance_recovery import MaintenanceRecovery
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.organization_filesystem import execution_gate


class MaintenanceRecoveryTests(TestCase):
    def quarantine(self):
        case = quarantine_fixture.QuarantineExecutionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.register_fixture()
        service = MaintenanceRecovery(str(case.health.database), (str(case.health.root),))
        return case, service

    def test_target_only_quarantine_preview_is_read_only_then_recovers_and_retries(self):
        case, service = self.quarantine()
        case.interrupt('after_effect', 0)
        before = tuple(case.db.iterdump()), case.target.read_bytes()
        review = service.preview_recovery('organization', case.job)
        self.assertTrue(review['eligible'], review)
        self.assertEqual(review['steps'][0]['action'], 'acknowledge_recorded_effect')
        self.assertEqual(before, (tuple(case.db.iterdump()), case.target.read_bytes()))
        with self.assertRaises(HistoryError):
            service.recover(case.job, review['digest'], confirmed=False)
        result = service.recover(case.job, review['digest'], confirmed=True)
        self.assertEqual(result['state'], 'complete')
        case.assert_inactive()
        before_retry = tuple(case.db.iterdump())
        restarted = MaintenanceRecovery(service.database, service.allowed_roots)
        self.assertEqual(restarted.recover(case.job, review['digest'], confirmed=True)['state'], 'complete')
        self.assertEqual(before_retry, tuple(case.db.iterdump()))
        with self.assertRaises(OrganizationError):
            restarted.recover(case.job, '0' * 64, confirmed=True)

    def test_source_only_does_not_write_validation_event_until_confirmation(self):
        case, service = self.quarantine()
        before = tuple(case.db.iterdump())
        review = service.preview_recovery('organization', case.job)
        self.assertTrue(review['eligible'], review)
        self.assertEqual(before, tuple(case.db.iterdump()))
        self.assertFalse(case.target.parent.exists())
        self.assertEqual(service.recover(case.job, review['digest'], confirmed=True)['state'], 'complete')

    def test_ambiguous_missing_changed_and_reservation_conflicts_are_not_forceable(self):
        for scenario in ('both', 'neither', 'mismatch', 'reservation'):
            with self.subTest(scenario=scenario):
                case, service = self.quarantine()
                case.interrupt('after_effect', 0)
                if scenario == 'both':
                    case.source.write_bytes(case.target.read_bytes())
                elif scenario == 'neither':
                    case.target.unlink()  # Disposable test artifact, explicit missing-path fixture.
                elif scenario == 'mismatch':
                    case.target.write_bytes(b'changed')
                else:
                    case.db.execute('DELETE FROM organization_reservations WHERE job_id=?', (case.job,))
                    case.db.commit()
                before = tuple(case.db.iterdump())
                result = service.preview_recovery('organization', case.job)
                self.assertFalse(result['eligible'], result)
                self.assertEqual(before, tuple(case.db.iterdump()))
                with self.assertRaises(OrganizationError):
                    service.recover(case.job, result['digest'], confirmed=True)

    def test_quarantine_restore_is_fresh_conditional_and_exact_registration_retry(self):
        case, service = self.quarantine()
        case.executor.apply_job(case.job)
        before = tuple(case.db.iterdump())
        preview = service.preview_inverse('organization', case.job)
        self.assertTrue(preview['eligible'], preview)
        self.assertIsNone(preview['current_path'])
        self.assertEqual(before, tuple(case.db.iterdump()))
        inverse = service.create_inverse(case.job, preview['digest'], confirmed=True)
        self.assertEqual(inverse['operation'], 'duplicate_restore')
        self.assertEqual(service.create_inverse(case.job, preview['digest'], confirmed=True)['id'], inverse['id'])
        with self.assertRaises(OrganizationError):
            service.create_inverse(case.job, '0' * 64, confirmed=True)
        self.assertEqual(case.executor.apply_job(inverse['id']).state, JobState.COMPLETED)
        self.assertEqual(service.history.get('organization', case.job)['relationship_state'], 'reverted')

    def test_inverse_eligibility_changes_with_artifact_and_destination(self):
        for what in ('artifact', 'destination', 'authority'):
            with self.subTest(what=what):
                case, service = self.quarantine()
                case.executor.apply_job(case.job)
                initial = service.preview_inverse('organization', case.job)
                self.assertTrue(initial['eligible'])
                if what == 'artifact':
                    case.target.write_bytes(b'changed')
                elif what == 'destination':
                    case.source.write_bytes(b'occupied')
                else:
                    case.db.execute('UPDATE volumes SET authority_generation=authority_generation+1')
                    case.db.commit()
                self.assertFalse(service.preview_inverse('organization', case.job)['eligible'])
                with self.assertRaises(OrganizationError):
                    service.create_inverse(case.job, initial['digest'], confirmed=True)

    def test_live_worker_gate_blocks_operator_recovery_not_history(self):
        case, service = self.quarantine()
        with execution_gate(str(case.health.database)):
            self.assertEqual(service.history.get('organization', case.job)['state'], 'pending')
            result = service.preview_recovery('organization', case.job)
            self.assertFalse(result['eligible'])
            self.assertIn('executor_or_path_claimed', result['reasons'])

    def test_completed_job_has_no_forward_recovery_action(self):
        case, service = self.quarantine()
        case.executor.apply_job(case.job)
        result = service.preview_recovery('organization', case.job)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reasons'], ['already_completed'])

    def test_rename_and_folder_recovery_and_inverse_delegate_exact_intent(self):
        for fixture in (rename_fixture.BulkRenameTests, folder_fixture.BulkFolderTests):
            with self.subTest(fixture=fixture.__name__):
                case = fixture()
                case.setUp()
                self.addCleanup(case.doCleanups)
                session = case.review()
                registered = case.register(session)
                database = str(case.fixture.fixture.database)
                roots = (str(case.fixture.fixture.root),)
                executor = OrganizationExecutor(database, roots)
                self.addCleanup(executor.close)
                job = case.db.execute('SELECT id FROM organization_jobs WHERE batch_id=?', (registered['batch_id'],)).fetchone()[0]
                def interrupt(stage, identifier, ordinal):
                    if stage == 'after_effect' and ordinal == 0:
                        raise SystemExit('fixture interruption')
                executor.hook = interrupt
                with self.assertRaises(SystemExit):
                    executor.apply_job(job)
                executor.hook = lambda *_: None
                service = MaintenanceRecovery(database, roots)
                before = tuple(case.db.iterdump())
                preview = service.preview_recovery('organization', job)
                self.assertTrue(preview['eligible'], preview)
                self.assertEqual(before, tuple(case.db.iterdump()))
                self.assertEqual(service.recover(job, preview['digest'], confirmed=True)['state'], 'complete')
                inverse = service.preview_inverse('organization', job)
                self.assertTrue(inverse['eligible'], inverse)
                created = service.create_inverse(job, inverse['digest'], confirmed=True)
                self.assertEqual(service.create_inverse(job, inverse['digest'], confirmed=True)['id'], created['id'])
                self.assertEqual(executor.apply_job(created['id']).state, JobState.COMPLETED)
                self.assertTrue(case.source.exists())

    def test_comicinfo_recovery_without_fake_lossless_inverse(self):
        case = comicinfo_fixture.ComicInfoRepairTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        def interrupt(stage, job, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                raise PermissionError('fixture interruption')
        case.service.checkpoint = interrupt
        result = case.apply(case.review())
        service = MaintenanceRecovery(str(case.fixture.fixture.database), (str(case.fixture.fixture.root),))
        preview = service.preview_recovery('organization', result['job_id'])
        self.assertTrue(preview['eligible'], preview)
        self.assertEqual(service.recover(result['job_id'], preview['digest'], confirmed=True)['state'], 'complete')
        inverse = service.preview_inverse('organization', result['job_id'])
        self.assertFalse(inverse['eligible'])
        self.assertIn('lossless_metadata_undo_not_supported', inverse['reasons'])

    def test_confirmation_response_loss_before_execution_can_get_fresh_review(self):
        case, service = self.quarantine()
        initial = service.preview_recovery('organization', case.job)
        with case.executor.store.transaction():
            case.executor.store.event(case.job, 'maintenance_recovery_confirmed',
                dict(version='recovery-review/v1', digest=initial['digest']))
        # Simulate process loss after durable confirmation, before mutation.
        self.assertEqual(service.recover(case.job, initial['digest'], confirmed=True)['state'], 'pending')
        fresh = service.preview_recovery('organization', case.job)
        self.assertNotEqual(fresh['digest'], initial['digest'])
        self.assertEqual(service.recover(case.job, fresh['digest'], confirmed=True)['state'], 'complete')

    def test_task_adapter_recovery_and_inverse_exact_retry(self):
        from backend.features.maintenance_history_tasks import \
            MaintenanceHistoryTask
        case, service = self.quarantine()
        case.interrupt('after_effect', 0)
        preview = service.preview_recovery('organization', case.job)
        task = MaintenanceHistoryTask(service, case.job, preview['digest'], operation='recovery', confirmed=True)
        self.assertIsNone(task.run())
        self.assertEqual(task.result['state'], 'complete')
        inverse = service.preview_inverse('organization', case.job)
        task = MaintenanceHistoryTask(service, case.job, inverse['digest'], operation='inverse', confirmed=True)
        task.run()
        self.assertEqual(task.result['state'], 'complete')
        before = tuple(case.db.iterdump())
        task.run()
        self.assertEqual(before, tuple(case.db.iterdump()))

    def test_fresh_preview_after_membership_change_and_blocked_stale_confirmation(self):
        case = folder_fixture.BulkFolderTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        registered = case.register(case.review())
        case.service.execute(case.db.cursor(), registered['batch_id'])
        job = case.db.execute('SELECT id FROM organization_jobs').fetchone()[0]
        service = MaintenanceRecovery(str(case.fixture.fixture.database), (str(case.fixture.fixture.root),))
        initial = service.preview_inverse('organization', job)
        self.assertTrue(initial['eligible'])
        from pathlib import Path
        target = Path(case.db.execute('SELECT folder FROM volumes WHERE id=1').fetchone()[0])
        (target / 'new.txt').write_text('later independent file')
        self.assertFalse(service.preview_inverse('organization', job)['eligible'])
        with self.assertRaises(OrganizationError):
            service.create_inverse(job, initial['digest'], confirmed=True)

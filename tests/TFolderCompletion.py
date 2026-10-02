"""Final folder-domain and legacy scan exclusion acceptance, disposable only."""

import json
import os
import sqlite3
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TBulkFolder as folder_fixture

from backend.base.bulk_folder import FolderReviewError
from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.features.bulk_folder import BulkFolderReviews
from backend.features.wanted_status import wanted_rows
from backend.implementations.file_matching import _scan_files, scan_files
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.provider_authority import capture
from backend.internals.scan_mutation import scan_mutation


class FolderCompletionTests(TestCase):
    def setUp(self):
        self.case = folder_fixture.BulkFolderTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.db = self.case.db

    @contextmanager
    def scanner(self):
        with ExitStack() as stack:
            for module in ('backend.implementations.file_matching', 'backend.internals.db_models'):
                stack.enter_context(patch(module + '.get_db', side_effect=self.db.cursor))
            settings = stack.enter_context(patch('backend.implementations.file_matching.Settings'))
            settings.return_value.get_settings.return_value = SimpleNamespace(
                create_empty_volume_folders=True, delete_empty_folders=True, unmonitor_deleted_issues=True)
            volume = stack.enter_context(patch('backend.implementations.volumes.Volume'))
            volume.return_value.get_data.side_effect = lambda: SimpleNamespace(
                folder=self.db.execute('SELECT folder FROM volumes WHERE id=1').fetchone()[0], root_folder=1)
            volume.return_value.get_issues.return_value = [SimpleNamespace(id=1, calculated_issue_number=1, date='2020-01-01')]
            volume.return_value.get_all_files.side_effect = lambda: [dict(id=r[0], filepath=r[1]) for r in
                self.db.execute('SELECT id,filepath FROM files')]
            volume.return_value.get_general_files.side_effect = lambda: [dict(id=r[0], file_type=r[1]) for r in
                self.db.execute('SELECT file_id,file_type FROM volume_files WHERE volume_id=1')]
            yield

    def prepare_links(self):
        cover = self.case.source.parent / 'cover.jpg'
        cover.write_bytes(b'cover')
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,5)', (str(cover),))
        self.db.execute("INSERT INTO volume_files(file_id,volume_id,file_type,forced) VALUES(2,1,'cover',1)")
        self.db.execute('UPDATE issues_files SET forced=1')
        self.db.commit()
        self.case.refresh_worklist()

    def test_actual_scan_cannot_recreate_source_or_change_links_during_recovery(self):
        self.prepare_links()
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(2,1,102,'2',2,1)")
        reference = PublicationRef('comicvine', '102')
        cursor = self.db.cursor()
        preview = claim_preview(cursor, 1, reference, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(cursor, 1, reference, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(cursor, 1, 1, [claim])
        apply_coverage(cursor, 1, 1, [claim], preview['preview_token'])
        self.db.commit()
        self.case.refresh_worklist()
        def wanted():
            self.db.row_factory = sqlite3.Row
            try:
                return {r['id']: (r['owned'], r['wanted']) for r in wanted_rows(SimpleNamespace(db=self.db, clock=lambda: 0))}
            finally:
                self.db.row_factory = None
        ownership = wanted()
        session = self.case.review()
        registered = self.case.register(session)
        token = capture(self.db.cursor(), (1,))[1]
        domains = ('issues_files', 'volume_files', 'issues', 'bibliographic_content_claims', 'file_content_coverage')
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in domains}
        with self.scanner():
            # Registered/pending excludes both manual and token-bearing refresh scans.
            with self.assertRaises(OrganizationError):
                scan_files(1)
            self.assertEqual(scan_files(1, expected_authority=token), 'deferred_organization_reservation')
            def interrupt(stage, identity, ordinal):
                if stage == 'after_effect' and ordinal == 0:
                    with self.assertRaises(OrganizationError):
                        scan_files(1)
                    self.assertEqual(scan_files(1, expected_authority=token), 'deferred_organization_reservation')
                    self.assertFalse(self.case.source.parent.exists())
                    raise PermissionError('pause before path reconciliation')
            self.case.service.checkpoint = interrupt
            result = self.case.service.execute(self.db.cursor(), registered['batch_id'])
            self.assertEqual(result['items'][0]['state'], 'recovery_required')
            with self.assertRaises(OrganizationError):
                scan_files(1)
            self.assertFalse(self.case.source.parent.exists())
            self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in domains})
            self.assertEqual(ownership, wanted())
            self.case.service.checkpoint = None
            self.assertEqual(self.case.service.execute(self.db.cursor(), registered['batch_id'])['state'], 'completed')
            self.assertEqual(scan_files(1), 'completed')
            self.assertEqual(scan_files(1, expected_authority=token), 'completed')
            self.assertFalse(self.case.source.parent.exists())
            self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in domains})
            self.assertEqual(ownership, wanted())

    def test_actual_switch_and_aba_stale_folder_review(self):
        from TProviderSwitchApply import SwitchApplyTests, remote

        from backend.features.provider_switch_review import \
            ProviderSwitchReviews
        switching = SwitchApplyTests()
        switching.db = self.db
        switching.cursor = self.db.cursor()
        switching.service = ProviderSwitchReviews(task_observer=lambda _: ())
        review = self.case.review()
        switching.apply(switching.review(remote('metron', count=1)))
        with self.assertRaises(FolderReviewError):
            self.case.register(review)
        switching.apply(switching.review(remote('comicvine', parent='100', first=101, count=1)))
        self.assertEqual(capture(self.db.cursor(), (1,))[1].generation, 2)
        with self.assertRaises(FolderReviewError):
            self.case.register(review)

    def test_real_comicinfo_jobs_exclude_folder_both_directions(self):
        from TComicInfoRepair import ComicInfoRepairTests

        from backend.base.library_health import HealthLevel
        from backend.base.maintenance_review import Action
        from backend.base.metadata_repair import RepairError
        from backend.features.organization_execution import \
            OrganizationExecutor
        fixture = ComicInfoRepairTests()
        fixture.setUp()
        try:
            db = fixture.db
            db.execute("UPDATE config SET value='{series_name} ({year})' WHERE key='volume_folder_naming'")
            db.commit()
            def folder_review():
                work = fixture.fixture.assign(fixture.fixture.review(), Action.FOLDER, 'folder_deviation')
                service = BulkFolderReviews(fixture.fixture.service)
                review = service.create(db.cursor(), work.id, work.revision, work.manifest_digest,
                    tuple(i.finding.id for i in work.items if i.selected))
                return service, review
            def register(service, review):
                return service.register(db.cursor(), review.id, review.revision, review.digest,
                    confirmed=True, origin=review.origin, selected=review.selected)
            # Fresh owned ComicInfo handoff after policy change.
            fixture.worklist = fixture.fixture.assign(fixture.fixture.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'comicinfo_absent')
            comic = fixture.review()
            folders, review = folder_review()
            with patch.object(OrganizationExecutor, 'apply_job', side_effect=SystemExit('registered only')):
                with self.assertRaises(SystemExit):
                    fixture.apply(comic)
            with self.assertRaises(FolderReviewError):
                register(folders, review)
            job = db.execute('SELECT id FROM organization_jobs').fetchone()[0]
            executor = OrganizationExecutor(str(fixture.fixture.fixture.database), (str(fixture.fixture.fixture.root),))
            try:
                self.assertEqual(executor.apply_job(job).state.value, 'completed')
            finally:
                executor.close()
            fixture.fixture.service._scans.clear()
            fixture.fixture.service._worklists.clear()
            # Another absent document provides a real reviewed repair before the
            # folder reservation. It must not register while that tree is owned.
            fixture.fixture.fixture.comic('second.cbz', xml=None)
            fixture.worklist = fixture.fixture.assign(fixture.fixture.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'comicinfo_absent')
            comic = fixture.review()
            folders, review = folder_review()
            batch = register(folders, review)
            with self.assertRaises((RepairError, OrganizationError)):
                fixture.apply(comic)
            self.assertEqual(folders.execute(db.cursor(), batch['batch_id'])['state'], 'completed')
            fixture.worklist = fixture.fixture.assign(fixture.fixture.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'comicinfo_absent')
            self.assertEqual(fixture.apply(fixture.review())['state'], 'completed')
        finally:
            fixture.doCleanups()

    def test_actual_switch_blocked_by_active_folder_then_allowed_after_completion(self):
        from TProviderSwitchApply import SwitchApplyTests, remote

        from backend.base.switch_review import SwitchReviewError
        from backend.features.provider_switch_review import \
            ProviderSwitchReviews
        switching = SwitchApplyTests()
        switching.db = self.db
        switching.cursor = self.db.cursor()
        switching.service = ProviderSwitchReviews(task_observer=lambda _: ())
        registered = self.case.register(self.case.review())
        switch = switching.review(remote('metron', count=1))
        with self.assertRaises(SwitchReviewError):
            switching.apply(switch)
        def stop(stage, identity, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                raise PermissionError('recovery keeps switch exclusion')
        self.case.service.checkpoint = stop
        self.assertEqual(self.case.service.execute(self.db.cursor(), registered['batch_id'])['items'][0]['state'], 'recovery_required')
        with self.assertRaises(SwitchReviewError):
            switching.apply(switch)
        self.case.service.checkpoint = None
        self.case.service.execute(self.db.cursor(), registered['batch_id'])
        switching.apply(switching.review(remote('metron', count=1)))
        self.assertEqual(capture(self.db.cursor(), (1,))[1].provider, 'metron')

    def test_scan_writer_spans_creation_links_and_cleanup(self):
        self.prepare_links()
        other = sqlite3.connect(self.case.fixture.fixture.database, timeout=0)
        self.addCleanup(other.close)
        seen = []
        def attempt(label):
            seen.append(label)
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                other.execute('BEGIN IMMEDIATE')
            other.rollback()
        from backend.implementations import file_matching
        listing, cleanup = file_matching.list_files, file_matching.delete_empty_child_folders
        def observe(*args, **kwargs):
            attempt('enumeration')
            return listing(*args, **kwargs)
        def clean(*args, **kwargs):
            attempt('cleanup')
            return cleanup(*args, **kwargs)
        with self.scanner(), patch.object(file_matching, 'list_files', side_effect=observe), \
                patch.object(file_matching, 'delete_empty_child_folders', side_effect=clean):
            self.assertEqual(scan_files(1), 'completed')
        self.assertIn('enumeration', seen)
        self.assertIn('cleanup', seen)
        other.execute('BEGIN IMMEDIATE')
        other.rollback()

    def test_local_organization_registration_excludes_folder_both_directions(self):
        from backend.base.import_candidate import DiscoveryScope
        from backend.base.organization_plan import PlanningPolicy
        from backend.features.local_artifact_planning import \
            preview_local_artifacts
        for folder_first in (False, True):
            with self.subTest(folder_first=folder_first):
                case = folder_fixture.BulkFolderTests()
                case.setUp()
                executor = case.executor()
                try:
                    plan = preview_local_artifacts(case.db, (str(case.source),),
                        DiscoveryScope('local-import-fixture', str(case.fixture.fixture.root)),
                        PlanningPolicy(rename=True, windows=os.name == 'nt', case_sensitive=os.name != 'nt'), volume_id=1).plans[0]
                    self.assertTrue(plan.effects)
                    folder = case.review()
                    if folder_first:
                        batch = case.register(folder)
                        with self.assertRaises(OrganizationError):
                            executor.create_job(plan)
                        self.assertEqual(case.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
                        self.assertEqual(case.service.execute(case.db.cursor(), batch['batch_id'])['state'], 'completed')
                    else:
                        job = executor.create_job(plan)
                        with self.assertRaises(FolderReviewError):
                            case.register(folder)
                        self.assertEqual(executor.apply_job(job).state.value, 'completed')
                        case.refresh_worklist()
                        self.assertEqual(case.review().items[0].blockers, ())
                finally:
                    case.doCleanups()

    def test_borrowed_manual_transaction_not_committed_and_private_bypass_rejected(self):
        self.prepare_links()
        self.db.execute("UPDATE volumes SET title='caller pending' WHERE id=1")
        with self.scanner():
            self.assertEqual(scan_files(1), 'completed')
            self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT title FROM volumes WHERE id=1').fetchone()[0], 'Example')
        with self.scanner(), self.assertRaisesRegex(RuntimeError, 'serialized'):
            _scan_files(1, [], True, False)

    def test_borrowed_failed_scan_preserves_prior_caller_work(self):
        session = self.case.review()
        self.case.register(session)
        self.db.execute("UPDATE volumes SET title='caller pending' WHERE id=1")
        with self.scanner(), self.assertRaises(OrganizationError):
            scan_files(1)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT title FROM volumes WHERE id=1').fetchone()[0], 'caller pending')
        self.db.rollback()

    def test_recovery_ignores_unrelated_metadata_and_policy_without_retargeting(self):
        session = self.case.review()
        registered = self.case.register(session)
        def crash(stage, identity, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                raise SystemExit()
        self.case.service.checkpoint = crash
        with self.assertRaises(SystemExit):
            self.case.service.execute(self.db.cursor(), registered['batch_id'])
        self.db.execute("UPDATE config SET value='Changed {series_name}' WHERE key='volume_folder_naming'")
        self.db.execute("UPDATE volumes SET title='new provider title' WHERE id=1")
        self.db.commit()
        self.case.service.checkpoint = None
        result = self.case.service.execute(self.db.cursor(), registered['batch_id'])
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(self.db.execute('SELECT folder,title FROM volumes').fetchone(),
                         (session.items[0].target, 'new provider title'))
        executor = self.case.executor()
        preview = executor.preview_undo(result['items'][0]['job_id'])
        self.assertTrue(preview.eligible, preview)
        inverse = executor.create_undo_job(preview.original_job, preview.intent_digest)
        self.assertEqual(executor.apply_job(inverse).state.value, 'completed')
        self.assertEqual(self.db.execute('SELECT title FROM volumes').fetchone()[0], 'new provider title')

    def test_missing_and_mismatching_recovery_never_reconcile(self):
        for damage in ('neither', 'missing_member', 'extra_member'):
            with self.subTest(damage=damage):
                case = folder_fixture.BulkFolderTests()
                case.setUp()
                try:
                    review = case.review()
                    registered = case.register(review)
                    def stop(stage, identity, ordinal):
                        if stage == 'after_effect' and ordinal == 0:
                            raise SystemExit()
                    case.service.checkpoint = stop
                    with self.assertRaises(SystemExit):
                        case.service.execute(case.db.cursor(), registered['batch_id'])
                    target = Path(review.items[0].target)
                    if damage == 'neither':
                        target.rename(case.fixture.fixture.base / 'held-for-inspection')
                    elif damage == 'missing_member':
                        (target / case.source.name).rename(case.fixture.fixture.base / 'held-comic.cbz')
                    else:
                        (target / 'unexpected.txt').write_bytes(b'new')
                    case.service.checkpoint = None
                    result = case.service.execute(case.db.cursor(), registered['batch_id'])
                    self.assertEqual(result['items'][0]['state'], 'recovery_required')
                    self.assertEqual(case.db.execute('SELECT filepath FROM files').fetchone()[0], str(case.source))
                finally:
                    case.doCleanups()

    def test_journal_capability_and_mixed_noop_durable_retry(self):
        review = self.case.review()
        size = review.items[0].journal_bytes
        with patch('backend.features.bulk_folder.MAX_PAYLOAD', size - 1):
            blocked = self.case.review()
            self.assertIn('journal_intent_too_large', blocked.items[0].blockers)
            with self.assertRaisesRegex(FolderReviewError, 'blocked'):
                self.case.register(blocked)
        with patch('backend.features.bulk_folder.MAX_PAYLOAD', size):
            self.assertNotIn('journal_intent_too_large', self.case.review().items[0].blockers)
        self.case.service = BulkFolderReviews(self.case.fixture.service)
        self.case.batch(3)
        self.db.execute('UPDATE volumes SET custom_folder=1 WHERE id=2')
        self.db.commit()
        # Reacquire the exact worklist after the local ownership change.
        from backend.base.library_health import HealthLevel, HealthScope
        from backend.base.maintenance_review import Action, Edit
        maintenance = self.case.fixture.service
        scan = maintenance.request_scan(HealthScope('volumes', (1, 2, 3)), HealthLevel.INVENTORY)
        self.case.fixture.tasks[-1].run()
        work = maintenance.create(scan)
        self.case.worklist = maintenance.revise(work.id, work.revision,
            tuple(Edit(i.finding.id, True, False, Action.FOLDER) for i in work.items if i.finding.code == 'folder_deviation'))
        session = self.case.review()
        registered = self.case.register(session)
        self.assertEqual(registered['total'], 3)
        self.assertEqual(registered['job_count'], 2)
        self.assertEqual(registered['counts']['no_changes'], 1)
        self.case.service = BulkFolderReviews(maintenance)
        self.assertEqual(self.case.register(session), registered)
        result = self.case.service.execute(self.db.cursor(), registered['batch_id'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['counts'], {'completed': 2, 'no_changes': 1})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 2)

    def test_full_ten_thousand_entry_review_diagnostic(self):
        import tracemalloc
        from dataclasses import asdict
        from time import perf_counter

        from backend.base.library_health import canonical
        for number in range(9999):
            (self.case.source.parent / f'ancillary-{number:05}.txt').write_bytes(b'fixture')
        # The owned folder intent predates newly observed ancillary files. The
        # fresh child review inventories them, without a 10,000-finding worklist.
        queries = []
        real_connect = sqlite3.connect
        def traced(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection
        self.db.set_trace_callback(queries.append)
        inspections = {'lstat': 0, 'scandir': 0}
        real_lstat, real_scandir = os.lstat, os.scandir
        def observed_lstat(*args, **kwargs):
            inspections['lstat'] += 1
            return real_lstat(*args, **kwargs)
        def observed_scandir(*args, **kwargs):
            inspections['scandir'] += 1
            return real_scandir(*args, **kwargs)
        tracemalloc.start()
        try:
            with patch('sqlite3.connect', side_effect=traced), patch('os.lstat', new=observed_lstat), \
                    patch('os.scandir', new=observed_scandir):
                started = perf_counter()
                review = self.case.review()
                elapsed = perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            self.db.set_trace_callback(None)
        self.assertEqual(len(review.items[0].inventory.entries), 10000)
        self.assertEqual(review.items[0].blockers, ())
        print('8F_FULL_REVIEW', dict(entries=10000, seconds=round(elapsed, 4), peak=peak,
            review_bytes=len(canonical(asdict(review))), journal_bytes=review.items[0].journal_bytes,
            filesystem=inspections,
            selects=sum(q.lstrip().upper().startswith('SELECT') for q in queries)))

    def test_maximum_atomic_registration_and_late_rollback_diagnostic(self):
        import tracemalloc
        from dataclasses import asdict
        from time import perf_counter

        from backend.base.library_health import canonical
        from backend.internals.organization_jobs import JobStore
        self.case.batch(50)
        review = self.case.review()
        create = JobStore.create_in_transaction
        calls = []
        def fail_last(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 50:
                raise RuntimeError('last registration failed')
            return create(store, *args, **kwargs)
        with patch.object(JobStore, 'create_in_transaction', fail_last), self.assertRaises(RuntimeError):
            self.case.register(review)
        for table in ('organization_jobs', 'organization_reservations'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        queries = []
        real_connect = sqlite3.connect
        def traced(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection
        self.db.set_trace_callback(queries.append)
        tracemalloc.start()
        try:
            with patch('sqlite3.connect', side_effect=traced):
                started = perf_counter()
                registered = self.case.register(review)
                elapsed = perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            self.db.set_trace_callback(None)
        self.assertEqual(registered['job_count'], 50)
        reservations = self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0]
        self.assertEqual(reservations, 100)
        print('8F_MAX_REGISTER', dict(jobs=50, seconds=round(elapsed, 4), peak=peak, reservations=reservations,
            review_bytes=len(canonical(asdict(review))), journal_bytes=sum(i.journal_bytes for i in review.items),
            selects=sum(q.lstrip().upper().startswith('SELECT') for q in queries)))

    def test_service_device_and_case_blockers(self):
        from backend.base.library_health import canonical
        from backend.features import bulk_folder
        real = bulk_folder.target_observation
        for device, code in ((None, 'target_device_unknown'), (-999, 'cross_device_not_supported')):
            def observed(target):
                value = json.loads(real(target))
                value['parent']['stamp'][0] = device
                return canonical(value)
            with patch.object(bulk_folder, 'target_observation', side_effect=observed):
                review = self.case.review()
                self.assertIn(code, review.items[0].blockers)
                with self.assertRaises(FolderReviewError):
                    self.case.register(review)
            self.case.service.delete(review.id)
        self.db.execute("UPDATE volumes SET title='EXAMPLE'")
        self.db.execute("UPDATE config SET value='{series_name}' WHERE key='volume_folder_naming'")
        self.db.commit()
        self.case.refresh_worklist()
        review = self.case.review()
        self.assertIn('case_only_folder_transition', review.items[0].blockers)
        with self.assertRaises(FolderReviewError):
            self.case.register(review)

    def test_service_cross_root_and_root_state_changes(self):
        from dataclasses import replace

        from backend.features import bulk_folder
        other = self.case.fixture.fixture.base / 'other-root'
        other.mkdir()
        self.db.execute('INSERT INTO root_folders(id,folder) VALUES(2,?)', (str(other),))
        self.db.commit()
        self.case.refresh_worklist()
        real = bulk_folder.decide_folder
        def other_root(*args):
            return replace(real(*args), root_id=2, root=str(other), target_folder=str(other / 'Example'))
        with patch.object(bulk_folder, 'decide_folder', side_effect=other_root):
            review = self.case.review()
            self.assertIn('root_transition_not_supported', review.items[0].blockers)
            with self.assertRaises(FolderReviewError):
                self.case.register(review)
        review = self.case.review()
        self.db.execute('UPDATE volumes SET root_folder=2')
        self.db.commit()
        with self.assertRaises(FolderReviewError):
            self.case.register(review)

    def test_custom_inverse_restores_flag_and_changed_flag_stales(self):
        self.db.execute('UPDATE volumes SET custom_folder=1')
        self.db.commit()
        self.case.refresh_worklist()
        preserved = self.case.review()
        review = self.case.service.create(self.db.cursor(), self.case.worklist.id, self.case.worklist.revision,
            self.case.worklist.manifest_digest, preserved.selected, canonical_custom=preserved.selected)
        result = self.case.service.execute(self.db.cursor(), self.case.register(review)['batch_id'])
        executor = self.case.executor()
        preview = executor.preview_undo(result['items'][0]['job_id'])
        inverse = executor.create_undo_job(preview.original_job, preview.intent_digest)
        self.assertEqual(executor.apply_job(inverse).state.value, 'completed')
        self.assertEqual(self.db.execute('SELECT folder,custom_folder FROM volumes').fetchone(),
                         (str(self.case.source.parent), 1))
        self.case.refresh_worklist()
        review = self.case.review()
        self.db.execute('UPDATE volumes SET custom_folder=0')
        self.db.commit()
        with self.assertRaises(FolderReviewError):
            self.case.register(review)

    def test_review_capacity_size_entry_revision_and_expiry_limits(self):
        from dataclasses import replace
        first = self.case.review()
        with patch.object(self.case.service, 'MAX_ENTRIES', 0), self.assertRaisesRegex(FolderReviewError, 'inventory_limit'):
            self.case.review()
        with patch.object(self.case.service, 'MAX_BYTES', 1), self.assertRaisesRegex(FolderReviewError, 'size_limit'):
            self.case.review()
        self.assertEqual(len(self.case.service._sessions), 1)
        self.case.service._sessions[first.id] = replace(first, revision=1000)
        with self.assertRaisesRegex(FolderReviewError, 'revision'):
            self.case.service.revise(first.id, 1000, first.selected)
        for _ in range(3):
            self.case.review()
        with self.assertRaisesRegex(FolderReviewError, 'capacity'):
            self.case.review()
        self.case.fixture.clock[0] += self.case.service.TTL
        with self.assertRaisesRegex(FolderReviewError, 'expired'):
            self.case.service.get(first.id)

    def test_real_journal_limit_blocks_complete_inventory_before_confirmation(self):
        # A bounded ownership snapshot can still exceed the smaller journal
        # after before/after state are retained. No tree evidence is discarded.
        self.db.execute('UPDATE issues SET title=? WHERE id=1', ('large evidence ' * 245000,))
        self.db.commit()
        self.case.refresh_worklist()
        review = self.case.review()
        from backend.internals.organization_jobs import MAX_PAYLOAD
        self.assertTrue(review.items[0].inventory.complete)
        self.assertGreater(review.items[0].journal_bytes, MAX_PAYLOAD)
        self.assertIn('journal_intent_too_large', review.items[0].blockers)
        with self.assertRaises(FolderReviewError):
            self.case.register(review)
        self.assertTrue(self.case.source.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)

    def test_global_orphan_pruning_checks_reserved_tree_under_writer(self):
        from backend.internals.scan_mutation import unmatched_reserved
        self.case.register(self.case.review())
        orphan = self.case.source.parent / 'not-linked.txt'
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(99,?,1)', (str(orphan),))
        self.db.commit()
        with self.assertRaises(RuntimeError):
            unmatched_reserved(self.db.cursor())
        self.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(unmatched_reserved(self.db.cursor()))
        self.db.rollback()
        with self.scanner(), self.assertRaises(OrganizationError):
            scan_files(1)
        self.assertEqual(self.db.execute('SELECT filepath FROM files WHERE id=99').fetchone()[0], str(orphan))

    def test_ownership_only_noop_blocked(self):
        from dataclasses import replace

        from backend.features import bulk_folder
        self.db.execute('UPDATE volumes SET custom_folder=1')
        self.db.commit()
        self.case.refresh_worklist()
        preserved = self.case.review()
        real = bulk_folder.decide_folder
        def no_path_effect(*args):
            return replace(real(*args), target_folder=str(self.case.source.parent))
        with patch.object(bulk_folder, 'decide_folder', side_effect=no_path_effect):
            review = self.case.service.create(self.db.cursor(), self.case.worklist.id, self.case.worklist.revision,
                self.case.worklist.manifest_digest, preserved.selected, canonical_custom=preserved.selected)
        self.assertIn('ownership_only_transition_not_supported', review.items[0].blockers)
        with self.assertRaises(FolderReviewError):
            self.case.register(review)

    def test_root_filesystem_identity_and_parent_disappearance_stale(self):
        review = self.case.review()
        root = self.case.fixture.fixture.root
        held = root.with_name('held-root')
        root.rename(held)
        with self.assertRaises((FolderReviewError, OrganizationError)):
            self.case.register(review)
        root.mkdir()
        (held / self.case.source.parent.name).rename(self.case.source.parent)
        with self.assertRaises(FolderReviewError):
            self.case.register(review)

    def test_namespace_failure_and_small_execution_timings(self):
        from time import perf_counter

        from backend.features import organization_directory
        review = self.case.review()
        batch = self.case.register(review)
        with patch.object(organization_directory, 'rename_no_replace', side_effect=PermissionError('namespace denied')):
            result = self.case.service.execute(self.db.cursor(), batch['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'recovery_required')
        self.assertTrue(self.case.source.exists())
        self.assertFalse(Path(review.items[0].target).exists())
        real = organization_directory.rename_no_replace
        observations = {}
        def timed(*args):
            started = perf_counter()
            real(*args)
            observations['namespace_seconds'] = perf_counter() - started
        def checkpoint(stage, identity, ordinal):
            if stage == 'before_db_commit':
                observations['db_start'] = perf_counter()
            elif stage == 'after_db_commit':
                observations['db_seconds'] = perf_counter() - observations.pop('db_start')
        self.case.service.checkpoint = checkpoint
        with patch.object(organization_directory, 'rename_no_replace', side_effect=timed):
            self.assertEqual(self.case.service.execute(self.db.cursor(), batch['batch_id'])['state'], 'completed')
        print('8F_EFFECT_TIMING', observations)

    def test_actual_metadata_repair_stales_folder_and_receipt_survives_fresh_move(self):
        from TMetadataRepairApply import MetadataRepairApplyTests

        from backend.base.maintenance_review import Action
        from backend.base.metadata_repair import Field, FieldSelection
        from backend.internals.metadata_repair_history import page
        fixture = MetadataRepairApplyTests()
        fixture.setUp()
        try:
            db = fixture.db
            maintenance = fixture.fixture.fixture
            db.execute("UPDATE config SET value='{series_name} ({year})' WHERE key='volume_folder_naming'")
            db.execute("INSERT OR REPLACE INTO config VALUES('database_version',65)")
            db.commit()
            fixture.fixture.worklist = maintenance.assign(maintenance.review(), Action.METADATA)
            repair = fixture.review((FieldSelection('volume', 1, Field.TITLE),))
            folders = BulkFolderReviews(maintenance.service)
            def reviewed():
                work = maintenance.assign(maintenance.review(), Action.FOLDER, 'folder_deviation')
                return folders.create(db.cursor(), work.id, work.revision, work.manifest_digest,
                    tuple(i.finding.id for i in work.items if i.selected))
            def register(review):
                return folders.register(db.cursor(), review.id, review.revision, review.digest,
                    confirmed=True, origin=review.origin, selected=review.selected)
            folder = reviewed()
            self.assertEqual(fixture.apply(repair)['state'], 'applied')
            receipt = page(db.cursor(), 1)
            with self.assertRaises(FolderReviewError):
                register(folder)
            fresh = reviewed()
            self.assertIn('Target comicvine', fresh.items[0].target)
            self.assertEqual(folders.execute(db.cursor(), register(fresh)['batch_id'])['state'], 'completed')
            self.assertEqual(page(db.cursor(), 1), receipt)
        finally:
            fixture.doCleanups()

"""Disposable whole-tree folder review/execution characterizations."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import TMaintenanceReview as fixture

from backend.base.bulk_folder import FolderReviewError
from backend.base.library_health import HealthLevel, HealthScope
from backend.base.maintenance_review import Action, Edit
from backend.base.organization_job import OrganizationError
from backend.features.bulk_folder import BulkFolderReviews, collision_groups
from backend.features.bulk_folder_tasks import BulkFolderTask
from backend.features.bulk_rename import BulkRenameReviews
from backend.features.organization_execution import OrganizationExecutor
from backend.internals.switch_review import dependencies


class BulkFolderTests(TestCase):
    def setUp(self):
        self.fixture = fixture.MaintenanceReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',65)")
        self.db.execute("UPDATE config SET value='{series_name} ({year})' WHERE key='volume_folder_naming'")
        self.source = self.fixture.fixture.comic('unchanged.cbz')
        self.worklist = self.fixture.assign(self.fixture.review(), Action.FOLDER, 'folder_deviation')
        self.service = BulkFolderReviews(self.fixture.service, clock=lambda: self.fixture.clock[0])

    def review(self):
        return self.service.create(self.db.cursor(), self.worklist.id, self.worklist.revision,
            self.worklist.manifest_digest, tuple(i.finding.id for i in self.worklist.items if i.selected))

    def register(self, session):
        return self.service.register(self.db.cursor(), session.id, session.revision, session.digest,
            confirmed=True, origin=session.origin, selected=session.selected)

    def test_tree_transition_and_retry(self):
        session = self.review()
        self.assertEqual(session.items[0].blockers, ())
        before = sha256(self.source.read_bytes()).hexdigest(), self.source.stat().st_mtime_ns
        registered = self.register(session)
        result = self.service.execute(self.db.cursor(), registered['batch_id'])
        self.assertEqual(result['state'], 'completed', result)
        target = Path(session.items[0].target) / self.source.name
        self.assertEqual(before, (sha256(target.read_bytes()).hexdigest(), target.stat().st_mtime_ns))
        self.assertFalse(self.source.exists())
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], str(target))
        self.service = BulkFolderReviews(self.fixture.service)
        self.assertEqual(self.register(session)['items'], result['items'])
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def executor(self, checkpoint=None):
        executor = OrganizationExecutor(str(self.fixture.fixture.database),
            (str(self.fixture.fixture.root),), checkpoint=checkpoint)
        self.addCleanup(executor.close)
        return executor

    def refresh_worklist(self):
        self.worklist = self.fixture.assign(self.fixture.review(), Action.FOLDER, 'folder_deviation')

    def test_ancillary_empty_nested_general_and_domain_preservation(self):
        tree = self.source.parent
        (tree / 'empty').mkdir()
        (tree / 'Covers').mkdir()
        (tree / 'Covers' / 'cover.jpg').write_bytes(b'untouched cover')
        (tree / 'notes.txt').write_bytes(b'untouched ancillary')
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,?)',
            (str(tree / 'Covers' / 'cover.jpg'), len(b'untouched cover')))
        self.db.execute("INSERT INTO volume_files(volume_id,file_id,file_type,forced) VALUES(1,2,'cover',0)")
        self.refresh_worklist()
        tables = ('issues', 'issues_files', 'volume_files', 'volume_external_ids', 'issue_external_ids',
                  'bibliographic_content_claims', 'file_content_coverage', 'classification_provenance')
        state = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables}
        files = {str(p.relative_to(tree)): (sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                 for p in tree.rglob('*') if p.is_file()}
        before = tuple(self.db.iterdump())
        session = self.review()
        self.assertEqual(before, tuple(self.db.iterdump()))
        with patch('socket.socket', side_effect=AssertionError('provider IO')):
            result = self.service.execute(self.db.cursor(), self.register(session)['batch_id'])
        self.assertEqual(result['state'], 'completed', result)
        target = Path(session.items[0].target)
        self.assertTrue((target / 'empty').is_dir())
        self.assertEqual(files, {str(p.relative_to(target)): (sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                                for p in target.rglob('*') if p.is_file()})
        self.assertEqual(state, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 2)
        self.assertFalse(tree.exists())

    def test_crash_after_move_restart_recovery_and_inverse(self):
        session = self.review()
        registered = self.register(session)
        job = registered['items'][0]['job_id']
        def crash(stage, identity, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                raise SystemExit('simulated process death')
        executor = self.executor(crash)
        with self.assertRaises(SystemExit):
            executor.apply_job(job)
        executor.close()
        self.assertFalse(self.source.exists())
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], str(self.source))
        executor = self.executor()
        executor.reconcile_job(job)
        self.assertEqual(executor.apply_job(job).state.value, 'completed')
        self.assertEqual(executor.apply_job(job).state.value, 'completed')
        self.assertTrue(executor.inspect_job(job)['observations']['database']['matches'])
        preview = executor.preview_undo(job)
        self.assertTrue(preview.eligible, preview)
        inverse = executor.create_undo_job(job, preview.intent_digest)
        self.assertEqual(executor.apply_job(inverse).state.value, 'completed')
        self.assertTrue(self.source.exists())
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], str(self.source))

    def test_db_failure_rolls_back_paths_and_recovers_forward(self):
        session = self.review()
        registered = self.register(session)
        def fail(stage, identity, ordinal):
            if stage == 'directory_path_updated':
                raise PermissionError('injected reconciliation failure')
        self.service.checkpoint = fail
        result = self.service.execute(self.db.cursor(), registered['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'recovery_required')
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], str(self.source))
        self.assertEqual(self.db.execute('SELECT folder FROM volumes').fetchone()[0], str(self.source.parent))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0], 2)
        self.service.checkpoint = None
        self.assertEqual(self.service.execute(self.db.cursor(), registered['batch_id'])['state'], 'completed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0], 0)

    def test_stale_generation_settings_membership_target(self):
        session = self.review()
        for generation in (1, 2):
            self.db.execute('UPDATE volumes SET authority_generation=?', (generation,))
            self.db.commit()
            with self.assertRaisesRegex(FolderReviewError, 'stale'):
                self.register(session)
        self.db.execute('UPDATE volumes SET authority_generation=0')
        self.db.commit()
        # Staleness is latched, including an ABA-like return to old values.
        with self.assertRaisesRegex(FolderReviewError, 'stale'):
            self.register(session)
        session = self.review()
        (self.source.parent / 'new.txt').write_bytes(b'new member')
        with self.assertRaisesRegex(FolderReviewError, 'stale'):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)

    def test_target_appeared_and_settings_change(self):
        session = self.review()
        Path(session.items[0].target).mkdir()
        with self.assertRaisesRegex(FolderReviewError, 'stale'):
            self.register(session)
        self.assertTrue(self.source.exists())
        self.db.execute("UPDATE config SET value='{series_name}' WHERE key='volume_folder_naming'")
        self.db.commit()
        with self.assertRaisesRegex(FolderReviewError, 'stale'):
            self.register(session)

    def test_registration_failure_no_job_or_move(self):
        session = self.review()
        def fail(stage, identity, ordinal):
            if stage == 'before_folder_registration':
                raise PermissionError('injected registration failure')
        self.service.checkpoint = fail
        with self.assertRaises(PermissionError):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        self.assertTrue(self.source.exists())

    def test_expiry_revision_and_confirmation(self):
        session = self.review()
        revised = self.service.revise(session.id, session.revision, session.selected)
        self.assertNotEqual(session.digest, revised.digest)
        with self.assertRaisesRegex(FolderReviewError, 'stale'):
            self.register(session)
        self.fixture.clock[0] += self.service.TTL
        with self.assertRaisesRegex(FolderReviewError, 'expired'):
            self.register(revised)

    def test_ambiguous_both_paths_preserves_recovery(self):
        session = self.review()
        result = self.register(session)
        def fail(stage, identity, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                self.source.parent.mkdir()
                raise PermissionError('ambiguous interruption')
        self.service.checkpoint = fail
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.service.checkpoint = None
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'recovery_required')
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], str(self.source))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0], 2)

    def batch(self, count):
        root = self.fixture.fixture.root
        for number in range(2, count + 1):
            source = root / f'Old {number}'
            source.mkdir()
            comic = source / 'unchanged.cbz'
            comic.write_bytes(b'disposable file')
            self.db.execute('''INSERT INTO volumes(id,title,root_folder,folder,metadata_provider,comicvine_id,volume_number,year)
                VALUES(?,?,1,?,'comicvine',?,1,2020)''', (number, f'Volume {number}', str(source), 100 * number))
            self.db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(?,?,?,\'1\',1)',
                (number, number, 100 * number + 1))
            self.db.execute('INSERT INTO files(id,filepath,size) VALUES(?,?,?)', (number, str(comic), comic.stat().st_size))
            self.db.execute('INSERT INTO issues_files(issue_id,file_id) VALUES(?,?)', (number, number))
        self.db.commit()
        scan = self.fixture.service.request_scan(HealthScope('volumes', tuple(range(1, count + 1))), HealthLevel.INVENTORY)
        self.fixture.tasks[-1].run()
        worklist = self.fixture.service.create(scan)
        self.worklist = self.fixture.service.revise(worklist.id, worklist.revision,
            tuple(Edit(i.finding.id, True, False, Action.FOLDER) for i in worklist.items if i.finding.code == 'folder_deviation'))

    def test_atomic_registration_and_independent_partial_completion(self):
        self.batch(3)
        session = self.review()
        self.assertEqual(len(session.items), 3)
        def failure(stage, identity, ordinal):
            if stage == 'before_folder_registration' and ordinal == 1:
                raise PermissionError('second registration')
        self.service.checkpoint = failure
        with self.assertRaises(PermissionError):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0], 0)
        self.service.checkpoint = None
        result = self.register(session)
        failed = result['items'][0]['job_id']
        def one_failure(stage, identity, ordinal):
            if stage == 'before_effect' and identity == failed and ordinal == 0:
                raise PermissionError('one sibling')
        self.service.checkpoint = one_failure
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['state'], 'partially_completed_batch', result)
        self.assertEqual(result['counts']['completed'], 2)

    def test_subtree_blocks_rename_and_switch_dependency_until_completion(self):
        rename_work = self.fixture.assign(self.fixture.review(), Action.RENAME)
        rename = BulkRenameReviews(self.fixture.service)
        rename_review = rename.create(self.db.cursor(), rename_work.id, rename_work.revision, rename_work.manifest_digest,
            tuple(i.finding.id for i in rename_work.items if i.selected))
        folder = self.review()
        registered = self.register(folder)
        self.assertTrue(any(d['category'] == 'organization' for d in dependencies(self.db.cursor(), 1)))
        with self.assertRaises((ValueError, OrganizationError)):
            rename.register(self.db.cursor(), rename_review.id, rename_review.revision, rename_review.digest,
                confirmed=True, origin=rename_review.origin, selected=rename_review.selected)
        self.service.execute(self.db.cursor(), registered['batch_id'])
        self.assertEqual(dependencies(self.db.cursor(), 1), [])
        with self.assertRaises((ValueError, OrganizationError)):
            rename.register(self.db.cursor(), rename_review.id, rename_review.revision, rename_review.digest,
                confirmed=True, origin=rename_review.origin, selected=rename_review.selected)
        fresh = self.fixture.assign(self.fixture.review(), Action.RENAME)
        new_review = rename.create(self.db.cursor(), fresh.id, fresh.revision, fresh.manifest_digest,
            tuple(i.finding.id for i in fresh.items if i.selected))
        self.assertEqual(Path(new_review.items[0].plan.target_path).parent, Path(folder.items[0].target))

    def test_file_reservation_blocks_parent_registration(self):
        folder = self.review()
        work = self.fixture.assign(self.fixture.review(), Action.RENAME)
        rename = BulkRenameReviews(self.fixture.service)
        review = rename.create(self.db.cursor(), work.id, work.revision, work.manifest_digest,
            tuple(i.finding.id for i in work.items if i.selected))
        rename.register(self.db.cursor(), review.id, review.revision, review.digest,
            confirmed=True, origin=review.origin, selected=review.selected)
        with self.assertRaisesRegex(FolderReviewError, 'dependency'):
            self.register(folder)

    def test_custom_preserved_and_explicit_canonical(self):
        self.db.execute('UPDATE volumes SET custom_folder=1')
        self.refresh_worklist()
        preserved = self.review()
        self.assertTrue(preserved.items[0].no_changes)
        self.assertEqual(self.register(preserved)['state'], 'no_changes')
        changed = self.service.create(self.db.cursor(), self.worklist.id, self.worklist.revision,
            self.worklist.manifest_digest, preserved.selected, canonical_custom=preserved.selected)
        self.assertFalse(changed.items[0].custom_after)
        self.assertEqual(self.service.execute(self.db.cursor(), self.register(changed)['batch_id'])['state'], 'completed')
        self.assertEqual(self.db.execute('SELECT custom_folder FROM volumes').fetchone()[0], 0)

    def test_collision_index_chains_cycles_nesting_and_case(self):
        item = self.review().items[0]
        for second_source, second_target in ((item.target, str(Path(item.target).with_name('Third'))),
                                             (item.target, item.ownership.source),
                                             (item.ownership.source + '/child', item.target + '/child'),
                                             (item.ownership.source.upper(), item.target.upper())):
            with self.subTest(source=second_source):
                other = replace(item, finding_id='b' * 64, target=second_target,
                                ownership=replace(item.ownership, source=second_source))
                self.assertTrue(json.loads(collision_groups((item, other), (item.finding_id, other.finding_id))))

    def test_task_retry_and_inverse_blocked_by_changed_tree(self):
        session = self.review()
        task = BulkFolderTask(self.service, session.id, session.revision, session.digest,
            origin=session.origin, selected=session.selected, confirmed=True)
        task.run()
        self.assertEqual(task.result['state'], 'completed')
        task.run()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
        executor = self.executor()
        job = task.result['items'][0]['job_id']
        self.assertTrue(executor.preview_undo(job).eligible)
        (Path(session.items[0].target) / 'unexpected.txt').write_bytes(b'new content')
        self.assertFalse(executor.preview_undo(job).eligible)

    def test_unknown_effect_version_fails_closed(self):
        session = self.review()
        result = self.register(session)
        executor = self.executor()
        intent = executor.store.intent(result['items'][0]['job_id'])
        intent['directory_effect'] = 'volume-tree/future'
        with self.assertRaises(OrganizationError):
            executor._validate_intent(intent)
        self.assertTrue(self.source.exists())

    def test_directory_failure_boundaries_forward_recover(self):
        for stage in ('after_started', 'before_effect', 'after_effect', 'before_db_commit',
                      'directory_path_updated', 'before_directory_volume_update', 'after_db_commit'):
            with self.subTest(stage=stage):
                case = BulkFolderTests('test_tree_transition_and_retry')
                case.setUp()
                try:
                    review = case.review()
                    registered = case.register(review)
                    def crash(name, identity, ordinal):
                        if name == stage:
                            raise SystemExit('simulated process death at ' + stage)
                    case.service.checkpoint = crash
                    with self.assertRaises(SystemExit):
                        case.service.execute(case.db.cursor(), registered['batch_id'])
                    case.service = BulkFolderReviews(case.fixture.service)
                    self.assertEqual(case.service.execute(case.db.cursor(), registered['batch_id'])['state'], 'completed')
                    self.assertEqual(case.db.execute('PRAGMA foreign_key_check').fetchall(), [])
                finally:
                    case.doCleanups()

    def test_monitor_pending_source_target_recovery_and_release(self):
        from backend.features.folder_monitor import stamp
        from backend.features.library_reconciliation import LibraryReconciler
        session = self.review()
        before = LibraryReconciler(str(self.fixture.fixture.database), self.db)(1, stamp(str(self.source)))
        self.assertNotEqual(before.reason, 'organization_job_owns_path')
        registered = self.register(session)
        blocked = LibraryReconciler(str(self.fixture.fixture.database), self.db)(1, stamp(str(self.source)))
        self.assertEqual(blocked.reason, 'organization_job_owns_path')
        def stop(stage, identity, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                raise PermissionError('pause with target subtree reserved')
        self.service.checkpoint = stop
        self.service.execute(self.db.cursor(), registered['batch_id'])
        target = str(Path(session.items[0].target) / self.source.name)
        blocked = LibraryReconciler(str(self.fixture.fixture.database), self.db)(1, stamp(target))
        self.assertEqual(blocked.reason, 'organization_job_owns_path')
        self.service.checkpoint = None
        self.service.execute(self.db.cursor(), registered['batch_id'])
        after = LibraryReconciler(str(self.fixture.fixture.database), self.db)(1, stamp(target))
        self.assertNotEqual(after.reason, 'organization_job_owns_path')

    def test_projection_mismatch_requires_inspection(self):
        session = self.review()
        registered = self.register(session)
        def stop(stage, identity, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                raise PermissionError('pause after namespace move')
        self.service.checkpoint = stop
        self.service.execute(self.db.cursor(), registered['batch_id'])
        target = Path(session.items[0].target)
        (target / 'unexpected.txt').write_bytes(b'not reviewed')
        self.service.checkpoint = None
        result = self.service.execute(self.db.cursor(), registered['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'recovery_required')
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], str(self.source))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0], 2)

    def test_review_diagnostics_and_ten_job_registration(self):
        import sqlite3
        import tracemalloc
        from dataclasses import asdict
        from time import perf_counter

        from backend.base.library_health import canonical
        for count in (100, 1000):
            for number in range(1, count):
                path = self.source.parent / f'ancillary-{number}.txt'
                if not path.exists():
                    path.write_bytes(b'fixture')
            self.refresh_worklist()
            queries = []
            real_connect = sqlite3.connect
            def traced(*args, **kwargs):
                db = real_connect(*args, **kwargs)
                db.set_trace_callback(queries.append)
                return db
            self.db.set_trace_callback(queries.append)
            tracemalloc.start()
            with patch('sqlite3.connect', side_effect=traced):
                started = perf_counter()
                review = self.review()
                elapsed = perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.db.set_trace_callback(None)
            self.assertEqual(review.items[0].blockers, ())
            print('8F_REVIEW', dict(entries=count, seconds=round(elapsed, 4), peak=peak,
                bytes=len(canonical(asdict(review))), selects=sum(q.lstrip().upper().startswith('SELECT') for q in queries)))
            self.service.delete(review.id)
        self.batch(10)
        review = self.review()
        started = perf_counter()
        registered = self.register(review)
        print('8F_REGISTER', dict(jobs=registered['total'], seconds=round(perf_counter() - started, 4)))
        self.assertEqual(registered['total'], 10)

"""Filename-only maintenance execution against disposable databases/files."""

import sqlite3
import tracemalloc
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

import TMaintenanceReview as fixture

from backend.base.bulk_rename import RenameReviewError
from backend.base.maintenance_review import Action, Edit
from backend.features.bulk_rename import BulkRenameReviews
from backend.features.bulk_rename_tasks import BulkRenameTask
from backend.features.organization_execution import OrganizationExecutor


class BulkRenameTests(TestCase):
    def setUp(self):
        self.fixture = fixture.MaintenanceReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',65)")
        self.source = self.fixture.fixture.comic('wrong.cbz')
        self.worklist = self.fixture.assign(self.fixture.review(), Action.RENAME)
        self.service = BulkRenameReviews(self.fixture.service, clock=lambda: self.fixture.clock[0])

    def review(self):
        return self.service.create(self.db.cursor(), self.worklist.id, self.worklist.revision,
            self.worklist.manifest_digest, tuple(i.finding.id for i in self.worklist.items if i.selected))

    def register(self, session):
        return self.service.register(self.db.cursor(), session.id, session.revision, session.digest,
            confirmed=True, origin=session.origin, selected=session.selected)

    def test_same_parent_bytes_associations_and_retry(self):
        before = tuple(self.db.iterdump()), self.fixture.fixture.filesystem()
        session = self.review()
        self.assertEqual(before, (tuple(self.db.iterdump()), self.fixture.fixture.filesystem()))
        old_hash, old_mtime = sha256(self.source.read_bytes()).hexdigest(), self.source.stat().st_mtime_ns
        domains = ('volumes', 'issues', 'issues_files', 'volume_files', 'volume_external_ids',
                   'issue_external_ids', 'bibliographic_content_claims', 'file_content_coverage')
        state = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in domains}
        with patch('socket.socket', side_effect=AssertionError('network')):
            registered = self.register(session)
            self.assertEqual(registered['total'], 1)
            result = self.service.execute(self.db.cursor(), registered['batch_id'])
        self.assertEqual(result['state'], 'completed', result)
        target = Path(session.items[0].plan.target_path)
        self.assertEqual(target.parent, self.source.parent)
        self.assertFalse(self.source.exists())
        self.assertEqual(sha256(target.read_bytes()).hexdigest(), old_hash)
        self.assertEqual(target.stat().st_mtime_ns, old_mtime)
        self.assertEqual(state, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in domains})
        self.service = BulkRenameReviews(self.fixture.service)  # lost transient review
        self.assertEqual(self.register(session)['items'], result['items'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_generation_and_aba_prevent_registration(self):
        for generation in (1, 2):
            with self.subTest(generation=generation):
                session = self.review()
                self.db.execute('UPDATE volumes SET authority_generation=?', (generation,))
                self.db.commit()
                with self.assertRaisesRegex(RenameReviewError, 'stale'):
                    self.register(session)
                self.db.execute('UPDATE volumes SET authority_generation=0')
                self.db.commit()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)

    def test_generation_change_after_registration_cannot_rename(self):
        session = self.review()
        result = self.register(session)
        self.db.execute('UPDATE volumes SET authority_generation=2')
        self.db.commit()
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertNotEqual(result['state'], 'completed')
        self.assertTrue(self.source.exists())

    def test_check_use_race_at_rename_step(self):
        session = self.review()
        result = self.register(session)
        def hook(stage, job, ordinal):
            if stage == 'before_effect' and ordinal == 0:
                self.db.execute('UPDATE volumes SET authority_generation=2')
                self.db.commit()
        self.service.checkpoint = hook
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'recovery_required')
        self.assertTrue(self.source.exists())

    def test_recovery_after_move_and_conditional_inverse(self):
        session = self.review()
        result = self.register(session)
        def hook(stage, job, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                raise PermissionError('disposable interruption')
        self.service.checkpoint = hook
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'recovery_required')
        self.service.checkpoint = None
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['state'], 'completed', result)
        executor = OrganizationExecutor(str(self.fixture.fixture.database), (str(self.fixture.fixture.root),))
        try:
            preview = executor.preview_undo(result['items'][0]['job_id'])
            self.assertTrue(preview.eligible, preview)
            self.source.write_bytes(b'occupied')
            self.assertFalse(executor.preview_undo(result['items'][0]['job_id']).eligible)
        finally:
            executor.close()

    def test_revision_expiry_unknown_and_manifest(self):
        session = self.review()
        revised = self.service.revise(session.id, session.revision, session.selected)
        self.assertNotEqual(session.digest, revised.digest)
        with self.assertRaisesRegex(RenameReviewError, 'stale'):
            self.register(session)
        with self.assertRaisesRegex(RenameReviewError, 'unknown'):
            self.service.revise(session.id, revised.revision, ('a' * 64,))
        self.fixture.clock[0] += self.service.TTL
        with self.assertRaisesRegex(RenameReviewError, 'expired'):
            self.register(revised)

    def test_registration_failure_is_rolled_back(self):
        session = self.review()
        def hook(stage, job, ordinal):
            if stage == 'before_rename_registration':
                raise PermissionError('registration injection')
        self.service.checkpoint = hook
        with self.assertRaises(PermissionError):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        self.assertTrue(self.source.exists())

    def test_source_and_destination_freshness(self):
        session = self.review()
        Path(session.items[0].plan.target_path).write_bytes(b'occupied')
        with self.assertRaisesRegex(RenameReviewError, 'stale'):
            self.register(session)
        self.assertTrue(self.source.exists())

    def test_review_bounds_and_no_job_on_delete(self):
        self.service.MAX_BYTES = 1
        with self.assertRaisesRegex(RenameReviewError, 'size_limit'):
            self.review()
        self.service.MAX_BYTES = 32 * 1024 * 1024
        session = self.review()
        self.service.delete(session.id)
        with self.assertRaisesRegex(RenameReviewError, 'unavailable'):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)

    def batch(self, count):
        for n in range(2, count + 1):
            self.db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(?,1,?,?,?)',
                            (n, 100 + n, str(n), n))
            path = self.fixture.fixture.comic(f'wrong-{n}.cbz')
            fid = self.db.execute('SELECT id FROM files WHERE filepath=?', (str(path),)).fetchone()[0]
            self.db.execute('UPDATE issues_files SET issue_id=? WHERE file_id=?', (n, fid))
        self.db.commit()
        worklist = self.fixture.review()
        self.worklist = self.fixture.service.revise(worklist.id, worklist.revision,
            tuple(Edit(i.finding.id, True, False, Action.RENAME) for i in worklist.items if i.finding.code == 'filename_deviation'))

    def test_atomic_registration_sibling_failure_and_partial_execution(self):
        self.batch(3)
        session = self.review()
        def registration(stage, job, ordinal):
            if stage == 'before_rename_registration' and ordinal == 1:
                raise PermissionError('second registration')
        self.service.checkpoint = registration
        with self.assertRaises(PermissionError):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone()[0], 0)
        self.service.checkpoint = None
        result = self.register(session)
        failing = result['items'][0]['job_id']
        def failure(stage, job, ordinal):
            if stage == 'before_effect' and job == failing and ordinal == 0:
                raise PermissionError('one sibling')
        self.service.checkpoint = failure
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['state'], 'partially_completed_batch', result)
        self.assertEqual(result['counts']['completed'], 2)
        self.assertEqual(result['counts']['recovery_required'], 1)

    def test_settings_metadata_classification_and_file_changes_stale(self):
        for sql in ("UPDATE volumes SET title='changed'", "UPDATE volumes SET special_version_locked=1",
                    "UPDATE config SET value='Changed {issue_number}' WHERE key='file_naming'"):
            with self.subTest(sql=sql):
                session = self.review()
                self.db.execute('SAVEPOINT disposable')
                self.db.execute(sql)
                # Use same-connection guard for DB visibility; apply refuses stale.
                self.db.execute('RELEASE disposable')
                with self.assertRaisesRegex(RenameReviewError, 'stale'):
                    self.register(session)
                # A new report/worklist is needed for the next intentionally changed state.
                self.worklist = self.fixture.assign(self.fixture.review(), Action.RENAME)

    def test_same_publication_duplicate_targets_block(self):
        self.fixture.fixture.comic('second-copy.cbz')
        worklist = self.fixture.review()
        self.worklist = self.fixture.service.revise(worklist.id, worklist.revision,
            tuple(Edit(i.finding.id, True, False, Action.RENAME) for i in worklist.items if i.finding.code == 'filename_deviation'))
        session = self.review()
        self.assertTrue(session.collisions_json != '[]')
        with self.assertRaisesRegex(RenameReviewError, 'blocked'):
            self.register(session)
        one = self.service.revise(session.id, session.revision, (session.selected[0],))
        self.assertEqual(one.items[0].plan.target_path, session.items[0].plan.target_path)
        self.assertEqual(self.register(one)['total'], 1)

    def test_review_and_registration_diagnostic_100(self):
        self.batch(100)
        statements = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        self.db.set_trace_callback(statements.append)
        tracemalloc.start()
        with patch('sqlite3.connect', side_effect=traced):
            start = perf_counter()
            session = self.review()
            reviewed = perf_counter() - start
            review_selects = sum(s.lstrip().upper().startswith('SELECT') for s in statements)
            statements.clear()
            start = perf_counter()
            result = self.register(session)
            elapsed = perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.db.set_trace_callback(None)
        register_selects = sum(s.lstrip().upper().startswith('SELECT') for s in statements)
        self.assertEqual(result['total'], 100)
        self.assertEqual(len(result['items']), 50)
        self.assertLess(review_selects, 100)
        self.assertLess(register_selects, 800)
        print(f'Rename 100: review={reviewed:.3f}s/{review_selects} SELECTs registration={elapsed:.3f}s/{register_selects} SELECTs '
              f'peak={peak} review_bytes={len(repr(session).encode())}')

    def test_task_uses_only_registered_journal(self):
        session = self.review()
        task = BulkRenameTask(self.service, session.id, session.revision, session.digest,
                              origin=session.origin, selected=session.selected, confirmed=True)
        task.run()
        self.assertEqual(task.result['state'], 'completed')
        self.assertFalse(task.stop)

    def test_case_only_is_blocked_on_both_platforms(self):
        self.db.execute("UPDATE config SET value='WRONG' WHERE key IN ('file_naming','file_naming_empty')")
        self.db.commit()
        self.worklist = self.fixture.assign(self.fixture.review(), Action.RENAME)
        session = self.review()
        with self.assertRaisesRegex(RenameReviewError, 'blocked'):
            self.register(session)
        self.assertTrue(self.source.exists())

    def test_multi_issue_uses_direct_links_without_reassociation(self):
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,102,'2',2)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,2)')
        self.db.commit()
        self.worklist = self.fixture.assign(self.fixture.review(), Action.RENAME)
        session = self.review()
        self.assertIn('001 - 002', session.items[0].plan.target_path)
        before = self.db.execute('SELECT * FROM issues_files ORDER BY issue_id').fetchall()
        result = self.register(session)
        self.assertEqual(self.service.execute(self.db.cursor(), result['batch_id'])['state'], 'completed')
        self.assertEqual(before, self.db.execute('SELECT * FROM issues_files ORDER BY issue_id').fetchall())

    def test_noncontiguous_direct_links_do_not_invent_range(self):
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,102,'2',2)")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(3,1,103,'3',3)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,3)')
        self.db.commit()
        worklist = self.fixture.review()
        item = next(i for i in worklist.items if i.finding.code == 'naming_unavailable' and i.finding.file_id == 1)
        self.worklist = self.fixture.service.revise(worklist.id, worklist.revision, (Edit(item.finding.id, True, False, Action.RENAME),))
        with self.assertRaisesRegex(RenameReviewError, 'unavailable'):
            self.review()

    def test_source_disappearance_rejects_before_registration(self):
        session = self.review()
        self.source.unlink()  # Only this disposable test artifact.
        with self.assertRaisesRegex(RenameReviewError, 'stale'):
            self.register(session)

    def test_review_limit_does_not_silently_truncate(self):
        self.service.MAX_SESSIONS = 1
        self.review()
        with self.assertRaisesRegex(RenameReviewError, 'capacity'):
            self.review()
        with self.assertRaisesRegex(RenameReviewError, 'selection'):
            self.service._validate_selection(tuple(f'{n:064x}' for n in range(251)))

    def test_registered_classification_control_change_stales(self):
        session = self.review()
        result = self.register(session)
        self.db.execute('UPDATE volumes SET special_version_locked=1')
        self.db.commit()
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'failed')
        self.assertTrue(self.source.exists())

    def test_c2_coverage_is_preserved_not_a_naming_association(self):
        from backend.base.content_claims import ClaimKind, PublicationRef
        from backend.internals.content_claims import (apply_coverage,
                                                      claim_preview,
                                                      confirm_claim,
                                                      coverage_preview)
        folder = self.fixture.fixture.root / 'Source'
        folder.mkdir()
        self.db.execute("INSERT INTO volumes(id,title,root_folder,folder,metadata_provider,comicvine_id) VALUES(2,'Source',1,?,'comicvine',200)",
                        (str(folder),))
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(2,2,201,'1A',1,1)")
        self.db.commit()
        cursor = self.db.cursor()
        source = PublicationRef('comicvine', '201')
        preview = claim_preview(cursor, 1, source, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(cursor, 1, source, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(cursor, 1, 1, [claim])
        apply_coverage(cursor, 1, 1, [claim], preview['preview_token'])
        self.db.commit()
        self.worklist = self.fixture.assign(self.fixture.review(), Action.RENAME)
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in
                  ('issues_files', 'bibliographic_content_claims', 'file_content_coverage', 'valid_file_content_coverage')}
        session = self.review()
        self.assertEqual(len(session.items[0].plan.associations.before), 1)
        result = self.register(session)
        self.assertEqual(self.service.execute(cursor, result['batch_id'])['state'], 'completed')
        self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in before})

    def test_active_intake_blocks_without_cancellation(self):
        session = self.review()
        self.db.execute('''INSERT INTO acquisition_downloads
            (id,intent_digest,intent,client_id,client_instance,state,created_at,updated_at)
            VALUES('download','digest','{"volume_id":1,"issue_ids":[1]}','fixture','fixture','ambiguous','now','now')''')
        self.db.commit()
        with self.assertRaisesRegex(RenameReviewError, 'active_dependency'):
            self.register(session)
        self.assertEqual(self.db.execute("SELECT state FROM acquisition_downloads WHERE id='download'").fetchone()[0], 'ambiguous')

    def test_ten_job_registration_and_wrong_retry_identity(self):
        self.batch(10)
        session = self.review()
        start = perf_counter()
        result = self.register(session)
        print(f'Rename registration 10: {perf_counter() - start:.3f}s')
        self.assertEqual(result['total'], 10)
        with self.assertRaisesRegex(RenameReviewError, 'retry_identity'):
            self.service.register(self.db.cursor(), session.id, session.revision, session.digest,
                confirmed=True, origin=('wrong', 0, 'digest'), selected=session.selected)

    def test_actual_provider_switch_and_reverse_stale_existing_reviews(self):
        import asyncio

        from TProviderSwitchApply import remote

        from backend.features.provider_switch_review import \
            ProviderSwitchReviews
        from backend.implementations.metadata.switch_target import admit
        from backend.internals.provider_authority import capture
        first, reverse = self.review(), self.review()
        switches = ProviderSwitchReviews(task_observer=lambda _: ())
        cursor = self.db.cursor()
        for provider, parent, issue in (('metron', '700', 701), ('comicvine', '100', 101)):
            target = remote(provider, parent=parent, first=issue, count=1)
            async def acquire(reference):
                return admit(target, reference)
            switches.acquire = acquire
            session = asyncio.run(switches.create(cursor, 1, provider, parent))
            session = switches.revise(cursor, session.id, session.revision, {1: str(issue)})
            switches.apply(cursor, session.id, session.revision, session.preview.view()['mapping_digest'],
                           confirmed=True, expected_authority=capture(cursor, (1,))[1])
            with self.assertRaisesRegex(RenameReviewError, 'stale'):
                self.register(first if provider == 'metron' else reverse)
        self.assertEqual(capture(cursor, (1,))[1].generation, 2)
        self.assertEqual({r[0] for r in cursor.execute('SELECT provider FROM issue_external_ids WHERE issue_id=1')},
                         {'comicvine', 'metron'})
        self.assertTrue(self.source.exists())

    def test_reservation_conflict_rolls_back_registration(self):
        from backend.base.organization_job import OrganizationError
        from backend.features.organization_execution import _key
        session = self.review()
        self.db.execute('''INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at)
            VALUES('prior','prior','fixture','{"volume_id":1}','fixture','completed','now','now')''')
        self.db.execute('INSERT INTO organization_reservations(path_key,job_id) VALUES(?,?)', (_key(str(self.source)), 'prior'))
        self.db.commit()
        with self.assertRaises(OrganizationError):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT id FROM organization_jobs').fetchall(), [('prior',)])
        self.assertTrue(self.source.exists())

    def test_db_reconciliation_failure_recovers(self):
        session = self.review()
        result = self.register(session)
        def hook(stage, job, ordinal):
            if stage == 'before_db_commit' and ordinal == 1:
                raise sqlite3.OperationalError('injected reconciliation failure')
        self.service.checkpoint = hook
        result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'recovery_required')
        self.assertEqual(self.db.execute('SELECT filepath FROM files').fetchone()[0], str(self.source))
        self.assertTrue(Path(session.items[0].plan.target_path).exists())
        self.service.checkpoint = None
        self.assertEqual(self.service.execute(self.db.cursor(), result['batch_id'])['state'], 'completed')

    def test_batch_graph_blocks_chain_cycle_and_parent_child(self):
        from backend.implementations.maintenance_review import batch_collisions
        for effects, expected in (
            ((('a', '/a', '/b'), ('b', '/b', '/c')), 'source_target_dependency_or_cycle'),
            ((('a', '/a', '/b'), ('b', '/b', '/a')), 'source_target_dependency_or_cycle'),
            ((('a', '/a', '/b'), ('b', '/b', '/c'), ('c', '/c', '/a')), 'source_target_dependency_or_cycle'),
            ((('a', '/source', '/target'), ('b', '/other', '/target/child')), 'parent_child_collision')):
            self.assertIn(expected, {d['code'] for d in batch_collisions(effects, windows=False)})
        session = self.review()
        self.assertNotIn('HEALTH_SECRET_SENTINEL', repr(session))

    def test_durable_retry_does_not_require_filesystem_scope(self):
        session = self.review()
        result = self.register(session)
        self.service.execute(self.db.cursor(), result['batch_id'])
        self.service.delete(session.id)
        with patch('backend.features.bulk_rename.OrganizationExecutor', side_effect=AssertionError('retry must not open executor')):
            retry = self.register(session)
            task = BulkRenameTask(self.service, session.id, session.revision, session.digest,
                                  origin=session.origin, selected=session.selected, confirmed=True)
            task.run()
            self.assertEqual(task.result['state'], 'completed')
        self.assertEqual(retry['state'], 'completed')
        self.assertEqual(retry['total'], 1)

    def test_execution_evidence_limit_becomes_controlled_stale_result(self):
        session = self.review()
        result = self.register(session)
        with patch('backend.internals.rename_review.naming_evidence', side_effect=ValueError('rename_evidence_limit')):
            result = self.service.execute(self.db.cursor(), result['batch_id'])
        self.assertEqual(result['items'][0]['state'], 'failed')
        self.assertTrue(self.source.exists())

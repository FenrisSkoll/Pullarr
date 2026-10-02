"""Owned worklist -> revised duplicate intent -> durable quarantine batch."""

import sqlite3
import tracemalloc
from dataclasses import replace
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

import TDuplicateReview as duplicate_fixture

from backend.base.duplicate_review import (DuplicateAction, DuplicateChoice,
                                           DuplicateReviewError)
from backend.features.duplicate_quarantine import DuplicateQuarantine
from backend.features.duplicate_review import DuplicateReviews


class DuplicateQuarantineTests(TestCase):
    def setUp(self):
        self.fixture = duplicate_fixture.DuplicateReviewTests('test_three_copies_one_equivalence_class')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.health = self.fixture.db, self.fixture.health
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',66)")
        self.db.commit()
        self.service = DuplicateQuarantine(self.fixture.service)

    def prepare(self):
        session = self.fixture.review()
        session = self.fixture.service.revise(session.id, session.revision,
            (DuplicateChoice(session.groups[0].id, DuplicateAction.QUARANTINE, (1,)),))
        return self.service.prepare(self.db.cursor(), session.id, session.revision)

    def register(self, session, **kwargs):
        return self.service.register(self.db.cursor(), session.id, session.revision, session.digest,
            origin=session.origin, selected=tuple(g.id for g in session.groups), confirmed=True, **kwargs)

    def test_owned_review_registration_execution_and_durable_retry(self):
        before = tuple(self.db.iterdump()), self.health.filesystem()
        session = self.prepare()
        self.assertTrue(session.summary()['apply_available'])
        self.assertTrue(session.groups[0].summary()['apply_available'])
        proposed = session.detail(session.groups[0].id)['quarantine_execution']
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]['file_id'], 1)
        self.assertIn('.kapowarr-quarantine', proposed[0]['target'])
        proposed[0]['target'] = 'client-mutated detached view'
        self.assertNotIn('client-mutated', session.execution_json)
        self.assertEqual(before, (tuple(self.db.iterdump()), self.health.filesystem()))
        batch = self.register(session)
        self.assertEqual(len(batch['jobs']), 1)
        self.assertEqual(batch['jobs'][0]['state'], 'pending')
        self.assertTrue(self.fixture.paths[0].exists())
        result = self.service.execute(self.db.cursor(), batch['batch_id'])
        self.assertEqual(result['state'], 'completed')
        self.assertFalse(self.fixture.paths[0].exists())
        replacement = DuplicateReviews(self.fixture.fixture.service)
        self.service = DuplicateQuarantine(replacement)
        self.assertEqual(self.register(session), result)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (1,))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM quarantined_files').fetchone(), (1,))

    def test_confirmation_requires_exact_digest_and_explicit_confirmation(self):
        session = self.prepare()
        with self.assertRaises(DuplicateReviewError):
            self.register(replace(session, revision=session.revision + 1))
        with self.assertRaises(DuplicateReviewError):
            self.service.register(self.db.cursor(), session.id, session.revision, session.digest,
                origin=session.origin, selected=tuple(g.id for g in session.groups), confirmed=False)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (0,))

    def test_changed_source_and_domain_rejected_without_registration(self):
        session = self.prepare()
        self.fixture.paths[0].write_bytes(b'changed after executable review')
        with self.assertRaises(DuplicateReviewError):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (0,))

    def test_choice_edit_discards_prepared_targets(self):
        session = self.prepare()
        revised = self.fixture.service.revise(session.id, session.revision,
            (DuplicateChoice(session.groups[0].id, DuplicateAction.KEEP),))
        self.assertEqual(revised.execution_json, '[]')
        self.assertFalse(revised.summary()['apply_available'])
        with self.assertRaises(DuplicateReviewError):
            self.register(session)

    def test_ownership_losing_selection_blocked(self):
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,102,'2',2)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,2)')
        self.db.commit()
        with self.assertRaisesRegex(DuplicateReviewError, 'selection_blocked'):
            self.prepare()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (0,))

    def batch_review(self, count):
        for number in range(2, count + 1):
            path = self.health.comic(f'copy-{number}.cbz')
            path.write_bytes(self.fixture.paths[0].read_bytes())
        self.db.commit()
        session = self.fixture.review()
        session = self.fixture.service.revise(session.id, session.revision,
            (DuplicateChoice(session.groups[0].id, DuplicateAction.QUARANTINE, tuple(range(1, count + 1))),))
        return self.service.prepare(self.db.cursor(), session.id, session.revision)

    def test_maximum_batch_atomic_registration_and_late_rollback(self):
        session = self.batch_review(25)
        def fail(stage, job, ordinal):
            if stage == 'before_quarantine_registration' and ordinal == 24:
                raise RuntimeError('late registration failure')
        self.service.checkpoint = fail
        with self.assertRaisesRegex(RuntimeError, 'late registration'):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (0,))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone(), (0,))
        self.assertFalse((self.health.base / '.kapowarr-quarantine').exists())
        self.service.checkpoint = None
        start = perf_counter()
        result = self.register(session)
        self.assertEqual(len(result['jobs']), 25)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_reservations').fetchone(), (50,))
        print('8G maximum quarantine registration', dict(seconds=perf_counter() - start,
            mutations=25, review_bytes=len(session.execution_json),
            journal_bytes=self.db.execute('SELECT SUM(length(intent)) FROM organization_jobs').fetchone()[0]))

    def test_independent_batch_reports_partial_completion(self):
        session = self.batch_review(2)
        registered = self.register(session)
        first = registered['jobs'][0]['id']
        def fail(stage, job, ordinal):
            if stage == 'before_effect' and ordinal == 0 and job != first:
                raise PermissionError('fixture namespace failure')
        self.service.checkpoint = fail
        result = self.service.execute(self.db.cursor(), registered['batch_id'])
        self.assertEqual(result['state'], 'partially_completed_batch')
        self.assertEqual(sorted(j['state'] for j in result['jobs']), ['completed', 'recovery_required'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM quarantined_files').fetchone(), (1,))

    def test_journal_capability_is_visible_before_registration(self):
        with patch('backend.features.duplicate_quarantine.MAX_PAYLOAD', 1):
            session = self.prepare()
        self.assertFalse(session.summary()['apply_available'])
        self.assertIn('journal_intent_too_large', session.groups[0].blockers)
        with self.assertRaises(DuplicateReviewError):
            self.register(session)

    def test_mixed_keep_and_quarantine_group_retry_preserves_outcomes(self):
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,102,'2',2)")
        for number in (3, 4):
            path = self.health.comic(f'other-{number}.cbz')
            path.write_bytes(b'different exact equivalence class')
            self.db.execute('UPDATE files SET size=? WHERE id=?', (path.stat().st_size, number))
            self.db.execute('UPDATE issues_files SET issue_id=2 WHERE file_id=?', (number,))
        self.db.commit()
        session = self.fixture.review()
        choices = tuple(DuplicateChoice(g.id, DuplicateAction.QUARANTINE, (1,)) if 1 in g.file_ids
                        else DuplicateChoice(g.id, DuplicateAction.KEEP) for g in session.groups)
        session = self.fixture.service.revise(session.id, session.revision, choices)
        session = self.service.prepare(self.db.cursor(), session.id, session.revision)
        result = self.register(session)
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(len(result['groups']), 2)
        self.service = DuplicateQuarantine(DuplicateReviews(self.fixture.fixture.service))
        self.assertEqual(result, self.register(session))
        with self.assertRaises(DuplicateReviewError):
            self.service.register(self.db.cursor(), session.id, session.revision, session.digest,
                origin=session.origin, selected=(session.groups[0].id,), confirmed=True)

    def switch_service(self):
        from TProviderSwitchApply import SwitchApplyTests

        from backend.features.provider_switch_review import \
            ProviderSwitchReviews
        switching = SwitchApplyTests()
        switching.db, switching.cursor = self.db, self.db.cursor()
        switching.service = ProviderSwitchReviews(task_observer=lambda _: ())
        return switching

    def test_actual_provider_switch_and_aba_never_revive_review(self):
        from TProviderSwitchApply import remote

        from backend.internals.provider_authority import capture
        session = self.prepare()
        switching = self.switch_service()
        switching.apply(switching.review(remote('metron', count=1)))
        with self.assertRaises(DuplicateReviewError):
            self.register(session)
        switching.apply(switching.review(remote('comicvine', parent='100', first=101, count=1)))
        self.assertEqual(capture(self.db.cursor(), (1,))[1].generation, 2)
        with self.assertRaises(DuplicateReviewError):
            self.register(session)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM quarantined_files').fetchone(), (0,))

    def test_active_quarantine_blocks_switch_terminal_history_does_not(self):
        from TProviderSwitchApply import remote

        from backend.base.switch_review import SwitchReviewError
        session = self.prepare()
        result = self.register(session)
        switching = self.switch_service()
        switch = switching.review(remote('metron', count=1))
        with self.assertRaises(SwitchReviewError):
            switching.apply(switch)
        self.assertEqual(self.service.execute(self.db.cursor(), result['batch_id'])['state'], 'completed')
        switching.apply(switching.review(remote('metron', count=1)))
        self.assertEqual(self.db.execute('SELECT metadata_provider FROM volumes WHERE id=1').fetchone(), ('metron',))

    def test_non_destructive_confirmation_never_creates_fake_job(self):
        session = self.fixture.review()
        session = self.fixture.service.revise(session.id, session.revision,
            (DuplicateChoice(session.groups[0].id, DuplicateAction.KEEP),))
        result = self.register(session)
        self.assertEqual(result['state'], 'no_changes')
        self.assertEqual(result['jobs'], [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (0,))

    def test_trusted_task_uses_existing_executor_and_retry(self):
        from backend.features.duplicate_quarantine_tasks import \
            DuplicateQuarantineTask
        session = self.prepare()
        task = DuplicateQuarantineTask(self.service, session.id, session.revision, session.digest,
            origin=session.origin, selected=tuple(g.id for g in session.groups), confirmed=True)
        task.run()
        self.assertEqual(task.result['state'], 'completed')
        task.run()
        self.assertEqual(task.result['state'], 'completed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (1,))

    def test_task_hash_cancellation_leaves_no_registered_job(self):
        from backend.features.duplicate_quarantine_tasks import \
            DuplicateQuarantineTask
        session = self.prepare()
        task = DuplicateQuarantineTask(self.service, session.id, session.revision, session.digest,
            origin=session.origin, selected=tuple(g.id for g in session.groups), confirmed=True)
        task.stop = True
        with self.assertRaisesRegex(DuplicateReviewError, 'cancelled'):
            task.run()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (0,))

    def test_ten_registration_query_memory_and_hash_diagnostics(self):
        session = self.batch_review(10)
        selects = [0]
        original = sqlite3.connect
        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(lambda sql: selects.__setitem__(0,
                selects[0] + int(sql.lstrip().upper().startswith('SELECT'))))
            return connection
        tracemalloc.start()
        start = perf_counter()
        try:
            with patch('sqlite3.connect', side_effect=connect):
                registered = self.register(session)
            elapsed, peak = perf_counter() - start, tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(registered['jobs']), 10)
        self.assertLess(selects[0], 500)
        print('8G ten quarantine registrations', dict(seconds=elapsed, selects=selects[0], peak_bytes=peak,
              review_bytes=len(session.execution_json), reviewed_hash_bytes=session.hash_bytes))

    def test_maximum_distinct_publication_review_is_bounded(self):
        from dataclasses import asdict
        from zipfile import ZipFile

        from backend.base.library_health import HealthLevel
        from backend.internals.organization_jobs import canonical
        first = self.fixture.paths[0].read_bytes()
        second = None
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,102,'2',2)")
        for number in range(3, 257):
            path = self.health.comic(f'maximum-{number}.cbz')
            if number == 129:
                with ZipFile(path, 'a') as archive:
                    archive.writestr('002.jpg', b'another fixture page')
                second = path.read_bytes()
            path.write_bytes(first if number <= 128 else second)
            self.db.execute('UPDATE files SET size=? WHERE id=?', (path.stat().st_size, number))
            if number >= 129:
                self.db.execute('UPDATE issues_files SET issue_id=2 WHERE file_id=?', (number,))
        self.db.commit()
        work, findings = self.fixture.worklist('same_publication_files', HealthLevel.INVENTORY)
        tracemalloc.start()
        start = perf_counter()
        try:
            session = self.fixture.service.create(work.id, work.revision, work.manifest_digest, findings)
            elapsed, peak = perf_counter() - start, tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(session.groups), 2)
        self.assertEqual(sum(len(g.file_ids) for g in session.groups), 256)
        self.assertEqual(session.hash_bytes, 0)
        print('8G maximum distinct-file review', dict(files=256, groups=2, seconds=elapsed,
            peak_bytes=peak, retained_bytes=len(canonical(asdict(session))), hash_bytes=session.hash_bytes))

    def test_production_boolean_converter_matches_dedicated_job_connection(self):
        from contextlib import closing

        from backend.internals.db import setup_db_adapters_and_converters
        from backend.internals.organization_jobs import canonical
        from backend.internals.quarantine_state import snapshot
        setup_db_adapters_and_converters()
        with closing(sqlite3.connect(self.health.database, detect_types=sqlite3.PARSE_DECLTYPES)) as converted:
            self.assertEqual(canonical(snapshot(converted.cursor(), (1, 2))),
                             canonical(snapshot(self.db.cursor(), (1, 2))))

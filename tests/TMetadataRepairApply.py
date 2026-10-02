"""Same-authority transactions, durable retry and disposable preservation."""

import sqlite3
import time
import tracemalloc
from unittest import TestCase
from unittest.mock import patch

import TMetadataRepairReview as review_fixture

from backend.base.maintenance_review import Action
from backend.base.metadata_repair import Field, FieldSelection, RepairError
from backend.features.metadata_repair import MetadataRepairReviews
from backend.features.metadata_repair_tasks import (RepairConfirmation,
                                                    apply_batch)
from backend.internals.db import SCHEMA_64
from backend.internals.db_migration import _migrate_metadata_repair
from backend.internals.metadata_repair_history import field_page, page


class MetadataRepairApplyTests(TestCase):
    def setUp(self):
        self.fixture = review_fixture.MetadataRepairReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.cursor, self.service = self.fixture.db, self.fixture.cursor, self.fixture.service
        self.service.task_observer = lambda _: ()

    def review(self, selections=None):
        session = self.fixture.create()
        return self.service.revise(self.cursor, session.id, 0, selections or (
            FieldSelection('volume', 1, Field.TITLE), FieldSelection('issue', 1, Field.TITLE),
            FieldSelection('issue', 1, Field.FACTS)))

    def apply(self, session):
        return self.service.apply(self.cursor, session.id, session.revision, session.digest,
                                  confirmed=True, expected_authority=session.authority)

    def test_selected_only_receipt_preservation_and_retry(self):
        session = self.review((FieldSelection('volume', 1, Field.TITLE),))
        before = self.fixture.fixture.fixture.filesystem()
        links = self.db.execute('SELECT * FROM issues_files').fetchall()
        result = self.apply(session)
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(self.db.execute('SELECT title,year,metadata_provider,authority_generation FROM volumes').fetchone(),
                         ('Target comicvine', 2020, 'comicvine', 0))
        self.assertEqual(self.db.execute('SELECT * FROM issues_files').fetchall(), links)
        self.assertEqual(before, self.fixture.fixture.fixture.filesystem())
        self.assertEqual(len(page(self.cursor, 1)), 1)
        detail = field_page(self.cursor, result['id'])
        self.assertEqual(detail[0]['before_value'], 'Example')
        self.assertEqual(detail[0]['after_value'], 'Target comicvine')
        before_retry = tuple(self.db.iterdump())
        # Durable lookup precedes transient-session lookup after restart/expiry.
        restarted = MetadataRepairReviews(self.fixture.fixture.service, task_observer=lambda _: ())
        retry = restarted.apply(self.cursor, session.id, session.revision, session.digest,
                                confirmed=True, expected_authority=session.authority)
        self.assertEqual(retry['id'], result['id'])
        self.assertEqual(retry['state'], 'already_applied')
        self.assertEqual(tuple(self.db.iterdump()), before_retry)
        with self.assertRaisesRegex(RepairError, 'retry_identity'):
            restarted.apply(self.cursor, session.id, session.revision + 1, session.digest,
                            confirmed=True, expected_authority=session.authority)

    def test_rollback_stages(self):
        session = self.review()
        before = tuple(self.db.iterdump()), self.fixture.fixture.fixture.filesystem()
        for checkpoint in ('before_mutation', 'volume_fields', 'issue_fields', 'canonical_facts',
                           'classification', 'receipt_details', 'before_commit'):
            with self.subTest(checkpoint=checkpoint):
                def fault(stage):
                    if stage == checkpoint:
                        raise RuntimeError('injected_repair_failure')
                self.service.fault_hook = fault
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    self.apply(session)
                self.assertEqual(before, (tuple(self.db.iterdump()), self.fixture.fixture.fixture.filesystem()))
                self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_stale_noop_and_task_blocker(self):
        session = self.fixture.create()
        self.assertEqual(self.apply(session)['state'], 'no_changes')
        self.assertEqual(page(self.cursor, 1), [])
        session = self.service.revise(self.cursor, session.id, 0, (FieldSelection('volume', 1, Field.TITLE),))
        self.service.task_observer = lambda _: ('busy',)
        with self.assertRaisesRegex(RepairError, 'active_process'):
            self.apply(session)
        self.service.task_observer = lambda _: ()
        self.db.execute('UPDATE volumes SET authority_generation=2 WHERE id=1')
        self.db.commit()
        with self.assertRaisesRegex(RepairError, 'stale'):
            self.apply(session)
        self.assertEqual(page(self.cursor, 1), [])

    def test_no_provider_or_filesystem_writer_in_apply(self):
        session = self.review()
        with patch('socket.socket', side_effect=AssertionError('network')), \
                patch('backend.implementations.file_matching.scan_files', side_effect=AssertionError('scan')), \
                patch('backend.implementations.comicinfo_archive.write_comicinfo', side_effect=AssertionError('archive')):
            self.assertEqual(self.apply(session)['state'], 'applied')

    def test_metron_and_gcd_fields_apply_without_identity_changes(self):
        for provider in ('metron', 'gcd'):
            with self.subTest(provider=provider):
                f = review_fixture.MetadataRepairReviewTests()
                f.setUp()
                try:
                    f.db.execute('UPDATE volumes SET metadata_provider=? WHERE id=1', (provider,))
                    f.db.execute('INSERT INTO volume_external_ids VALUES(1,?,?,?,NULL)', (provider, '100', 'fixture'))
                    f.db.execute('INSERT INTO issue_external_ids VALUES(1,?,?,?)', (provider, '101', 'fixture'))
                    f.db.commit()
                    f.worklist = f.fixture.assign(f.fixture.review(), Action.METADATA)
                    session = f.create()
                    choices = tuple(r.selection for r in session.fields if r.support == 'supported')
                    session = f.service.revise(f.cursor, session.id, 0, choices)
                    f.service.task_observer = lambda _: ()
                    result = f.service.apply(f.cursor, session.id, session.revision, session.digest,
                                             confirmed=True, expected_authority=session.authority)
                    self.assertEqual(result['state'], 'applied')
                    self.assertEqual(f.db.execute('SELECT authority_generation FROM volumes').fetchone()[0], 0)
                    self.assertEqual(f.db.execute('SELECT COUNT(*) FROM issue_external_ids').fetchone()[0], 2)
                    if provider == 'gcd':
                        self.assertEqual(f.db.execute('SELECT volume_number FROM volumes').fetchone()[0], 1)
                        self.assertIsNone(f.db.execute('SELECT date FROM issues').fetchone()[0])
                        self.assertEqual(f.db.execute('SELECT precision FROM issue_date_facts').fetchone()[0], 'month')
                        self.assertEqual(f.db.execute('SELECT provider FROM issue_bibliography').fetchone()[0], 'gcd')
                finally:
                    f.doCleanups()

    def test_batch_duplicates_partial_failure_and_durable_retry(self):
        session = self.review()
        command = RepairConfirmation('database', session.id, session.revision, session.digest, session.authority)
        result = apply_batch(self.cursor, self.service, None, (command, command), confirmed=True)
        self.assertEqual(result['state'], 'blocked_or_failed')
        self.assertEqual(page(self.cursor, 1), [])
        missing = RepairConfirmation('database', 'missing', 0, '0' * 64, session.authority)
        result = apply_batch(self.cursor, self.service, None, (command, missing), confirmed=True)
        self.assertEqual(result['state'], 'partially_completed_batch')
        self.assertEqual(result['outcomes'][0]['state'], 'applied')
        result = apply_batch(self.cursor, self.service, None, (command,), confirmed=True)
        self.assertEqual(result['outcomes'][0]['state'], 'already_applied')

    def test_additive_migration_reopen_and_rollback(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript(SCHEMA_64)
        db.execute("INSERT OR REPLACE INTO config VALUES('database_version',64)")
        db.commit()
        before = tuple(db.iterdump())
        class FailCursor:
            def execute(self, sql, params=()):
                if 'CREATE TABLE metadata_repair_fields' in sql:
                    raise RuntimeError('injected migration failure')
                return db.execute(sql, params)
        with patch('backend.internals.db_migration.get_db', return_value=FailCursor()):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                _migrate_metadata_repair()
        self.assertEqual(tuple(db.iterdump()), before)
        with patch('backend.internals.db_migration.get_db', side_effect=db.cursor):
            _migrate_metadata_repair()
            _migrate_metadata_repair()
        self.assertEqual(db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 65)
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_large_review_apply_diagnostics(self):
        import TProviderSwitchApply as provider_fixture

        from backend.implementations.metadata.switch_target import admit
        for count in (100, 1000):
            f = review_fixture.MetadataRepairReviewTests()
            f.setUp()
            try:
                f.db.executemany('''INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number)
                    VALUES(?,1,?,?,?)''', ((i, 100 + i, str(i), i) for i in range(2, count + 1)))
                f.db.commit()
                f.worklist = f.fixture.assign(f.fixture.review(), Action.METADATA)
                requests = []
                async def acquire(reference):
                    requests.append(reference)
                    return admit(provider_fixture.remote('comicvine', parent='100', first=101, count=count), reference)
                f.service.acquire = acquire
                f.service.task_observer = lambda _: ()
                reads = []
                f.db.set_trace_callback(lambda sql: reads.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
                tracemalloc.start()
                started = time.perf_counter()
                session = f.create()
                session = f.service.revise(f.cursor, session.id, 0,
                    tuple(FieldSelection('issue', i, Field.TITLE) for i in range(1, count + 1)))
                review_time = time.perf_counter() - started
                review_reads = len(reads)
                reads.clear()
                started = time.perf_counter()
                result = f.service.apply(f.cursor, session.id, session.revision, session.digest,
                                         confirmed=True, expected_authority=session.authority)
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertEqual(result['field_count'], count)
                self.assertEqual(len(requests), 1)
                self.assertLess(len(reads), 150)
                print(f'Repair {count}: review {review_reads} cursor SELECTs/{review_time:.3f}s; '
                      f'apply {len(reads)} SELECTs/{elapsed:.3f}s; peak {peak}; fixture acquisitions {len(requests)}')
            finally:
                if tracemalloc.is_tracing():
                    tracemalloc.stop()
                f.doCleanups()

    def test_locked_classification_and_populated_coverage_preserved(self):
        import TProviderSwitchApply as provider_fixture

        from backend.base.content_claims import ClaimKind, PublicationRef
        from backend.implementations.metadata.switch_target import admit
        from backend.internals.content_claims import (apply_coverage,
                                                      claim_preview,
                                                      confirm_claim,
                                                      coverage_preview)
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number) VALUES(2,1,102,'2')")
        self.db.execute('UPDATE volumes SET special_version_locked=1 WHERE id=1')
        reference = PublicationRef('comicvine', '102')
        preview = claim_preview(self.cursor, 1, reference, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(self.cursor, 1, reference, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(self.cursor, 1, 1, [claim])
        apply_coverage(self.cursor, 1, 1, [claim], preview['preview_token'])
        self.db.commit()
        self.fixture.worklist = self.fixture.fixture.assign(self.fixture.fixture.review(), Action.METADATA)
        async def acquire(reference):
            return admit(provider_fixture.remote('comicvine', parent='100', first=101, count=2), reference)
        self.service.acquire = acquire
        session = self.review((FieldSelection('volume', 1, Field.TITLE),))
        tables = ('bibliographic_content_claims', 'file_content_coverage', 'issues_files')
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables}
        classification = self.db.execute('SELECT special_version,special_version_locked FROM volumes').fetchone()
        result = self.apply(session)
        self.assertEqual(result['classification_action'], 'preserve_locked_value_and_receipt')
        self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables})
        self.assertEqual(classification, self.db.execute('SELECT special_version,special_version_locked FROM volumes').fetchone())

    def test_bibliography_failure_rolls_back(self):
        self.db.execute("UPDATE volumes SET metadata_provider='gcd' WHERE id=1")
        self.db.execute("INSERT INTO volume_external_ids VALUES(1,'gcd','100','fixture',NULL)")
        self.db.execute("INSERT INTO issue_external_ids VALUES(1,'gcd','101','fixture')")
        self.db.commit()
        self.fixture.worklist = self.fixture.fixture.assign(self.fixture.fixture.review(), Action.METADATA)
        session = self.review((FieldSelection('volume', 1, Field.BIBLIOGRAPHY),))
        before = tuple(self.db.iterdump())
        def fail(stage):
            if stage == 'bibliography':
                raise RuntimeError('injected bibliography')
        self.service.fault_hook = fail
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            self.apply(session)
        self.assertEqual(tuple(self.db.iterdump()), before)

"""Real SQLite claims, migration, derived ownership and restart checkpoints."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from backend.implementations.release_scoring import evaluate_release
from backend.internals.db import SCHEMA_58 as DB_SCHEMA
from backend.internals.db_migration import _migrate_wanted_automation
from backend.internals.wanted import WantedConflict, WantedStore
from backend.internals.wanted_schema import SCHEMA, STATEMENTS
from tests.Tbackend.features.release_scoring import candidate, target


class WantedPersistenceTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='kapowarr-wanted-')
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / 'wanted.db')
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA.removesuffix(SCHEMA))
        self.db.execute("INSERT INTO config VALUES('database_version',57)")
        self.db.execute("INSERT INTO root_folders VALUES(1,'/library')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,monitored) VALUES(1,101,'Batman',1,1)")
        self.db.executemany('''INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored)
            VALUES(?,1,?,?,?,1)''', [(i, 200 + i, str(i), i) for i in range(1, 7)])
        self.db.commit()

    def migrate(self, *, ownership=True):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_wanted_automation()
        self.db.commit()
        if ownership:
            # Functional Wanted tests use the current ownership contract;
            # the historical 57->58 migration parity test remains frozen.
            from backend.internals.content_schema import \
                SCHEMA as CONTENT_SCHEMA
            from backend.internals.quarantine_schema import \
                SCHEMA as QUARANTINE_SCHEMA
            from backend.internals.reprint_schema import SCHEMA as GRAPH_SCHEMA
            self.db.executescript(GRAPH_SCHEMA + CONTENT_SCHEMA + QUARANTINE_SCHEMA)

    def store(self):
        store = WantedStore(self.path, clock=lambda: 1000)
        self.addCleanup(store.close)
        return store

    def test_fresh_upgrade_reopen_repeat(self):
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in
                  ('volumes', 'issues', 'volume_external_ids', 'issue_external_ids')}
        self.migrate(ownership=False)
        self.migrate(ownership=False)
        self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in before})
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(DB_SCHEMA)
            query = "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"
            self.assertEqual(dict(fresh.execute(query)), dict(self.db.execute(query)))
        self.assertEqual(self.store().db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 58)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_migration_rollback(self):
        before = tuple(self.db.iterdump())
        with patch('backend.internals.wanted_schema.STATEMENTS', (*STATEMENTS[:2], 'INVALID SQL')):
            with self.assertRaises(sqlite3.OperationalError):
                self.migrate()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_wanted_is_monitored_and_unowned(self):
        self.migrate()
        self.db.execute('UPDATE issues SET monitored=0 WHERE id=1')
        self.db.execute("INSERT INTO files VALUES(1,'/library/comic.cbz',10)")
        self.db.execute('INSERT INTO issues_files VALUES(1,2,0)')
        self.db.commit()
        self.assertEqual([r['id'] for r in self.store().due(limit=10)], [3, 4, 5, 6])

    def test_collected_ownership_closes_reservation_without_losing_acquisition_history(self):
        from backend.base.content_claims import ClaimKind, PublicationRef
        from backend.internals.content_claims import (apply_coverage,
                                                      claim_preview,
                                                      confirm_claim,
                                                      coverage_preview, retire,
                                                      retirement_preview)
        self.migrate()
        store = self.store()
        decision = self.reserve(store)
        store.db.execute("INSERT INTO wanted_acquisitions VALUES(?,'direct_download','still-tracked')", (decision,))
        store.db.execute("INSERT INTO files VALUES(1,'/library/collection.cbz',10)")
        store.db.execute('INSERT INTO issues_files VALUES(1,6,0)')
        cursor = store.db.cursor()
        ref = PublicationRef('comicvine', '205')
        preview = claim_preview(cursor, 6, ref, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(cursor, 6, ref, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(cursor, 6, 1, [claim])
        apply_coverage(cursor, 6, 1, [claim], preview['preview_token'])
        store.reconcile_ownership()
        self.assertEqual(store.db.execute('SELECT active,closed_reason FROM wanted_reservations WHERE decision_id=?',
                                         (decision,)).fetchone()[:], (0, 'owned'))
        self.assertEqual(store.db.execute('SELECT acquisition_id FROM wanted_acquisitions WHERE decision_id=?',
                                         (decision,)).fetchone()[0], 'still-tracked')
        self.assertFalse(store.eligible((5,)))
        preview = retirement_preview(cursor, claim)
        retire(cursor, claim, preview['preview_token'])
        self.assertTrue(store.eligible((5,)))

    def reserve(self, store):
        run = store.begin_search(target(), 'automatic_missing')
        return store.reserve(run, evaluate_release(target(), candidate()))

    def test_second_connection_cannot_reserve_same_issue(self):
        self.migrate()
        first, second = self.store(), self.store()
        self.reserve(first)
        with self.assertRaises(WantedConflict):
            self.reserve(second)
        self.assertNotIn(5, [r['id'] for r in second.due(limit=10)])

    def test_importing_filter_uses_same_authoritative_intake_as_row(self):
        from backend.features.wanted_status import wanted_rows
        self.migrate()
        store = self.store()
        decision = self.reserve(store)
        with store.transaction():
            store.db.execute("INSERT INTO wanted_acquisitions VALUES(?,'direct_download','completion')", (decision,))
            store.db.execute('''INSERT INTO acquisition_intakes
                (id,kind,download_id,completion,completion_digest,rename,auto_apply,created_at,updated_at)
                VALUES('intake','direct_download','completion','{}','fixture',0,1,'now','now')''')
        rows = wanted_rows(store, state='importing')
        self.assertEqual([r['id'] for r in rows], [5])
        self.assertEqual(rows[0]['acquisitions'][0]['intake_id'], 'intake')
        self.assertEqual(wanted_rows(store, state='downloading'), [])

    def test_selected_crash_can_release_but_grabbing_crash_holds(self):
        self.migrate()
        store = self.store()
        first = self.reserve(store)
        store.recover_claims()
        self.assertTrue(store.eligible((5,)))
        second = self.reserve(store)
        store.transition(second, 'grabbing')
        store.recover_claims()
        self.assertFalse(store.eligible((5,)))
        self.assertEqual(store.db.execute('SELECT state FROM wanted_decisions WHERE id=?', (first,)).fetchone()[0], 'abandoned')
        self.assertEqual(store.db.execute('SELECT state FROM wanted_decisions WHERE id=?', (second,)).fetchone()[0], 'review')

    def test_ownership_ack_after_crash_and_later_removal(self):
        self.migrate()
        store = self.store()
        decision = self.reserve(store)
        store.transition(decision, 'grabbing')
        store.transition(decision, 'tracking', acquisition_id='remote')
        self.assertFalse(store.eligible((5,)))
        self.db.execute("INSERT INTO files VALUES(1,'/library/comic.cbz',10)")
        self.db.execute('INSERT INTO issues_files VALUES(1,5,0)')
        self.db.commit()
        store.reconcile_ownership()
        self.assertEqual(store.db.execute('SELECT state FROM wanted_decisions WHERE id=?', (decision,)).fetchone()[0], 'satisfied')
        self.db.execute('DELETE FROM issues_files WHERE issue_id=5')
        self.db.commit()
        self.assertTrue(store.eligible((5,)))

    def test_no_results_backoff_not_wanted_truth(self):
        self.migrate()
        store = self.store()
        run = store.begin_search(target(), 'automatic_missing')
        store.finish_search(run, 'no_acceptable_release')
        self.assertNotIn(5, [r['id'] for r in store.due(limit=10)])
        self.assertTrue(store.eligible((5,)))
        self.assertIn(5, [r['id'] for r in store.due(limit=10, ignore_cooldown=True)])

    def test_unmonitor_does_not_cancel_active_receipt(self):
        self.migrate()
        store = self.store()
        decision = self.reserve(store)
        self.db.execute('UPDATE issues SET monitored=0 WHERE id=5')
        self.db.commit()
        store.reconcile_ownership()
        self.assertEqual(store.db.execute('SELECT active FROM wanted_reservations WHERE decision_id=?', (decision,)).fetchone()[0], 1)

    def test_invalid_transition_no_completed_replay(self):
        self.migrate()
        store = self.store()
        decision = self.reserve(store)
        store.transition(decision, 'abandoned')
        with self.assertRaises(WantedConflict):
            store.transition(decision, 'grabbing')

    def test_additive_migration_preserves_existing_component_receipts(self):
        self.db.execute("INSERT INTO acquisition_downloads(id,intent_digest,intent,client_id,client_instance,state,created_at,updated_at) VALUES('sab','digest','{}','client','instance','ambiguous','date','date')")
        self.db.execute("INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at) VALUES('job','plan','executor','{}','digest','recovery_required','date','date')")
        self.db.execute("INSERT INTO organization_events(job_id,created_at,event,detail) VALUES('job','date','checkpoint','{}')")
        self.db.execute("INSERT INTO acquisition_intakes(id,kind,download_id,completion,completion_digest,rename,auto_apply,created_at,updated_at) VALUES('intake','sabnzbd','sab','{}','digest',0,0,'date','date')")
        self.db.execute("INSERT INTO monitor_roots(root_id,path) VALUES(1,'/library')")
        self.db.execute("INSERT INTO download_queue(volume_id,client_type,download_link,source_type,source_name) VALUES(1,'direct','https://fixture.invalid','direct','fixture')")
        self.db.execute("INSERT INTO download_history(volume_id,downloaded_at,success) VALUES(1,1,1)")
        tables = ('acquisition_downloads', 'acquisition_intakes', 'organization_jobs', 'organization_events', 'monitor_roots', 'download_queue', 'download_history')
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables}
        self.db.commit()
        self.migrate()
        self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables})

    def test_scheduler_due_query_count_is_constant_for_large_backlog(self):
        from time import perf_counter
        self.migrate()
        self.db.executemany('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(?,1,?,?,?,1)',
            ((i, 200 + i, str(i), i) for i in range(7, 1001)))
        self.db.commit()
        store = self.store()
        receipts = []
        for count in (100, 1000):
            queries = []
            store.db.set_trace_callback(queries.append)
            started = perf_counter()
            rows = store.due(limit=count)
            elapsed = perf_counter() - started
            store.db.set_trace_callback(None)
            self.assertEqual(len(rows), count)
            self.assertEqual(len(queries), 1)
            receipts.append((count, len(queries), round(elapsed, 5)))
        print('Wanted targets / SELECTs / seconds:', receipts)

    def test_explicit_request_off_mode_does_not_starve_behind_other_due_targets(self):
        self.migrate()
        store = self.store()
        store.request_search(1, 5)
        self.assertEqual(store.due(limit=1, requested_only=True)[0]['id'], 5)

    def test_source_rate_limit_pauses_other_targets_without_source_calls(self):
        from types import SimpleNamespace

        from Tbackend.features.release_search import config

        from backend.features.wanted_automation import WantedAutomation

        self.migrate()
        source = config()
        searches = SimpleNamespace(nzb_loader=lambda: (source,), ddl_loader=lambda: {})
        service = WantedAutomation(self.path, searches=searches, clock=lambda: 1000)
        self.addCleanup(service.close)
        service.source_backoff(SimpleNamespace(source_receipts={'ddl': None,
            'nzb': [{'source': source.namespace, 'errors': ['rate_limited'], 'retry_after': 1200}]}))
        self.assertTrue(service.sources_paused())
        service.store.clock = lambda: 2201
        self.assertFalse(service.sources_paused())

    def test_typed_corrupted_target_backs_off_and_does_not_starve_next_target(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from backend.features.wanted_automation import WantedAutomation
        from backend.implementations.direct_download_source import DDLError

        self.migrate()
        searches = SimpleNamespace(target_loader=Mock(side_effect=DDLError('target_unavailable')))
        service = WantedAutomation(self.path, searches=searches, clock=lambda: 1000)
        self.addCleanup(service.close)
        self.assertEqual(service.run_target(1, 1)['state'], 'target_unavailable')
        self.assertEqual(service.store.due(limit=1)[0]['id'], 2)

    def test_os_gate_refuses_a_second_worker(self):
        from backend.base.organization_job import OrganizationError
        from backend.implementations.organization_filesystem import \
            execution_gate

        with execution_gate(self.path + '.wanted'):
            with self.assertRaises(OrganizationError):
                with execution_gate(self.path + '.wanted'):
                    self.fail('Second worker entered')

"""Actual schema-58 rebuild, rollback, IDs, journals and bounded backfill."""

import sqlite3
import tracemalloc
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from backend.internals.db import SCHEMA_58, SCHEMA_59
from backend.internals.db_migration import _migrate_canonical_issue_facts
from backend.internals.issue_facts import load_records
from backend.internals.issue_facts_schema import STATEMENTS


class IssueFactsMigrationTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA_58)
        self.db.execute("INSERT INTO config VALUES('database_version',58)")
        self.db.execute("INSERT INTO root_folders VALUES(1,'/library')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder) VALUES(1,101,'Series',1)")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,date) VALUES(1,1,201,'1A',1.01,'2021-12-25')")
        self.db.execute("INSERT INTO files VALUES(1,'/library/comic.cbz',10)")
        self.db.execute('INSERT INTO issues_files VALUES(1,1,1)')
        self.db.execute("INSERT INTO acquisition_downloads(id,intent_digest,intent,client_id,client_instance,state,nzo_id,created_at,updated_at) VALUES('sab','digest','{}','sab','instance','completed','nzo','before','before')")
        self.db.execute("INSERT INTO acquisition_intakes(id,kind,download_id,completion,completion_digest,rename,auto_apply,created_at,updated_at) VALUES('intake','sabnzbd','sab','{}','digest',0,1,'before','before')")
        self.db.execute("INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at) VALUES('job','plan','version','{}','digest','recovery_required','before','before')")
        self.db.execute("INSERT INTO organization_steps(job_id,ordinal,kind,state) VALUES('job',0,'move','started')")
        self.db.execute("INSERT INTO monitor_roots(root_id,path) VALUES(1,'/library')")
        self.db.execute("INSERT INTO download_queue(id,volume_id,client_type,download_link,source_type,source_name) VALUES(1,1,'direct','fixture','ddl','fixture')")
        self.db.execute("INSERT INTO download_history(downloaded_at,issue_id) VALUES(1,1)")
        self.db.execute("INSERT INTO wanted_searches(id,volume_id,issue_ids,trigger,state,selection_policy,started_at) VALUES('run',1,'[1]','manual','done','v1',1)")
        self.db.execute("INSERT INTO wanted_schedule(issue_id,last_search) VALUES(1,'run')")
        self.db.execute("INSERT INTO wanted_decisions VALUES('decision','run','automatic','candidate','ddl','source','evaluation','score','selection','[]','[1]','title','tracking','direct_download',NULL,NULL,1,1)")
        self.db.execute("INSERT INTO wanted_reservations VALUES('decision',1,1,NULL)")
        self.db.execute("INSERT INTO organization_events(job_id,created_at,event,detail) VALUES('job','before','started','{}')")
        self.db.execute("INSERT INTO organization_reservations VALUES('/library/comic.cbz','job')")
        self.db.execute("INSERT INTO acquisition_artifacts(id,intake_id,path,stamp,stable_since,organization_job_id,updated_at) VALUES('artifact','intake','/incoming/comic.cbz','{}',1,'job','before')")
        self.db.execute("INSERT INTO indexer_clients(id,download_type,client_type,title,url) VALUES(1,1,'GetComics','Fixture','https://example.invalid')")
        self.db.commit()

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_canonical_issue_facts()

    def snapshot(self):
        names = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='config'")]
        return {name: self.db.execute('SELECT * FROM "' + name + '"').fetchall() for name in names}

    def test_preserve_every_existing_table_and_backfill_honestly(self):
        before = self.snapshot()
        self.migrate()
        self.assertEqual(before, {name: self.snapshot()[name] for name in before})
        value = load_records(self.db.cursor(), volume_id=1)[0]
        self.assertEqual(value.facts.number.provenance, 'legacy_mapped')
        self.assertEqual(value.facts.number.raw_label, '1A')
        self.assertIsNone(value.facts.number.numeric)
        self.assertEqual(value.legacy_number, 1.01)
        self.assertEqual(value.facts.dates[0].kind.value, 'legacy_selected_unknown')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_fresh_parity_repeat_and_reopen(self):
        self.migrate()
        before = self.snapshot()
        self.migrate()
        self.assertEqual(before, self.snapshot())
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(SCHEMA_59)
            query = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            self.assertEqual(fresh.execute(query).fetchall(), self.db.execute(query).fetchall())
            for (table,) in fresh.execute(query):
                for pragma in ('table_info', 'foreign_key_list', 'index_list'):
                    left = fresh.execute(f'PRAGMA {pragma}("{table}")').fetchall()
                    right = self.db.execute(f'PRAGMA {pragma}("{table}")').fetchall()
                    if pragma == 'index_list':
                        # SQLite sequence is creation order, not index semantics.
                        left, right = sorted(r[1:] for r in left), sorted(r[1:] for r in right)
                    self.assertEqual(left, right)
        with closing(sqlite3.connect(':memory:')) as reopened:
            self.db.backup(reopened)
            self.assertEqual(reopened.execute('SELECT COUNT(*) FROM issue_number_facts').fetchone()[0], 1)
            self.assertEqual(reopened.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 59)

    def test_real_file_close_reopen_and_repeat(self):
        self.migrate()
        with TemporaryDirectory(prefix='kapowarr-facts-reopen-') as folder:
            path = str(Path(folder) / 'facts.db')
            with closing(sqlite3.connect(path)) as saved:
                self.db.backup(saved)
            with closing(sqlite3.connect(path)) as reopened:
                with patch('backend.internals.db_migration.get_db', side_effect=reopened.cursor):
                    _migrate_canonical_issue_facts()
                self.assertEqual(load_records(reopened.cursor(), volume_id=1)[0].facts.number.raw_label, '1A')
                self.assertEqual(reopened.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_failure_after_backfill_restores_all_legacy_rows(self):
        before = tuple(self.db.iterdump())
        from backend.internals.issue_facts import write_facts
        def fail_after_write(*args):
            write_facts(*args)
            raise RuntimeError('injected backfill checkpoint')
        with patch('backend.internals.issue_facts.write_facts', side_effect=fail_after_write):
            with self.assertRaisesRegex(RuntimeError, 'injected backfill'):
                self.migrate()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_injected_failure_rolls_back_rebuild_and_facts(self):
        before = tuple(self.db.iterdump())
        with patch('backend.internals.issue_facts_schema.STATEMENTS', (*STATEMENTS[:1], 'INVALID SQL')):
            with self.assertRaises(sqlite3.OperationalError):
                self.migrate()
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_nullable_projection_without_fake_zero(self):
        self.migrate()
        self.db.execute("INSERT INTO issues(id,volume_id,issue_number,calculated_issue_number) VALUES(2,1,'[nn]',NULL)")
        self.assertIsNone(self.db.execute('SELECT calculated_issue_number FROM issues WHERE id=2').fetchone()[0])

    def test_large_batched_backfill(self):
        self.db.executemany('INSERT INTO issues(id,volume_id,issue_number,calculated_issue_number) VALUES(?,1,?,?)',
            ((i, str(i), i) for i in range(2, 10001)))
        self.db.commit()
        checkpoints = []
        self.db.set_trace_callback(lambda q: checkpoints.append(q) if q.startswith('SAVEPOINT') else None)
        tracemalloc.start()
        started = perf_counter()
        self.migrate()
        elapsed = perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.db.set_trace_callback(None)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issue_number_facts').fetchone()[0], 10000)
        self.assertEqual(checkpoints, ['SAVEPOINT issue_facts_59'])
        print(f'Canonical issue migration: 10000 rows, one savepoint, 750-row buffers, {elapsed:.3f}s, Python peak {peak} bytes')

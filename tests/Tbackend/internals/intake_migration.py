"""Schema 56→57 additive parity and rollback, using disposable databases."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from backend.internals.db import SCHEMA_58 as DB_SCHEMA
from backend.internals.db_migration import (DatabaseMigrationHandler,
                                            _migrate_acquisition_intake)
from backend.internals.intake_schema import SCHEMA, STATEMENTS
from backend.internals.wanted_schema import SCHEMA as WANTED_SCHEMA


class IntakeMigrationTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='kapowarr-intake-migration-')
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / 'migration.db')
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA.removesuffix(WANTED_SCHEMA).removesuffix(SCHEMA))
        self.db.execute("INSERT INTO config VALUES('database_version',56)")
        self.db.execute("INSERT INTO config VALUES('unrelated_setting','preserved')")
        self.db.commit()

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_acquisition_intake()

    def test_fresh_upgrade_repeat_reopen_parity(self):
        original = {row[0]: row[1] for row in self.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        self.migrate()
        self.migrate()
        self.db.commit()
        with closing(sqlite3.connect(self.path)) as reopened:
            self.assertEqual(reopened.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 57)
            self.assertEqual(reopened.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(reopened.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            migrated = dict(reopened.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        for name, definition in original.items():
            self.assertEqual(migrated[name], definition)
        fresh = sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        fresh.executescript(DB_SCHEMA.removesuffix(WANTED_SCHEMA))
        self.assertEqual(migrated, dict(fresh.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")))
        self.assertGreaterEqual(DatabaseMigrationHandler.latest_db_version(), 57)

    def test_injected_failure_rolls_back_tables_and_version(self):
        before = tuple(self.db.iterdump())
        with patch('backend.internals.intake_schema.STATEMENTS', (*STATEMENTS[:2], 'INVALID SQL')):
            with self.assertRaises(sqlite3.OperationalError):
                self.migrate()
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_existing_rows_unchanged(self):
        self.db.execute("INSERT INTO root_folders VALUES(1,'/library')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(1,101,'Preserved',1,'/library/Preserved')")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(1,1,201,'1A',1)")
        self.db.execute("INSERT INTO acquisition_downloads(id,intent_digest,intent,client_id,client_instance,state,nzo_id,created_at,updated_at) VALUES('sab-job','digest','{}','sab','instance','completed','nzo-1','before','before')")
        self.db.execute("INSERT INTO acquisition_download_events(job_id,created_at,state) VALUES('sab-job','before','completed')")
        self.db.execute("INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at) VALUES('journal','plan','version','{}','digest','recovery_required','before','before')")
        self.db.execute("INSERT INTO organization_steps(job_id,ordinal,kind,state) VALUES('journal',0,'move','started')")
        self.db.execute("INSERT INTO monitor_roots(root_id,path) VALUES(1,'/library')")
        self.db.execute("INSERT INTO download_queue(id,volume_id,client_type,download_link,source_type,source_name) VALUES(1,1,'direct','fixture','ddl','fixture')")
        self.db.execute("INSERT INTO download_history(downloaded_at,file_title) VALUES(1,'legacy')")
        tables = ('volumes', 'issues', 'volume_external_ids', 'issue_external_ids',
                  'acquisition_downloads', 'acquisition_download_events', 'organization_jobs',
                  'organization_steps', 'monitor_roots', 'download_queue', 'download_history')
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables}
        self.migrate()
        self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables})

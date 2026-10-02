"""Schema 53 -> 54 is additive, transactional and restart-safe."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from backend.internals.db import SCHEMA_58 as DB_SCHEMA
from backend.internals.db_migration import DatabaseMigrationHandler
from backend.internals.download_schema import SCHEMA as DOWNLOAD_SCHEMA
from backend.internals.intake_schema import SCHEMA as INTAKE_SCHEMA
from backend.internals.monitor_schema import SCHEMA as MONITOR_SCHEMA
from backend.internals.organization_schema import SCHEMA, STATEMENTS
from backend.internals.wanted_schema import SCHEMA as WANTED_SCHEMA


class OrganizationMigrationTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kapowarr-journal-migration-')
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'schema53.db')
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA.removesuffix(WANTED_SCHEMA).removesuffix(INTAKE_SCHEMA).removesuffix(DOWNLOAD_SCHEMA).removesuffix(MONITOR_SCHEMA).removesuffix(SCHEMA))
        self.db.execute("INSERT INTO config VALUES('database_version',53)")
        self.db.execute("INSERT INTO root_folders VALUES(1,'/disposable')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder) VALUES(1,123,'Fixture',1)")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(1,1,456,'1',1)")
        self.db.execute("INSERT INTO files VALUES(1,'/disposable/file.cbz',9)")
        self.db.execute('INSERT INTO issues_files VALUES(1,1,1)')
        self.db.execute("INSERT INTO task_history VALUES('test','Old task',1)")
        self.db.execute("INSERT INTO download_history(downloaded_at,file_title) VALUES(1,'Old download')")

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            DatabaseMigrationHandler.handlers[53]()

    def snapshot(self):
        return {table: self.db.execute('SELECT * FROM ' + table).fetchall() for table in (
            'volumes', 'issues', 'files', 'issues_files', 'volume_external_ids', 'issue_external_ids', 'task_history', 'download_history')}

    def test_additive_preserves_library_provider_and_history_rows(self):
        before = self.snapshot()
        self.migrate()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone(), (54,))
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_repeated_migration_and_reopen(self):
        self.migrate()
        self.migrate()
        with closing(sqlite3.connect(self.path)) as reopened:
            self.assertEqual(reopened.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (0,))

    def test_failure_rolls_back_tables_and_version(self):
        with patch('backend.internals.organization_schema.STATEMENTS', (*STATEMENTS[:2], 'INVALID SQL')):
            with self.assertRaises(sqlite3.Error):
                self.migrate()
        self.db.commit()  # Teardown must not accidentally commit partial DDL.
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone(), (53,))
        self.assertEqual(self.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'organization_%'").fetchall(), [])

    def test_fresh_and_upgraded_journal_schema_equal(self):
        self.migrate()
        fresh = sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        fresh.executescript(DB_SCHEMA)
        query = "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'organization_%' ORDER BY type,name"
        self.assertEqual(self.db.execute(query).fetchall(), fresh.execute(query).fetchall())

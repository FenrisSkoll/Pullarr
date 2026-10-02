"""Additive schema 60 preserves every existing domain and performs no fetch."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from Tbackend.internals import issue_facts_migration as previous

from backend.internals.bibliography_schema import STATEMENTS
from backend.internals.db import SCHEMA_60
from backend.internals.db_migration import _migrate_bibliography


class BibliographyMigrationTests(TestCase):
    def setUp(self):
        previous.IssueFactsMigrationTests.setUp(self)
        previous.IssueFactsMigrationTests.migrate(self)
        self.db.execute("INSERT INTO issue_variant_of VALUES(1,'gcd','2','gcd_rest')")
        self.db.commit()

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_bibliography()

    def test_all_prior_tables_preserved_and_no_backfill(self):
        before = previous.IssueFactsMigrationTests.snapshot(self)
        self.migrate()
        after = previous.IssueFactsMigrationTests.snapshot(self)
        self.assertEqual(before, {k: after[k] for k in before})
        self.assertEqual(after['issue_bibliography'], [])
        self.assertEqual(after['story_observations'], [])
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_fresh_repeat_reopen(self):
        self.migrate()
        before = tuple(self.db.iterdump())
        self.migrate()
        self.assertEqual(before, tuple(self.db.iterdump()))
        query = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(SCHEMA_60)
            # Historical issue rebuild changes CREATE quoting, not semantics.
            for kind, name, _, sql in fresh.execute(query):
                if kind == 'table':
                    for pragma in ('table_info', 'foreign_key_list'):
                        self.assertEqual(fresh.execute(f'PRAGMA {pragma}("{name}")').fetchall(),
                                         self.db.execute(f'PRAGMA {pragma}("{name}")').fetchall())
                else:
                    self.assertEqual(sql, self.db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0])
        with TemporaryDirectory(prefix='kapowarr-bibliography-') as folder:
            path = str(Path(folder) / 'db.sqlite')
            with closing(sqlite3.connect(path)) as saved:
                self.db.backup(saved)
            with closing(sqlite3.connect(path)) as reopened:
                with patch('backend.internals.db_migration.get_db', side_effect=reopened.cursor):
                    _migrate_bibliography()
                self.assertEqual(reopened.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 60)
            from backend.internals.download_jobs import DownloadStore
            from backend.internals.organization_jobs import JobStore
            for store_type in (DownloadStore, JobStore):
                store = store_type(path)
                store.close()

    def test_failure_rolls_back_tables_and_version(self):
        before = tuple(self.db.iterdump())
        with patch('backend.internals.bibliography_schema.STATEMENTS', (*STATEMENTS[:3], 'INVALID SQL')):
            with self.assertRaises(sqlite3.OperationalError):
                self.migrate()
        self.assertEqual(before, tuple(self.db.iterdump()))

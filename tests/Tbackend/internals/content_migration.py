"""61->62 is empty, additive, repeatable and statement-atomic on failure."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from Tbackend.internals import reprint_migration as previous

from backend.internals.content_schema import STATEMENTS
from backend.internals.db import SCHEMA_62
from backend.internals.db_migration import _migrate_content_coverage


class ContentMigrationTests(TestCase):
    def setUp(self):
        previous.GraphMigrationTests.setUp(self)
        previous.GraphMigrationTests.migrate(self)
        self.db.execute("INSERT INTO bibliographic_graph_snapshots VALUES('old','gcd','p','s','f',1,1,0,0,0,0,'d')")
        self.db.execute("INSERT INTO bibliographic_issue_refs VALUES('gcd','1','1','series','1','',0,'old')")
        self.db.commit()

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_content_coverage()

    def test_all_prior_tables_preserved_no_inferred_claims(self):
        names = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='config'")]
        before = {name: self.db.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}
        self.migrate()
        for name, values in before.items():
            self.assertEqual(values, self.db.execute(f'SELECT * FROM "{name}"').fetchall())
        for name in ('bibliographic_content_claims', 'bibliographic_content_claim_evidence', 'file_content_coverage'):
            self.assertEqual(self.db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0], 0)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_repeat_reopen_and_fresh_schema(self):
        self.migrate()
        before = tuple(self.db.iterdump())
        self.migrate()
        self.assertEqual(before, tuple(self.db.iterdump()))
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(SCHEMA_62)
            for (name,) in fresh.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')"):
                for pragma in ('table_info', 'foreign_key_list'):
                    self.assertEqual(fresh.execute(f'PRAGMA {pragma}("{name}")').fetchall(),
                                     self.db.execute(f'PRAGMA {pragma}("{name}")').fetchall())
        with TemporaryDirectory(prefix='kapowarr-content-migration-') as directory:
            path = Path(directory) / 'app.sqlite'
            with closing(sqlite3.connect(path)) as disk:
                self.db.backup(disk)
            with closing(sqlite3.connect(path)) as reopened:
                with patch('backend.internals.db_migration.get_db', side_effect=reopened.cursor):
                    _migrate_content_coverage()
                self.assertEqual(reopened.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 62)

    def test_injected_rollback_including_views_and_version(self):
        before = tuple(self.db.iterdump())
        with patch('backend.internals.content_schema.STATEMENTS', (*STATEMENTS, 'INVALID SQL')):
            with self.assertRaises(sqlite3.OperationalError):
                self.migrate()
        self.assertEqual(before, tuple(self.db.iterdump()))

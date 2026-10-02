"""No historical backfill or value/control rewrite during schema installation."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from Tbackend.internals import content_migration as previous

from backend.internals.classification_provenance import details
from backend.internals.classification_schema import STATEMENTS
from backend.internals.db import SCHEMA_63
from backend.internals.db_migration import _migrate_classification_provenance


class ClassificationMigrationTests(TestCase):
    def setUp(self):
        previous.ContentMigrationTests.setUp(self)
        previous.ContentMigrationTests.migrate(self)
        self.db.execute("""INSERT INTO bibliographic_content_claims VALUES('c','gcd','1','gcd','2',
            'complete_issue_containment','operator_confirmed','kapowarr-collected-content/v1',1,NULL,NULL)""")
        self.db.commit()

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_classification_provenance()

    def test_all_tables_values_and_ids_preserved_no_backfill(self):
        names = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='config'")]
        before = {name: self.db.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}
        self.migrate()
        for name, value in before.items():
            self.assertEqual(value, self.db.execute(f'SELECT * FROM "{name}"').fetchall())
        for name in ('classification_provenance', 'classification_evidence_receipts', 'classification_control_state', 'classification_state'):
            self.assertEqual(self.db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0], 0)
        for (local,) in self.db.execute('SELECT id FROM volumes').fetchall():
            self.assertEqual(details(self.db.cursor(), local)['provenance']['status'], 'unavailable')

    def test_fresh_repeat_reopen_triggers_identical(self):
        self.migrate()
        before = tuple(self.db.iterdump())
        self.migrate()
        self.assertEqual(tuple(self.db.iterdump()), before)
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(SCHEMA_63)
            for (name,) in fresh.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')"):
                for pragma in ('table_info', 'foreign_key_list'):
                    self.assertEqual(fresh.execute(f'PRAGMA {pragma}("{name}")').fetchall(),
                                     self.db.execute(f'PRAGMA {pragma}("{name}")').fetchall())
            query = "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            self.assertEqual(fresh.execute(query).fetchall(), self.db.execute(query).fetchall())
        with TemporaryDirectory(prefix='kapowarr-provenance-migration-') as folder:
            path = Path(folder) / 'db.sqlite'
            with closing(sqlite3.connect(path)) as disk:
                self.db.backup(disk)
            with closing(sqlite3.connect(path)) as disk:
                with patch('backend.internals.db_migration.get_db', side_effect=disk.cursor):
                    _migrate_classification_provenance()
                self.assertEqual(disk.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(disk.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_injected_rollback_of_schema_triggers_and_version(self):
        before = tuple(self.db.iterdump())
        with patch('backend.internals.classification_schema.STATEMENTS', (*STATEMENTS, 'INVALID SQL')):
            with self.assertRaises(sqlite3.OperationalError):
                self.migrate()
        self.assertEqual(tuple(self.db.iterdump()), before)

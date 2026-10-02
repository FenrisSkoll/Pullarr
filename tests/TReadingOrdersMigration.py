"""Schema 69 statement rollback, reopen and historical-domain preservation."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from backend.internals.collections import CollectionStore
from backend.internals.db import DB_SCHEMA, SCHEMA_68
from backend.internals.db_migration import _migrate_reading_orders
from backend.internals.reading_orders_schema import STATEMENTS


class ReadingOrderMigrationTests(TestCase):
    def test_every_statement_failure_atomic_and_fresh_parity(self):
        for failure in range(len(STATEMENTS)+1):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                path = Path(directory)/'fixture.db'
                db = sqlite3.connect(path)
                try:
                    db.execute('PRAGMA foreign_keys=ON'); db.executescript(SCHEMA_68)
                    db.execute("INSERT INTO config VALUES('database_version',68)")
                    db.commit()
                    CollectionStore(db.cursor()).create('Preserved')
                    db.commit(); before = list(db.iterdump()); cursor = db.cursor()
                    class Failing:
                        count = 0
                        def execute(self, sql, args=()):
                            if sql in STATEMENTS:
                                if self.count == failure:
                                    raise RuntimeError('injected statement failure')
                                self.count += 1
                            return cursor.execute(sql, args)
                    with patch('backend.internals.db_migration.get_db', return_value=Failing()):
                        if failure < len(STATEMENTS):
                            with self.assertRaises(RuntimeError):
                                _migrate_reading_orders()
                            self.assertEqual(before, list(db.iterdump()))
                        else:
                            _migrate_reading_orders(); _migrate_reading_orders()
                            self.assertEqual(db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 69)
                            fresh = sqlite3.connect(':memory:')
                            try:
                                fresh.executescript(DB_SCHEMA)
                                query = "SELECT name,sql FROM sqlite_master WHERE name LIKE 'reading_%' ORDER BY name"
                                self.assertEqual(db.execute(query).fetchall(), fresh.execute(query).fetchall())
                            finally:
                                fresh.close()
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
                finally:
                    db.close()
                with closing(sqlite3.connect(path)) as reopened:
                    self.assertEqual(reopened.execute('SELECT title FROM collection_nodes').fetchone()[0], 'Preserved')

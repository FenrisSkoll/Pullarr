"""Schema 64 installs no authority transition or guessed historical receipt."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from Tbackend.internals import classification_migration as previous

from backend.internals.db import SCHEMA_64 as DB_SCHEMA
from backend.internals.db_migration import _migrate_provider_switching
from backend.internals.provider_switch_schema import STATEMENTS


class ProviderSwitchMigrationTests(TestCase):
    def setUp(self):
        previous.ClassificationMigrationTests.setUp(self)
        previous.ClassificationMigrationTests.migrate(self)
        self.addCleanup(self.db.close)

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_provider_switching()

    def test_existing_domain_preserved_and_generations_zero(self):
        names = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='config'")]
        fields = {name: [r[1] for r in self.db.execute(f'PRAGMA table_info("{name}")')] for name in names}
        before = {name: self.db.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}
        self.migrate()
        for name, columns in fields.items():
            projection = ','.join('"' + column + '"' for column in columns)
            self.assertEqual(before[name], self.db.execute(f'SELECT {projection} FROM "{name}"').fetchall())
        self.assertTrue(self.db.execute('SELECT id FROM volumes').fetchall())
        self.assertEqual(self.db.execute('SELECT DISTINCT authority_generation FROM volumes').fetchall(), [(0,)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM provider_switch_receipts').fetchone()[0], 0)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_fresh_repeat_reopen(self):
        self.migrate()
        before = tuple(self.db.iterdump())
        self.migrate()
        self.assertEqual(before, tuple(self.db.iterdump()))
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(DB_SCHEMA)
            for (name,) in fresh.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')"):
                for pragma in ('table_info', 'foreign_key_list', 'index_list'):
                    actual = fresh.execute(f'PRAGMA {pragma}("{name}")').fetchall()
                    upgraded = self.db.execute(f'PRAGMA {pragma}("{name}")').fetchall()
                    if pragma == 'index_list':
                        # SQLite sequence is creation order, not index semantics.
                        actual = sorted(row[1:] for row in actual)
                        upgraded = sorted(row[1:] for row in upgraded)
                    self.assertEqual(actual, upgraded)
        with TemporaryDirectory(prefix='kapowarr-switch-migration-') as folder:
            path = Path(folder) / 'db.sqlite'
            with closing(sqlite3.connect(path)) as disk:
                self.db.backup(disk)
            with closing(sqlite3.connect(path)) as disk:
                with patch('backend.internals.db_migration.get_db', side_effect=disk.cursor):
                    _migrate_provider_switching()
                self.assertEqual(disk.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(disk.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_each_schema_failure_rolls_back_column_tables_and_version(self):
        before = tuple(self.db.iterdump())
        for position in range(len(STATEMENTS) + 1):
            with self.subTest(position=position):
                broken = STATEMENTS[:position] + ('INVALID SQL',) + STATEMENTS[position:]
                with patch('backend.internals.provider_switch_schema.STATEMENTS', broken):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.migrate()
                self.assertEqual(before, tuple(self.db.iterdump()))

    def test_wrong_start_version_rejected(self):
        self.db.execute("UPDATE config SET value=62 WHERE key='database_version'")
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, 'requires schema 63'):
            self.migrate()

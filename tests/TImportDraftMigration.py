"""Schema 73 is additive, fresh/install parity, and rolls back each statement."""

import sqlite3
from unittest import TestCase
from unittest.mock import patch

from backend.internals.db import DB_SCHEMA, SCHEMA_71
from backend.internals.db_migration import _migrate_import_drafts
from backend.internals.import_draft_schema import STATEMENTS
from backend.internals.torrent_schema import SCHEMA as TORRENT_SCHEMA


class ImportDraftMigrationTests(TestCase):
    def database(self, schema=SCHEMA_71 + TORRENT_SCHEMA):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript(schema)
        db.execute("INSERT INTO config VALUES('database_version',72)")
        db.commit()
        return db

    def test_schema_parity_and_no_existing_library_mutation(self):
        old = self.database()
        old.execute("INSERT INTO root_folders VALUES(1,'/synthetic/root')")
        old.commit()
        fresh = self.database(DB_SCHEMA)
        with patch('backend.internals.db_migration.get_db', side_effect=old.cursor):
            _migrate_import_drafts()
        sql = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        self.assertEqual(old.execute(sql).fetchall(), fresh.execute(sql).fetchall())
        self.assertEqual(old.execute('SELECT COUNT(*) FROM root_folders').fetchone()[0], 1)
        self.assertEqual(old.execute('SELECT COUNT(*) FROM library_import_drafts').fetchone()[0], 0)
        self.assertEqual(old.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_each_statement_failure_rolls_back(self):
        for index in range(len(STATEMENTS)):
            with self.subTest(index=index):
                db = self.database()
                before = list(db.iterdump())
                broken = STATEMENTS[:index] + ('INVALID FIXTURE SQL',) + STATEMENTS[index:]
                with patch('backend.internals.db_migration.get_db', side_effect=db.cursor), \
                     patch('backend.internals.import_draft_schema.STATEMENTS', broken), \
                     self.assertRaises(sqlite3.Error):
                    _migrate_import_drafts()
                self.assertEqual(list(db.iterdump()), before)

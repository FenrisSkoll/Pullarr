"""Schema72 parity and every-statement rollback with historical lineage."""
import sqlite3
from unittest import TestCase
from unittest.mock import patch

from backend.internals.db import DB_SCHEMA, SCHEMA_71
from backend.internals.db_migration import _migrate_expanded_clients
from backend.internals.torrent_schema import STATEMENTS


class ExpandedMigrationTests(TestCase):
    def database(self, schema=SCHEMA_71):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('PRAGMA foreign_keys=ON')
        connection.executescript(schema)
        connection.execute("INSERT INTO config VALUES('database_version',71)")
        for identifier, previous in (('a' * 32,None), ('b' * 32,'a' * 32)):
            connection.execute('''INSERT INTO acquisition_provenance
                (id,reason,state,release_title,source,claims,profile_snapshot,decision,supersedes,created_at,updated_at)
                VALUES(?,'legacy','imported','Legacy','unknown','{}','{}','{}',?,1,1)''', (identifier,previous))
        connection.commit()
        return connection

    def test_parity_and_lineage(self):
        old = self.database()
        fresh = self.database(DB_SCHEMA)
        with patch('backend.internals.db_migration.get_db', side_effect=old.cursor):
            _migrate_expanded_clients()
        sql = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        self.assertEqual(old.execute(sql).fetchall(),fresh.execute(sql).fetchall())
        self.assertEqual(old.execute("SELECT supersedes FROM acquisition_provenance WHERE id=?", ('b' * 32,)).fetchone()[0], 'a' * 32)
        self.assertEqual(old.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(old.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(int(old.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0]), 72)

    def test_every_statement_rolls_back(self):
        for index in range(len(STATEMENTS)):
            with self.subTest(index=index):
                db = self.database()
                before = list(db.iterdump())
                statements = STATEMENTS[:index] + ('INVALID FIXTURE SQL',) + STATEMENTS[index:]
                with patch('backend.internals.db_migration.get_db', side_effect=db.cursor), \
                     patch('backend.internals.torrent_schema.STATEMENTS', statements), self.assertRaises(sqlite3.Error):
                    _migrate_expanded_clients()
                self.assertEqual(list(db.iterdump()),before)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

"""Additive schema 75 preserves schema 74 library rows and cover bytes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from flask import Flask

from backend.internals.db import SCHEMA_74, DBConnection
from backend.internals.db_migration import DatabaseMigrationHandler


class CustomMigration(TestCase):
    def setUp(self):
        context = Flask(__name__).app_context(); context.push(); self.addCleanup(context.pop)
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.db = DBConnection(db_file=str(Path(temporary.name) / 'db.sqlite'))
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_74)
        self.db.execute("INSERT INTO config(key,value) VALUES('database_version',74)")
        self.db.execute("INSERT INTO root_folders(id,folder) VALUES(1,'/fixture')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder) VALUES(1,11,'Remote',1)")
        self.db.execute("INSERT INTO volumes_covers(volume_id,cover) VALUES(1,?)", (b'provider',))
        self.db.execute("INSERT INTO manual_volume_covers(volume_id,cover,digest,updated_at) VALUES(1,?,?,1)",
                        (b'manual', 'digest'))
        self.db.commit()

    def test_upgrade_preserves_existing_cover_and_library(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            DatabaseMigrationHandler.handlers[74]()
        self.db.execute("UPDATE config SET value=75 WHERE key='database_version'")
        self.assertEqual(self.db.execute('SELECT title FROM volumes WHERE id=1').fetchone()[0], 'Remote')
        self.assertEqual(self.db.execute('SELECT cover FROM volumes_covers WHERE volume_id=1').fetchone()[0], b'provider')
        self.assertEqual(self.db.execute('SELECT cover FROM manual_volume_covers WHERE volume_id=1').fetchone()[0], b'manual')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertIsNotNone(self.db.execute("SELECT name FROM sqlite_master WHERE name='ignored_import_files'").fetchone())

    def test_foreign_key_failure_rolls_back_new_tables(self):
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute("INSERT INTO manual_volume_covers(volume_id,cover,digest,updated_at) VALUES(999,X'00','bad',1)")
        self.db.commit(); self.db.execute('PRAGMA foreign_keys=ON')
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            with self.assertRaisesRegex(RuntimeError, 'foreign key violation'):
                DatabaseMigrationHandler.handlers[74]()
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE name='ignored_import_files'").fetchone())
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 74)

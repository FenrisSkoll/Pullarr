"""Schema 73 artwork/ownership survives both additive artwork and ignore migrations."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from flask import Flask

from backend.internals.db import DB_SCHEMA, SCHEMA_73, DBConnection
from backend.internals.db_migration import DatabaseMigrationHandler
from backend.internals.download_jobs import DownloadStore
from backend.internals.organization_jobs import JobStore


class ManualCoverMigration(TestCase):
    def setUp(self):
        context = Flask(__name__).app_context()
        context.push()
        self.addCleanup(context.pop)
        temporary = TemporaryDirectory(prefix='pullarr-manual-cover-migration-')
        self.addCleanup(temporary.cleanup)
        self.db = DBConnection(db_file=str(Path(temporary.name) / 'library.db'))
        self.addCleanup(self.db.close)

    def test_populated_schema_73_upgrade_preserves_rows(self):
        self.db.executescript(SCHEMA_73)
        self.db.executemany('INSERT INTO config(key,value) VALUES(?,?)',
                            (('fixture_setting', 'retained'), ('database_version', 73)))
        self.db.execute('INSERT INTO root_folders(id,folder) VALUES(1,?)', ('/fixture',))
        self.db.execute('''INSERT INTO volumes(id,comicvine_id,title,root_folder,metadata_provider)
            VALUES(1,11,'Fixture',1,'comicvine')''')
        self.db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,?)', (b'provider-bytes',))
        self.db.execute('''INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number)
            VALUES(1,1,21,'1',1)''')
        self.db.execute("INSERT INTO files(id,filepath,size) VALUES(1,'/fixture/book.cbz',9)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
        before = {table: self.db.execute('SELECT * FROM ' + table).fetchall()
                  for table in ('config', 'volumes', 'volumes_covers', 'issues', 'files', 'issues_files')}
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor), \
                patch('backend.internals.db.get_db', side_effect=self.db.cursor), \
                patch('backend.internals.settings.Settings') as settings:
            settings.return_value.sv.database_version = 73
            settings.return_value.update.side_effect = lambda changes: self.db.execute(
                "UPDATE config SET value=? WHERE key='database_version'",
                (changes['database_version'],))
            DatabaseMigrationHandler.migrate()
            self.assertEqual(settings.return_value.update.call_count, 2)
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 75)
        for table in before:
            if table == 'config':
                self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='fixture_setting'").fetchone()[0],
                                 'retained')
            else:
                self.assertEqual(self.db.execute('SELECT * FROM ' + table).fetchall(), before[table])
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.db.execute('''INSERT INTO manual_volume_covers(volume_id,cover,digest,updated_at)
            VALUES(1,?,?,1)''', (b'manual-bytes', 'digest'))
        self.db.execute('DELETE FROM issues_files WHERE issue_id=1')
        self.db.execute('DELETE FROM issues WHERE volume_id=1')
        self.db.execute('DELETE FROM volumes WHERE id=1')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM manual_volume_covers').fetchone()[0], 0)

    def test_fresh_schema_75_has_independent_cover_and_ignore_tables(self):
        self.db.executescript(DB_SCHEMA)
        self.db.execute("INSERT INTO config(key,value) VALUES('database_version',75)")
        self.db.commit()
        self.assertIn(73, DatabaseMigrationHandler.handlers)
        self.assertEqual(DatabaseMigrationHandler.latest_db_version(), 75)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertIsNotNone(self.db.execute('''SELECT name FROM sqlite_master
            WHERE type='table' AND name='manual_volume_covers' ''').fetchone())
        self.assertIsNotNone(self.db.execute('''SELECT name FROM sqlite_master
            WHERE type='table' AND name='ignored_import_files' ''').fetchone())
        with closing(DownloadStore(self.db.db_file)), closing(JobStore(self.db.db_file)):
            pass

    def test_migration_rolls_back_if_existing_foreign_keys_are_invalid(self):
        self.db.executescript(SCHEMA_73)
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(999,?)',
                        (b'preexisting-provider-artwork',))
        self.db.commit()
        self.db.execute('PRAGMA foreign_keys=ON')
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            with self.assertRaisesRegex(RuntimeError, 'foreign key violation'):
                DatabaseMigrationHandler.handlers[73]()
        self.assertIsNone(self.db.execute('''SELECT name FROM sqlite_master
            WHERE type='table' AND name='manual_volume_covers' ''').fetchone())
        self.assertEqual(self.db.execute('SELECT cover FROM volumes_covers WHERE volume_id=999').fetchone()[0],
                         b'preexisting-provider-artwork')

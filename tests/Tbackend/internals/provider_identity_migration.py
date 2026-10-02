"""Migration acceptance tests committed before the schema-52 implementation."""

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fixtures.provider_identity import build_legacy, receipt
from flask import Flask

from backend.internals.db import BASE_DB_SCHEMA, DBConnection
from backend.internals.db_migration import DatabaseMigrationHandler
from backend.internals.identity_schema import IDENTITY_SCHEMA


class IdentityMigrationTests(TestCase):
    def setUp(self):
        context = Flask(__name__).app_context()
        context.push()
        self.addCleanup(context.pop)
        temporary = TemporaryDirectory(prefix='kapowarr-migration-')
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / 'legacy.db')
        self.db = DBConnection(db_file=self.path)
        self.addCleanup(self.db.close)
        self.patcher = patch('backend.internals.db_migration.get_db',
                             side_effect=self.db.cursor)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def migrate(self):
        self.assertIn(51, DatabaseMigrationHandler.handlers)
        DatabaseMigrationHandler.handlers[51]()

    def assert_healthy(self):
        self.assertEqual(
            self.db.execute('PRAGMA integrity_check').fetchall(), [
                ('ok',)])
        self.assertEqual(self.db.execute(
            'PRAGMA foreign_key_check').fetchall(), [])

    def assert_backfill(self):
        self.assertEqual(
            self.db.execute('''SELECT volume_id,provider,provider_id,
            provenance,last_fetch FROM volume_external_ids ORDER BY volume_id''').fetchall(),
            self.db.execute('''SELECT id,'comicvine',CAST(comicvine_id AS TEXT),
                'migration',last_cv_fetch FROM volumes ORDER BY id''').fetchall())
        self.assertEqual(
            self.db.execute('''SELECT issue_id,provider,provider_id,
            provenance FROM issue_external_ids ORDER BY issue_id''').fetchall(),
            self.db.execute('''SELECT id,'comicvine',CAST(comicvine_id AS TEXT),
                'migration' FROM issues ORDER BY id''').fetchall())
        self.assertEqual(self.db.execute('''SELECT COUNT(*) FROM volumes
            WHERE metadata_provider != 'comicvine' ''').fetchone()[0], 0)

    def test_empty(self):
        build_legacy(self.db, volumes=0)
        self.migrate()
        self.assert_backfill()
        self.assert_healthy()

    def test_golden_rows_relationships_keys_monitoring_metadata(self):
        build_legacy(self.db)
        columns, before = receipt(self.db)
        self.migrate()
        self.assertEqual(receipt(self.db, columns)[1], before)
        self.assert_backfill()
        self.assert_healthy()
        self.assertEqual(self.db.execute('''SELECT value FROM config
            WHERE key='database_version' ''').fetchone()[0], 52)

    def test_one_volume(self):
        build_legacy(self.db, volumes=1)
        self.migrate()
        self.assert_backfill()

    def test_duplicate_volume_ids_preserved(self):
        build_legacy(self.db)
        self.migrate()
        self.assertEqual(self.db.execute('''SELECT volume_id FROM volume_external_ids
            WHERE provider='comicvine' AND provider_id='42' ORDER BY volume_id''').fetchall(),
            [(10,), (20,)])

    def test_issue_identity_unique_and_cascade(self):
        build_legacy(self.db)
        self.migrate()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO issue_external_ids VALUES
                (30,'comicvine','9223372036854775807','test')''')
        self.db.execute('DELETE FROM issues WHERE id=30')
        self.assertEqual(self.db.execute(
            '''SELECT COUNT(*) FROM issue_external_ids
            WHERE issue_id=30''').fetchone()[0], 0)
        self.assert_healthy()

    def test_reopen_and_framework_idempotence(self):
        build_legacy(self.db)
        self.migrate()
        self.db.commit()
        with closing(sqlite3.connect(self.path)) as reopened:
            self.assertEqual(reopened.execute(
                '''SELECT COUNT(*) FROM issue_external_ids''').fetchone()[0], 9)
        before = receipt(self.db)
        settings_patch = patch('backend.internals.settings.Settings')
        settings = settings_patch.start()
        self.addCleanup(settings_patch.stop)
        settings.return_value.sv.database_version = 52
        with patch.object(DatabaseMigrationHandler, 'latest_db_version', return_value=52):
            DatabaseMigrationHandler.migrate()
        settings.return_value.update.assert_not_called()
        self.assertEqual(receipt(self.db), before)

    def test_failure_rolls_back_ddl_backfill_and_version(self):
        build_legacy(self.db)
        schema = self.db.execute(
            'SELECT * FROM sqlite_master ORDER BY name').fetchall()
        before = receipt(self.db)

        def deny_issue_insert(action, table, *unused):
            if action == sqlite3.SQLITE_INSERT and table == 'issue_external_ids':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(deny_issue_insert)
        with self.assertRaises(sqlite3.DatabaseError):
            self.migrate()
        self.db.set_authorizer(None)
        # Simulate existing app teardown: no partial state survives.
        self.db.commit()
        self.assertEqual(self.db.execute(
            'SELECT * FROM sqlite_master ORDER BY name').fetchall(), schema)
        self.assertEqual(receipt(self.db), before)
        self.assertEqual(self.db.execute(
            "SELECT value FROM config WHERE key='database_version'").fetchone()[0], 51)
        self.migrate()
        self.assert_backfill()

    def test_version_write_failure_rolls_back_everything(self):
        build_legacy(self.db)
        before = receipt(self.db)
        self.db.set_authorizer(
            lambda action, table, *args: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_UPDATE and table == 'config' else
            sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            self.migrate()
        self.db.set_authorizer(None)
        self.assertEqual(receipt(self.db), before)
        self.assertNotIn('metadata_provider',
                         [r[1]
                          for r in
                          self.db.execute('PRAGMA table_info(volumes)')])

    def test_fresh_and_upgrade_columns_constraints_match(self):
        build_legacy(self.db)
        self.migrate()
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(BASE_DB_SCHEMA + IDENTITY_SCHEMA)
            for table in ('volumes', 'issues', 'volume_external_ids',
                          'issue_external_ids'):
                for pragma in ('table_info', 'foreign_key_list', 'index_list'):
                    query = f'PRAGMA {pragma}({table})'
                    self.assertEqual(sorted(fresh.execute(query).fetchall()),
                                     sorted(self.db.execute(query).fetchall()))

    def test_legacy_not_null_and_issue_unique_unchanged(self):
        build_legacy(self.db)
        self.migrate()
        for table in ('volumes', 'issues'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(f'UPDATE {table} SET comicvine_id=NULL')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE issues SET comicvine_id=3 WHERE id=10')
        self.assert_healthy()

    def test_invalid_legacy_fk_fails_without_partial_upgrade(self):
        build_legacy(self.db)
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute('UPDATE issues SET volume_id=999 WHERE id=30')
        self.db.commit()
        self.db.execute('PRAGMA foreign_keys=ON')
        before = receipt(self.db)
        with self.assertRaisesRegex(RuntimeError, 'foreign key violation'):
            self.migrate()
        self.db.commit()
        self.assertEqual(receipt(self.db), before)

    def test_real_setup_empty_bootstrap_and_repeat_startup(self):
        # Separate interpreter isolates Settings singleton, converters and
        # connection manager; this is the actual startup/bootstrap/framework.
        self.db.close()
        program = '''
import sys
from flask import Flask
from backend.internals.db import DBConnection, setup_db, close_db
DBConnection.default_file = sys.argv[1]
with Flask(__name__).app_context():
    setup_db()
    close_db()
with Flask(__name__).app_context():
    setup_db()
    close_db()
'''
        result = subprocess.run([sys.executable, '-c', program, self.path],
                                capture_output=True, text=True, timeout=30)
        # Never echo startup output: fresh setup can log a generated API key.
        self.assertEqual(result.returncode, 0, 'Isolated setup_db failed')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT value FROM config WHERE key='database_version'").fetchone(), (DatabaseMigrationHandler.latest_db_version(),))
            self.assertEqual(
                db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(
                db.execute('PRAGMA integrity_check').fetchall(), [
                    ('ok',)])
            self.assertEqual(
                db.execute('SELECT COUNT(*) FROM volume_external_ids').fetchone(),
                (0,))
            self.assertEqual(
                db.execute('SELECT COUNT(*) FROM issue_external_ids').fetchone(),
                (0,))

    def test_real_setup_51_upgrade_and_repeat_startup(self):
        build_legacy(self.db)
        columns, before = receipt(self.db)
        program = '''
import sys
from flask import Flask
from backend.internals.db import DBConnection, setup_db, close_db
DBConnection.default_file = sys.argv[1]
for _ in range(2):
    with Flask(__name__).app_context():
        setup_db()
        close_db()
'''
        result = subprocess.run([sys.executable, '-c', program, self.path],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(
            result.returncode,
            0,
            'Isolated schema-51 setup failed')
        after = receipt(self.db, columns)[1]
        for table in before:
            if table not in ('config', 'task_intervals'):
                self.assertEqual(after[table], before[table], table)
        self.assert_backfill()
        self.assert_healthy()

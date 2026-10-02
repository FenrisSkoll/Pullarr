"""Lossless schema-52 reconstruction acceptance, committed before migration."""

import sqlite3
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from fixtures.provider_identity import receipt
from fixtures.provider_schema52 import build_schema52
from flask import Flask, g

from backend.internals.db import DBConnection
from backend.internals.db_migration import DatabaseMigrationHandler


class ProviderSchema53(TestCase):
    def setUp(self):
        context = Flask(__name__).app_context()
        context.push()
        self.addCleanup(context.pop)
        self.db = DBConnection(db_file=':memory:')
        self.addCleanup(self.db.close)
        p = patch('backend.internals.db_migration.get_db',
                  side_effect=lambda: self.db.cursor())
        p.start()
        self.addCleanup(p.stop)

    def migrate(self):
        self.assertIn(52, DatabaseMigrationHandler.handlers)
        DatabaseMigrationHandler.handlers[52]()

    def healthy(self):
        self.assertEqual(self.db.execute(
            'PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(self.db.execute(
            'PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute(
            'PRAGMA foreign_keys').fetchone()[0], 1)

    def test_empty(self):
        build_schema52(self.db, volumes=0)
        self.migrate()
        self.healthy()
        self.assertEqual(self.db.execute(
            "SELECT value FROM config WHERE key='database_version'").fetchone()[0], 53)

    def test_golden_all_rows_keys_relationships_refs_and_timestamps(self):
        build_schema52(self.db)
        before = receipt(self.db)
        indexes = self.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name").fetchall()
        self.migrate()
        self.assertEqual(receipt(self.db), before)
        self.assertEqual(self.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name").fetchall(), indexes)
        self.healthy()

    def test_nullable_ids_and_extensible_provider_keys(self):
        build_schema52(self.db)
        self.migrate()
        self.db.execute(
            "INSERT INTO volumes(id,title,root_folder,metadata_provider) VALUES (99,'Neutral',7,'test_provider')")
        self.db.execute(
            "INSERT INTO volume_external_ids VALUES (99,'test_provider','abc:123','provider',NULL)")
        for identity in (99, 100):
            self.db.execute(
                "INSERT INTO issues(id,volume_id,issue_number,calculated_issue_number) VALUES (?,99,'1',1)", (identity,))
            self.db.execute(
                "INSERT INTO issue_external_ids VALUES (?,'test_provider',?,'provider')", (identity, str(identity)))
        self.assertEqual(self.db.execute(
            'SELECT comicvine_id FROM issues WHERE volume_id=99').fetchall(), [(None,), (None,)])
        self.assertEqual(self.db.execute(
            "SELECT * FROM issue_external_ids WHERE issue_id=99 AND provider='comicvine'").fetchall(), [])
        self.db.execute('DELETE FROM volumes WHERE id=99')
        self.assertEqual(self.db.execute(
            'SELECT * FROM issue_external_ids WHERE issue_id IN (99,100)').fetchall(), [])
        self.healthy()

    def test_failure_rolls_back_schema_rows_indexes_triggers_and_version(self):
        build_schema52(self.db)
        before = receipt(self.db)
        schema = self.db.execute(
            'SELECT * FROM sqlite_master ORDER BY type,name').fetchall()
        failed = False

        def deny(action, arg1, arg2, database, source):
            nonlocal failed
            if action == sqlite3.SQLITE_DROP_TABLE and arg1 == 'issues' and not failed:
                failed = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.db.set_authorizer(deny)
        with self.assertRaises(sqlite3.DatabaseError):
            self.migrate()
        self.db.set_authorizer(None)
        self.assertTrue(failed)
        self.assertEqual(receipt(self.db), before)
        self.assertEqual(self.db.execute(
            'SELECT * FROM sqlite_master ORDER BY type,name').fetchall(), schema)
        self.assertEqual(self.db.execute(
            "SELECT value FROM config WHERE key='database_version'").fetchone()[0], 52)
        self.healthy()
        self.migrate()
        self.healthy()

    def test_large_reconstruction_lossless(self):
        for volumes, issues in ((100, 50), (1000, 50)):
            with self.subTest(volumes=volumes):
                if volumes == 1000:
                    self.db.close()
                    g.cursors.pop(':memory:', None)
                    self.db = DBConnection(db_file=':memory:')
                    self.addCleanup(self.db.close)
                build_schema52(self.db, volumes, issues, edges=False)
                before = receipt(self.db)
                started = perf_counter()
                self.migrate()
                print('\nMigration %d volumes/%d issues: %.4fs' %
                      (volumes, volumes*issues, perf_counter()-started))
                self.assertEqual(receipt(self.db), before)
                self.healthy()

    def test_version_update_failure_restores_complete_old_schema(self):
        build_schema52(self.db)
        before = receipt(self.db)
        schema = self.db.execute(
            'SELECT * FROM sqlite_master ORDER BY type,name').fetchall()

        def deny(action, arg1, arg2, database, source):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and arg1 == 'config' else sqlite3.SQLITE_OK

        self.db.set_authorizer(deny)
        with self.assertRaises(sqlite3.DatabaseError):
            self.migrate()
        self.db.set_authorizer(None)
        self.assertEqual(receipt(self.db), before)
        self.assertEqual(self.db.execute(
            'SELECT * FROM sqlite_master ORDER BY type,name').fetchall(), schema)
        self.healthy()

    def test_extra_trigger_preserved_and_updated_shadow_triggers_work(self):
        build_schema52(self.db)
        self.db.execute('''CREATE TRIGGER preserve_custom_trigger AFTER UPDATE OF title ON volumes
            BEGIN UPDATE volumes SET alt_title=NEW.title WHERE id=NEW.id; END''')
        self.db.commit()
        self.migrate()
        self.db.execute("UPDATE volumes SET title='Changed' WHERE id=10")
        self.assertEqual(self.db.execute(
            'SELECT alt_title FROM volumes WHERE id=10').fetchone()[0], 'Changed')
        self.db.execute('UPDATE volumes SET comicvine_id=NULL WHERE id=10')
        self.assertEqual(self.db.execute(
            "SELECT * FROM volume_external_ids WHERE volume_id=10 AND provider='comicvine'").fetchall(), [])
        self.assertEqual(self.db.execute(
            "SELECT provider_id FROM volume_external_ids WHERE volume_id=10 AND provider='metron'").fetchone()[0], 'M:10')
        self.db.execute('UPDATE volumes SET comicvine_id=456 WHERE id=10')
        self.assertEqual(self.db.execute(
            "SELECT provider_id FROM volume_external_ids WHERE volume_id=10 AND provider='comicvine'").fetchone()[0], '456')
        self.healthy()

"""Schema 60 -> 61 is additive and never reads a catalog or converts C1 IDs."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from Tbackend.internals import bibliography_migration as previous

from backend.internals.db import SCHEMA_61
from backend.internals.db_migration import _migrate_reprint_graph
from backend.internals.reprint_schema import STATEMENTS


class GraphMigrationTests(TestCase):
    def setUp(self):
        previous.BibliographyMigrationTests.setUp(self)
        previous.BibliographyMigrationTests.migrate(self)
        self.db.execute("INSERT INTO issue_bibliography(issue_id,provider,policy) VALUES(1,'gcd','test')")
        self.db.execute("INSERT INTO story_observation_sets VALUES(1,1,'gcd','v1','observation','digest',1,1)")
        self.db.execute("INSERT INTO story_observations(id,set_id,position,source_position,mode,title) VALUES(1,1,0,0,'issue_scoped_story_observation','No provider ID')")
        self.db.commit()

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            _migrate_reprint_graph()

    def test_preserve_every_prior_table_and_no_fake_identity(self):
        tables = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='config'")]
        before = {t: self.db.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
        self.migrate()
        for table, values in before.items():
            self.assertEqual(values, self.db.execute(f'SELECT * FROM "{table}"').fetchall())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bibliographic_story_entities').fetchone()[0], 0)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_repeat_and_fresh_parity(self):
        self.migrate()
        before = tuple(self.db.iterdump())
        self.migrate()
        self.assertEqual(before, tuple(self.db.iterdump()))
        with closing(sqlite3.connect(':memory:')) as fresh:
            fresh.executescript(SCHEMA_61)
            for (name,) in fresh.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                for pragma in ('table_info', 'foreign_key_list'):
                    self.assertEqual(fresh.execute(f'PRAGMA {pragma}("{name}")').fetchall(),
                                     self.db.execute(f'PRAGMA {pragma}("{name}")').fetchall())

    def test_injected_ddl_rollback(self):
        before = tuple(self.db.iterdump())
        with patch('backend.internals.reprint_schema.STATEMENTS', (*STATEMENTS[:4], 'INVALID SQL')):
            with self.assertRaises(sqlite3.OperationalError):
                self.migrate()
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_reopen_and_later_phase_stores_accept_schema61(self):
        from backend.internals.download_jobs import DownloadStore
        from backend.internals.organization_jobs import JobStore
        self.migrate()
        with TemporaryDirectory(prefix='kapowarr-graph-migration-') as folder:
            path = str(Path(folder) / 'app.sqlite')
            with closing(sqlite3.connect(path)) as target:
                self.db.backup(target)
            with closing(sqlite3.connect(path)) as reopened:
                with patch('backend.internals.db_migration.get_db', side_effect=reopened.cursor):
                    _migrate_reprint_graph()
                self.assertEqual(reopened.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 61)
            for kind in (DownloadStore, JobStore):
                store = kind(path)
                store.close()

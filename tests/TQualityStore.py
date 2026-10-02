"""Migration-safe policy, inheritance and identity preservation."""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from TQuality import policy

from backend.base.quality import QualityError, default_policy
from backend.internals.collections import CollectionStore
from backend.internals.db import DB_SCHEMA, SCHEMA_69, SCHEMA_70
from backend.internals.db_migration import _migrate_quality_profiles
from backend.internals.quality import QualityStore
from backend.internals.quality_schema import STATEMENTS


class QualityStoreTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA)
        self.db.execute("INSERT INTO root_folders VALUES(1,'fixture-root')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder,monitored) VALUES(1,101,'One',1,'fixture-volume',1)")
        self.db.execute("INSERT INTO issues(id,comicvine_id,volume_id,issue_number,calculated_issue_number,monitored) VALUES(1,102,1,'1',1,1)")
        self.db.commit()
        self.store = QualityStore(self.db.cursor())

    def test_crud_revisions_default_dependencies(self):
        self.assertFalse(self.store.profile(1)['upgrades'])
        with self.assertRaisesRegex(QualityError, 'profile_in_use'):
            self.store.delete(1, 1, True)
        new = self.store.save('Test', default_policy())
        self.assertEqual(new['revision'], 1)
        new = self.store.save('Changed', default_policy(), identifier=new['id'], revision=1)
        self.assertEqual(new['revision'], 2)
        with self.assertRaisesRegex(QualityError, 'revision_conflict'):
            self.store.save('Stale', default_policy(), identifier=new['id'], revision=1)
        self.store.assign('volume', 1, new['id'], None)
        self.assertEqual(self.store.effective([1])[1]['source'], 'volume')
        with self.assertRaisesRegex(QualityError, 'profile_in_use'):
            self.store.delete(new['id'], 2, True)
        self.store.assign('volume', 1, None, new['id'])
        self.store.delete(new['id'], 2, True)

    def test_collection_conflict_and_explicit_override(self):
        c = CollectionStore(self.db.cursor())
        a, b = c.create('A'), c.create('B')
        for tree in (a,b):
            c.add_local(tree['nodes'][0]['id'], tree['revision'], 1)
        second = self.store.save('Second', default_policy())['id']
        self.store.assign('node', a['nodes'][0]['id'], 1, None)
        self.store.assign('node', b['nodes'][0]['id'], 1, None)
        self.assertEqual(self.store.effective([1])[1]['source'], 'collection')
        self.store.assign('node', b['nodes'][0]['id'], second, 1)
        self.assertTrue(self.store.effective([1])[1]['conflict'])
        self.store.assign('volume', 1, 1, None)
        self.assertFalse(self.store.effective([1])[1]['conflict'])
        self.assertEqual(self.db.execute('SELECT monitored FROM volumes').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT monitored FROM issues').fetchone()[0], 1)

    def test_missing_not_upgrade(self):
        item = self.store.issue_states([1])[0]
        self.assertEqual(item['reason'], 'missing')
        self.assertFalse(item['upgrade_eligible'])

    def test_concurrent_policy_edit_cannot_mix_revision_and_groups(self):
        with TemporaryDirectory() as directory:
            path=str(Path(directory)/'snapshot.db')
            reader=sqlite3.connect(path);writer=sqlite3.connect(path)
            try:
                self.db.backup(reader)
                reader.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA foreign_keys=ON')
                changed=[]
                def interleave(sql):
                    if sql.startswith('SELECT * FROM quality_groups') and not changed:
                        changed.append(True)
                        QualityStore(writer.cursor()).save('Concurrent',policy(),identifier=1,revision=1)
                reader.set_trace_callback(interleave)
                old=QualityStore(reader.cursor()).profile(1)
                self.assertEqual(old['revision'],1)
                self.assertEqual(len(old['groups']),1)
                reader.set_trace_callback(None)
                new=QualityStore(reader.cursor()).profile(1)
                self.assertEqual(new['revision'],2)
                self.assertEqual(len(new['groups']),2)
            finally:
                reader.close();writer.close()


class QualityMigrationTests(TestCase):
    def test_failure_rollback_and_parity(self):
        for failure in range(len(STATEMENTS)+1):
            with self.subTest(failure=failure):
                db = sqlite3.connect(':memory:')
                try:
                    db.execute('PRAGMA foreign_keys=ON')
                    db.executescript(SCHEMA_69)
                    db.execute("INSERT INTO config VALUES('database_version',69)")
                    db.commit()
                    before = list(db.iterdump())
                    cursor = db.cursor()
                    class Failing:
                        count = 0
                        def execute(self, sql, args=()):
                            if sql in STATEMENTS:
                                if self.count == failure:
                                    raise RuntimeError('injected')
                                self.count += 1
                            return cursor.execute(sql, args)
                    with patch('backend.internals.db_migration.get_db', return_value=Failing()):
                        if failure < len(STATEMENTS):
                            with self.assertRaises(RuntimeError):
                                _migrate_quality_profiles()
                            self.assertEqual(before, list(db.iterdump()))
                        else:
                            _migrate_quality_profiles()
                            _migrate_quality_profiles()
                            fresh = sqlite3.connect(':memory:')
                            try:
                                fresh.executescript(SCHEMA_70)
                                query = "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
                                self.assertEqual(db.execute(query).fetchall(), fresh.execute(query).fetchall())
                            finally:
                                fresh.close()
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
                finally:
                    db.close()

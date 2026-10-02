"""Real read-only SQLite boundary, based on pinned Django columns/migrations.

Synthetic catalog, not an official dump or evidence of live archive availability.
"""

import sqlite3
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from backend.implementations.metadata.gcd_catalog import Catalog, CatalogError

DDL = '''
CREATE TABLE gcd_series(id INTEGER PRIMARY KEY,name TEXT NOT NULL,deleted BOOL NOT NULL);
CREATE TABLE gcd_issue(id INTEGER PRIMARY KEY,series_id INTEGER NOT NULL REFERENCES gcd_series(id),
 number TEXT NOT NULL,title TEXT NOT NULL,deleted BOOL NOT NULL);
CREATE TABLE gcd_story(id INTEGER PRIMARY KEY,issue_id INTEGER NOT NULL REFERENCES gcd_issue(id),
 title TEXT NOT NULL,sequence_number INTEGER NOT NULL,deleted BOOL NOT NULL);
CREATE TABLE gcd_creator(id INTEGER PRIMARY KEY,gcd_official_name TEXT NOT NULL,deleted BOOL NOT NULL);
CREATE TABLE gcd_creator_name_detail(id INTEGER PRIMARY KEY,creator_id INTEGER NOT NULL REFERENCES gcd_creator(id),
 name TEXT NOT NULL,is_official_name BOOL NOT NULL,type_id INTEGER,deleted BOOL NOT NULL);
CREATE TABLE gcd_credit_type(id INTEGER PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE gcd_story_credit(id INTEGER PRIMARY KEY,story_id INTEGER NOT NULL REFERENCES gcd_story(id),
 creator_id INTEGER NOT NULL REFERENCES gcd_creator_name_detail(id),credit_type_id INTEGER NOT NULL REFERENCES gcd_credit_type(id),
 credited_as TEXT NOT NULL,signed_as TEXT NOT NULL,is_credited BOOL NOT NULL,is_signed BOOL NOT NULL,
 uncertain BOOL NOT NULL,deleted BOOL NOT NULL);
CREATE TABLE gcd_reprint(id INTEGER PRIMARY KEY,origin_issue_id INTEGER NOT NULL REFERENCES gcd_issue(id),
 target_issue_id INTEGER NOT NULL REFERENCES gcd_issue(id),origin_id INTEGER REFERENCES gcd_story(id),
 target_id INTEGER REFERENCES gcd_story(id),notes TEXT NOT NULL,modified TEXT NOT NULL);
CREATE INDEX reprint_origin ON gcd_reprint(origin_issue_id);
CREATE INDEX reprint_target ON gcd_reprint(target_issue_id);
CREATE INDEX story_issue ON gcd_story(issue_id);
CREATE INDEX credit_story ON gcd_story_credit(story_id);
'''


def fixture(path):
    db = sqlite3.connect(path)
    db.executescript(DDL)
    db.executemany('INSERT INTO gcd_series VALUES(?,?,0)', ((1, 'Local'), (2, 'External')))
    db.executemany('INSERT INTO gcd_issue VALUES(?,?,?,?,0)', ((1, 1, '1', ''), (2, 2, 'Annual', '<script>')))
    db.executemany('INSERT INTO gcd_story VALUES(?,?,?,?,0)', ((100, 1, 'Same title', 1), (200, 2, 'Same title', 1)))
    db.executemany('INSERT INTO gcd_creator VALUES(?,?,0)', ((10, 'Same Name'), (11, 'Same Name')))
    db.executemany('INSERT INTO gcd_creator_name_detail VALUES(?,?,?,?,?,0)',
                   ((20, 10, 'Official', 1, None), (21, 10, 'Alias', 0, 1), (22, 11, 'Official', 1, None)))
    db.execute("INSERT INTO gcd_credit_type VALUES(1,'script')")
    db.executemany('INSERT INTO gcd_story_credit VALUES(?,?,?,?,?,?,?,?,?,0)',
        ((30, 100, 20, 1, 'Spelling', '', 1, 0, 0), (31, 100, 21, 1, '', '', 0, 0, 1),
         (32, 200, 22, 1, '', 'Signature', 0, 1, 0)))
    db.executemany('INSERT INTO gcd_reprint VALUES(?,?,?,?,?,?,?)',
        ((40, 1, 2, 100, 200, 'complete entire full', '2026-09-01'),
         (41, 1, 2, 100, None, '', '2026-09-01'),
         (42, 1, 2, None, 200, '', '2026-09-01'),
         (43, 1, 2, None, None, '', '2026-09-01')))
    db.commit()
    return db


class CatalogTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='kapowarr-catalog-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'catalog.sqlite'
        self.db = fixture(self.path)
        self.addCleanup(self.db.close)

    def acquire(self):
        return Catalog(self.path).acquire({'1': '1'})

    def test_all_shapes_real_primary_keys_no_inference(self):
        before = self.path.read_bytes()
        graph = self.acquire()
        self.assertEqual({e.shape for e in graph.edges},
            {'story_to_story', 'story_to_issue', 'issue_to_story', 'issue_to_issue'})
        self.assertEqual([e.provider_id for e in graph.evidence_for_target_issue('2')], ['40', '41', '42', '43'])
        self.assertEqual(graph.evidence_for_target_issue('1'), ())
        self.assertEqual({s.provider_id for s in graph.stories}, {'100', '200'})
        self.assertEqual({c.provider_id for c in graph.creators}, {'10', '11'})
        self.assertEqual({n.creator_id for n in graph.names}, {'10', '11'})
        self.assertEqual(graph.edges[0].notes, 'complete entire full')
        self.assertFalse(hasattr(graph, 'contains'))
        self.assertFalse(hasattr(graph.edges[3], 'complete_containment'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_parent_missing_same_issue_invalid_scope_rejected(self):
        mutations = (
            'UPDATE gcd_reprint SET origin_id=999 WHERE id=40',
            'UPDATE gcd_reprint SET origin_id=200 WHERE id=40',
            'UPDATE gcd_reprint SET target_issue_id=1 WHERE id=43',
            'UPDATE gcd_issue SET series_id=2 WHERE id=1',
            'DELETE FROM gcd_creator WHERE id=10',
        )
        for sql in mutations:
            with self.subTest(sql=sql):
                self.db.execute('SAVEPOINT change')
                self.db.execute(sql)
                self.db.execute('RELEASE change')
                # Revert from a backup after each independent invalid source.
                with self.assertRaises(CatalogError):
                    self.acquire()
                self.db.close()
                self.path.unlink()
                self.db = fixture(self.path)
        self.db.close()

    def test_soft_deleted_entities_preserve_identities(self):
        for table, value in (('gcd_story', 100), ('gcd_issue', 2), ('gcd_creator', 10),
                             ('gcd_creator_name_detail', 20), ('gcd_story_credit', 30)):
            self.db.execute(f'UPDATE {table} SET deleted=1 WHERE id=?', (value,))
        self.db.commit()
        graph = self.acquire()
        self.assertTrue(graph.stories[0].deleted)
        self.assertTrue(graph.issues[1].deleted)
        self.assertTrue(graph.creators[0].deleted)
        self.assertTrue(graph.names[0].deleted)
        self.assertTrue(graph.credits[0].deleted)
        self.assertEqual(len(graph.edges), 4)

    def test_no_recursive_neighbor_inventory(self):
        self.db.execute("INSERT INTO gcd_issue VALUES(3,2,'3','',0)")
        self.db.execute("INSERT INTO gcd_story VALUES(201,2,'Unreferenced',2,0)")
        self.db.execute("INSERT INTO gcd_reprint VALUES(44,2,3,NULL,NULL,'','2026')")
        self.db.commit()
        graph = self.acquire()
        self.assertEqual(len(graph.edges), 4)
        self.assertEqual(len(graph.issues), 2)
        self.assertNotIn('201', {s.provider_id for s in graph.stories})

    def test_readonly_connection_and_query_plan(self):
        catalog = Catalog(self.path)
        with catalog.opened() as (db, _):
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE gcd_issue SET number='changed'")
            plan = db.execute('EXPLAIN QUERY PLAN SELECT id FROM gcd_reprint WHERE origin_issue_id=?', (1,)).fetchall()
            self.assertTrue(any('INDEX' in row[3] for row in plan))
        self.assertTrue(catalog.test()['schema_supported'])

    def test_changed_file_detected_before_admission(self):
        from backend.implementations.metadata.gcd_catalog import \
            file_observation
        observed = file_observation(self.path)
        with patch('backend.implementations.metadata.gcd_catalog.file_observation',
                   side_effect=(observed, observed, (*observed[:-1], observed[-1] + 1))):
            with self.assertRaises(CatalogError):
                self.acquire()

    def test_schema_index_view_and_sidecar_fail_closed(self):
        self.db.execute('DROP INDEX reprint_target')
        self.db.commit()
        with self.assertRaises(CatalogError):
            self.acquire()
        self.db.execute('CREATE INDEX reprint_target ON gcd_reprint(target_issue_id)')
        self.db.execute('ALTER TABLE gcd_creator RENAME TO hidden_creator')
        self.db.execute('CREATE VIEW gcd_creator AS SELECT * FROM hidden_creator')
        self.db.commit()
        with self.assertRaises(CatalogError):
            self.acquire()

    def test_invalid_path_missing_source_and_text_bounds(self):
        for path in ('relative.sqlite', self.path.parent, self.path.parent / 'missing'):
            with self.subTest(path=path), self.assertRaises(CatalogError):
                Catalog(path).test()
        self.db.execute('UPDATE gcd_reprint SET notes=?', ('x' * 8193,))
        self.db.commit()
        with self.assertRaises(CatalogError):
            self.acquire()

    def test_snapshot_identity_and_parent_invariants(self):
        graph = self.acquire()
        with self.assertRaises(ValueError):
            replace(graph, stories=graph.stories + graph.stories)
        with self.assertRaises(ValueError):
            replace(graph, stories=(replace(graph.stories[0], issue_id='2'), graph.stories[1]))
        with self.assertRaises(ValueError):
            replace(graph.edges[0], target_issue='1')

    def test_empty_seed_is_bounded_and_not_whole_catalog(self):
        graph = Catalog(self.path).acquire({})
        self.assertEqual(graph.edges, ())
        self.assertEqual(graph.select_count, 0)

    def test_oversized_optional_story_endpoint_cannot_become_issue_only(self):
        self.db.execute('UPDATE gcd_reprint SET origin_id=? WHERE id=40', ('x' * 9000,))
        self.db.commit()
        with self.assertRaises(CatalogError):
            self.acquire()

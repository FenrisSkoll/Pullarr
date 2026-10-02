"""Local organization, exact identity, transaction and migration contracts."""

import json
import sqlite3
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from backend.base.collections import CollectionError
from backend.base.content_claims import ClaimKind, PublicationRef
from backend.features.wanted_status import wanted_rows
from backend.internals.collections import CollectionStore
from backend.internals.collections_schema import STATEMENTS
from backend.internals.content_claims import claim_preview, confirm_claim
from backend.internals.db import DB_SCHEMA, SCHEMA_66, SCHEMA_67
from backend.internals.db_migration import _migrate_collections


class CollectionsTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA)
        self.db.execute("INSERT INTO root_folders VALUES(1,'fixture-root')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,year,publisher,root_folder,folder,monitored) VALUES(1,101,'Batman',2016,'DC',1,'fixture-volume',0)")
        self.db.commit()
        self.store = CollectionStore(self.db.cursor())
        self.collection = self.store.create('Batman')
        self.cid = self.collection['id']
        self.root = self.collection['nodes'][0]['id']

    def revision(self):
        return self.store.tree(self.cid)['revision']

    def child(self, title='Omnibuses', parent=None, monitoring='inherit'):
        tree = self.store.edit_node(self.cid, self.revision(), None, title=title, description='', kind='unknown',
            monitoring=monitoring, parent_id=parent or self.root, position=0)
        return max(n['id'] for n in tree['nodes'])

    def suggestion(self, identity='900', node=None):
        self.store.propose(node or self.root, [dict(provider='comicvine', provider_id=identity, title='External Omnibus', year=2020, publisher='DC')], 'Batman omnibus')
        return next(s for s in self.store.suggestions(node or self.root)['items'] if s['provider_id'] == identity)

    def accept(self, suggestion):
        return self.store.decide(suggestion['id'], suggestion['revision'], self.revision(), 'accepted')['publication_id']

    def test_local_external_completeness_and_deletion(self):
        before = self.db.execute('SELECT * FROM volumes').fetchall()
        self.store.add_local(self.root, self.revision(), 1)
        self.accept(self.suggestion())
        page = self.store.publications(self.cid)
        self.assertEqual([p['status'] for p in page['items']], ['in_library', 'external'])
        self.assertEqual(self.store.tree(self.cid)['completeness']['percent'], 50)
        self.assertEqual(before, self.db.execute('SELECT * FROM volumes').fetchall())
        self.db.execute('DELETE FROM volumes WHERE id=1')
        self.db.commit()
        self.assertEqual(self.store.tree(self.cid)['completeness']['in_library'], 0)
        self.assertEqual(len(self.store.publications(self.cid)['items']), 2)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_tree_cycle_depth_stale_and_subtree_delete(self):
        a = self.child()
        b = self.child('Deluxe', a)
        with self.assertRaises(CollectionError):
            self.store.edit_node(self.cid, self.revision(), a, title='Omnibuses', description='', kind='unknown',
                monitoring='inherit', parent_id=b, position=0)
        with self.assertRaises(CollectionError):
            self.store.edit_node(self.cid, 0, b, title='New', description='', kind='unknown',
                monitoring='inherit', parent_id=self.root, position=0)
        for i in range(5):
            b = self.child(str(i), b)
        with self.assertRaises(CollectionError):
            self.child('Too deep', b)
        self.store.delete_node(self.cid, self.revision(), a, True)
        self.assertEqual(len(self.store.tree(self.cid)['nodes']), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM volumes').fetchone()[0], 1)

    def test_decisions_idempotent_reconsider_and_atomic_failure(self):
        suggestion = self.suggestion()
        self.store.decide(suggestion['id'], 0, self.revision(), 'rejected')
        self.store.propose(self.root, [dict(provider='comicvine', provider_id='900', title='Changed', year=None)], 'new query')
        self.assertFalse(self.store.suggestions(self.root)['items'])
        self.store.decide(suggestion['id'], 1, self.revision(), 'pending')
        before = list(self.db.iterdump())
        def fail(stage):
            if stage == 'decision':
                raise RuntimeError('injected')
        self.store.fault = fail
        with self.assertRaises(RuntimeError):
            self.accept(self.store.suggestions(self.root)['items'][0])
        self.assertEqual(before, list(self.db.iterdump()))
        self.store.fault = lambda _: None
        self.accept(self.store.suggestions(self.root)['items'][0])
        self.assertEqual(self.store.tree(self.cid)['completeness']['total'], 1)

    def test_monitoring_is_separate_and_calendar_only_accepted(self):
        node = self.child(monitoring='monitored')
        before = self.db.execute('SELECT monitored FROM volumes').fetchall()
        self.store.add_local(node, self.revision(), 1)
        self.suggestion()
        handoff = self.store.calendar_page()
        self.assertEqual(len(handoff['items']), 1)
        self.assertTrue(handoff['items'][0]['effective_monitored'])
        self.assertEqual(before, self.db.execute('SELECT monitored FROM volumes').fetchall())
        self.assertIn('evidence', handoff['items'][0]['memberships'][0])

    def test_exact_ref_not_title_links_and_metadata_display(self):
        publication = self.accept(self.suggestion('101'))
        self.assertEqual(self.store._resolve([publication])[0]['local_volume_id'], 1)
        self.db.execute("UPDATE volumes SET title='Updated title' WHERE id=1")
        self.db.commit()
        self.assertEqual(self.store.publications(self.cid)['items'][0]['title'], 'Updated title')
        self.accept(self.suggestion('102'))
        self.assertEqual(self.store.tree(self.cid)['completeness']['in_library'], 1)

    def test_c2_external_collection_publication_never_owned_by_constituents(self):
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(1,1,102,'1',1)")
        self.db.execute("INSERT INTO files(id,filepath,size) VALUES(1,'fixture.cbz',1)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(2,900,'Omnibus',1,'omnibus')")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number) VALUES(2,2,901,'1')")
        self.db.commit()
        preview = claim_preview(self.db.cursor(), 2, PublicationRef('comicvine', '102'), ClaimKind.COMPLETE, manual=True)
        confirm_claim(self.db.cursor(), 2, PublicationRef('comicvine', '102'), ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        self.db.execute("INSERT INTO bibliographic_graph_snapshots VALUES('fixture','comicvine','fixture','fixture','fixture',0,0,0,0,0,0,'fixture')")
        self.db.execute("INSERT INTO bibliographic_issue_refs VALUES('comicvine','901','900','Omnibus','1','',0,'fixture')")
        self.db.execute('DELETE FROM volumes WHERE id=2')
        self.db.commit()
        claims = self.db.execute('SELECT * FROM bibliographic_content_claims').fetchall()
        self.db.row_factory = sqlite3.Row
        wanted_before = wanted_rows(SimpleNamespace(db=self.db, clock=lambda: 0))
        self.db.row_factory = None
        self.accept(self.suggestion())
        publication = self.store.publications(self.cid)['items'][0]
        self.assertEqual(publication['status'], 'external')
        self.assertEqual(publication['content_context']['state'], 'complete_known_content')
        self.assertEqual(self.store.tree(self.cid)['completeness']['in_library'], 0)
        self.assertEqual(claims, self.db.execute('SELECT * FROM bibliographic_content_claims').fetchall())
        self.db.row_factory = sqlite3.Row
        self.assertEqual(wanted_before, wanted_rows(SimpleNamespace(db=self.db, clock=lambda: 0)))
        self.db.row_factory = None
        self.db.execute("UPDATE bibliographic_content_claims SET kind='partial_issue_content'")
        self.db.commit()
        self.assertEqual(self.store.publications(self.cid)['items'][0]['content_context']['state'], 'partial_known_content')

    def test_monitor_multi_membership_and_noop_move_blocked(self):
        self.store.add_local(self.root, self.revision(), 1)
        other = self.store.create('Other', monitoring='monitored')
        self.store.add_local(other['nodes'][0]['id'], 0, 1)
        self.assertTrue(self.store.publications(self.cid)['items'][0]['effective_monitored'])
        before = list(self.db.iterdump())
        with self.assertRaises(CollectionError):
            self.store.membership(self.root, self.revision(), 1, 'move', self.root)
        self.assertEqual(before, list(self.db.iterdump()))

    def test_actual_provider_switch_aba_preserves_membership(self):
        import TProviderSwitchApply as switch
        fixture = switch.SwitchApplyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.source()
        store = CollectionStore(fixture.cursor)
        tree = store.create('Family')
        node = tree['nodes'][0]['id']
        store.add_local(node, 0, 1)
        membership = fixture.db.execute('SELECT * FROM collection_memberships').fetchall()
        publication = fixture.db.execute('SELECT * FROM collection_publications').fetchall()
        fixture.apply(fixture.review(switch.remote('metron')))
        fixture.apply(fixture.review(switch.remote('comicvine', parent='100', first=101)))
        self.assertEqual(membership, fixture.db.execute('SELECT * FROM collection_memberships').fetchall())
        self.assertEqual(publication, fixture.db.execute('SELECT * FROM collection_publications').fetchall())
        self.assertEqual(store.publications(tree['id'])['items'][0]['local_volume_id'], 1)
        self.assertEqual(store.tree(tree['id'])['completeness']['total'], 1)

    def test_actual_metadata_repair_changes_display_not_membership(self):
        import TMetadataRepairApply as repair
        fixture = repair.MetadataRepairApplyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = CollectionStore(fixture.cursor)
        tree = store.create('Family')
        store.add_local(tree['nodes'][0]['id'], 0, 1)
        before = fixture.db.execute('SELECT * FROM collection_memberships').fetchall()
        fixture.apply(fixture.review())
        self.assertEqual(before, fixture.db.execute('SELECT * FROM collection_memberships').fetchall())
        self.assertEqual(store.publications(tree['id'])['items'][0]['title'], 'Target comicvine')

    def test_actual_quarantine_preserves_local_publication_registration(self):
        import TQuarantineExecution as quarantine
        fixture = quarantine.QuarantineExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = CollectionStore(fixture.db.cursor())
        tree = store.create('Family')
        store.add_local(tree['nodes'][0]['id'], 0, 1)
        before = fixture.db.execute('SELECT * FROM collection_memberships').fetchall()
        fixture.register_fixture()
        fixture.executor.apply_job(fixture.job)
        fixture.assert_inactive()
        self.assertEqual(before, fixture.db.execute('SELECT * FROM collection_memberships').fetchall())
        self.assertEqual(store.tree(tree['id'])['completeness']['in_library'], 1)

    def test_owned_omnibus_does_not_own_external_singles(self):
        self.db.execute("UPDATE volumes SET title='Omnibus' WHERE id=1")
        self.db.commit()
        self.store.add_local(self.root, self.revision(), 1)
        self.accept(self.suggestion('999'))
        before = self.db.execute('SELECT * FROM bibliographic_content_claims').fetchall()
        pubs = self.store.publications(self.cid)['items']
        self.assertEqual([p['status'] for p in pubs], ['in_library', 'external'])
        self.store.membership(self.root, self.revision(), pubs[1]['id'], 'remove')
        self.assertEqual(before, self.db.execute('SELECT * FROM bibliographic_content_claims').fetchall())

    def test_paging_reorder_constraints_and_unknown_kind(self):
        self.store.add_local(self.root, self.revision(), 1)
        self.accept(self.suggestion())
        self.assertTrue(self.store.publications(self.cid, limit=1)['has_next'])
        self.assertEqual(self.store.publications(self.cid, offset=1, limit=1)['items'][0]['status'], 'external')
        self.store.membership(self.root, self.revision(), 1, 'edit', position=10)
        self.assertEqual(self.store.publications(self.cid, limit=1)['items'][0]['id'], 2)
        with self.assertRaises(CollectionError):
            self.store.edit_publication(self.root, self.revision(), 1, 'invented')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE collection_nodes SET monitoring='yes'")
        self.db.rollback()

    def test_schema67_organizer_compatibility(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from backend.internals.organization_jobs import JobStore
        with TemporaryDirectory() as folder:
            path = str(Path(folder) / 'db.sqlite')
            target = sqlite3.connect(path)
            self.db.backup(target)
            target.execute("INSERT OR REPLACE INTO config VALUES('database_version',67)")
            target.commit(); target.close()
            job_store = JobStore(path)
            job_store.close()


class CollectionsMigrationTests(TestCase):
    def test_migration_parity_reopen_and_preservation(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript(SCHEMA_66)
            db.execute("INSERT INTO config VALUES('database_version',66)")
            db.commit()
            with patch('backend.internals.db_migration.get_db', return_value=db.cursor()):
                _migrate_collections()
            self.assertEqual(db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 67)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            actual = db.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'collection%' ORDER BY name").fetchall()
        with sqlite3.connect(':memory:') as fresh:
            fresh.executescript(SCHEMA_67)
            self.assertEqual(actual, fresh.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'collection%' ORDER BY name").fetchall())

    def test_each_statement_failure_rolls_back(self):
        for index in range(len(STATEMENTS)):
            with self.subTest(index=index), sqlite3.connect(':memory:') as db:
                db.executescript(SCHEMA_66)
                db.execute("INSERT INTO config VALUES('database_version',66)")
                db.commit()
                before = list(db.iterdump())
                class Cursor:
                    def execute(self, sql, args=()):
                        if sql == STATEMENTS[index]:
                            raise RuntimeError('injected migration failure')
                        return db.execute(sql, args)
                with patch('backend.internals.db_migration.get_db', return_value=Cursor()), self.assertRaises(RuntimeError):
                    _migrate_collections()
                self.assertEqual(before, list(db.iterdump()))

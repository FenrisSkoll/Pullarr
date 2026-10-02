"""Batched complete DB ownership and shared subtree exclusion."""

import os
from pathlib import Path
from unittest import TestCase

from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.implementations.folder_inventory import inspect_folder
from backend.internals.folder_ownership import load_ownership
from backend.internals.organization_jobs import JobStore
from backend.internals.organization_reservations import ReservationIndex


class FolderOwnershipTests(TestCase):
    def setUp(self):
        from tests.TLibraryHealth import LibraryHealthTests
        self.fixture = LibraryHealthTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.path = self.fixture.comic()

    def load(self, **kwargs):
        return load_ownership(self.db.cursor(), (1,), **kwargs)

    def test_complete_loader_read_only_and_stable(self):
        before = tuple(self.db.iterdump())
        first, second = self.load(), self.load()
        self.assertEqual(first.volumes, second.volumes)
        self.assertEqual(first.volumes[0].registrations[0].direct, ((1, 1, False),))
        self.assertEqual(first.volumes[0].authority.generation, 0)
        self.assertEqual(tuple(self.db.iterdump()), before)
        item = first.volumes[0]
        self.assertTrue(inspect_folder(item.root, item.source, item.volume_id, item.registrations).complete)

    def test_outside_owned_file_not_omitted(self):
        outside = self.fixture.root / 'outside.cbz'
        outside.write_bytes(b'outside')
        fid = self.db.execute('INSERT INTO files(filepath,size) VALUES(?,?)', (str(outside), 7)).lastrowid
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,1)', (fid,))
        self.db.commit()
        item = self.load().volumes[0]
        self.assertEqual(len(item.registrations), 2)
        self.assertEqual(inspect_folder(item.root, item.source, 1, item.registrations).reason,
                         'registered_path_outside_source')

    def test_general_and_unowned_registered_paths_included(self):
        for name in ('cover.jpg', 'orphan.nfo'):
            path = self.fixture.volume / name
            path.write_bytes(b'ancillary')
            fid = self.db.execute('INSERT INTO files(filepath,size) VALUES(?,9)', (str(path),)).lastrowid
            if name == 'cover.jpg':
                self.db.execute("INSERT INTO volume_files(file_id,volume_id,file_type,forced) VALUES(?,1,'cover',0)", (fid,))
        self.db.commit()
        item = self.load().volumes[0]
        report = inspect_folder(item.root, item.source, 1, item.registrations)
        self.assertTrue(report.complete)
        self.assertEqual({e.classification for e in report.entries},
                         {'registered_direct', 'registered_general', 'registered_unowned'})

    def test_foreign_owned_file_inside_tree_not_hidden(self):
        self.db.execute("INSERT INTO volumes(id,title,root_folder,folder,metadata_provider,comicvine_id) VALUES(2,'Other',1,?,'comicvine',200)",
                        (str(self.fixture.root / 'Other'),))
        self.db.execute("INSERT INTO volume_files(file_id,volume_id,file_type,forced) SELECT id,2,'cover',0 FROM files")
        self.db.commit()
        item = self.load().volumes[0]
        self.assertEqual(inspect_folder(item.root, item.source, 1, item.registrations).reason, 'foreign_volume_file_owner')

    def test_generation_and_settings_change_evidence(self):
        before = self.load().volumes[0].digest
        self.db.execute('UPDATE volumes SET authority_generation=2 WHERE id=1')
        self.db.commit()
        self.assertNotEqual(before, self.load().volumes[0].digest)
        before = self.load().volumes[0].digest
        self.db.execute("UPDATE config SET value='New {series_name}' WHERE key='volume_folder_naming'")
        self.db.commit()
        self.assertNotEqual(before, self.load().volumes[0].digest)

    def test_bound_fail_closed_and_caller_transaction_preserved(self):
        self.db.execute("UPDATE volumes SET title='pending user work' WHERE id=1")
        with self.assertRaises(OrganizationError):
            self.load(row_limit=1)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT title FROM volumes').fetchone()[0], 'Example')

    def test_ownership_reads_do_not_scale_per_file(self):
        def selects():
            queries = []
            self.db.set_trace_callback(queries.append)
            self.load()
            self.db.set_trace_callback(None)
            return sum(q.lstrip().upper().startswith('SELECT') for q in queries)
        baseline = selects()
        self.db.executemany('INSERT INTO files(filepath,size) VALUES(?,1)',
                            ((str(self.fixture.volume / f'{i}.txt'),) for i in range(1000)))
        self.db.commit()
        self.assertEqual(selects(), baseline)

    def test_subtree_parent_child_and_sibling_rules(self):
        parent = str(self.fixture.volume)
        child = str(self.path)
        sibling = str(self.fixture.root / 'Example Other')
        index = ReservationIndex(((parent, 'folder'),))
        self.assertEqual(index.conflicts(child), ('folder',))
        self.assertEqual(index.conflicts(child.upper()), ('folder',))
        self.assertEqual(index.conflicts(sibling), ())
        self.assertEqual(index.conflicts(child, exclude='folder'), ())
        self.assertEqual(ReservationIndex(((child, 'file'),)).conflicts(parent), ('file',))

    def test_shared_registration_blocks_parent_and_child_atomically(self):
        from backend.base.organization_job import EXECUTOR_POLICY
        self.db.execute("INSERT OR REPLACE INTO config(key,value) VALUES('database_version',65)")
        self.db.commit()
        store = JobStore(str(self.fixture.database))
        self.addCleanup(store.close)
        source = str(self.fixture.volume)
        intent = dict(version=EXECUTOR_POLICY, source=source, target=source, effects=[])
        job = store.create(intent, 'parent', (os.path.normpath(source).casefold(),))
        for target in (str(self.path), str(self.fixture.root)):
            with self.assertRaises(OrganizationError) as error:
                store.create(intent, 'conflicting', (os.path.normpath(target).casefold(),))
            self.assertEqual(error.exception.code, ExecutionCode.BUSY)
        self.assertEqual(store.history(), (job,))
        store.create(intent, 'sibling', (str(self.fixture.root / 'Sibling').casefold(),))

    def test_monitor_defers_before_any_plan_or_job(self):
        from backend.base.organization_job import EXECUTOR_POLICY
        from backend.features.folder_monitor import stamp
        from backend.features.library_reconciliation import LibraryReconciler
        self.db.execute("INSERT OR REPLACE INTO config(key,value) VALUES('database_version',65)")
        self.db.commit()
        store = JobStore(str(self.fixture.database))
        self.addCleanup(store.close)
        source = str(self.fixture.volume)
        intent = dict(version=EXECUTOR_POLICY, source=source, target=source, effects=[])
        store.create(intent, 'folder', (source.casefold(),))
        outcome = LibraryReconciler(str(self.fixture.database), self.db)(1, stamp(str(self.path)))
        self.assertEqual(outcome.status, 'pending')
        self.assertEqual(outcome.reason, 'organization_job_owns_path')
        self.assertEqual(len(store.history()), 1)

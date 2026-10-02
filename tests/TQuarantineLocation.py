"""Read-only storage admission, not quarantine execution acceptance."""

import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TLibraryHealth as health_fixture

from backend.base.custom_exceptions import RootFolderInvalid
from backend.base.duplicate_review import DuplicateReviewError
from backend.implementations.quarantine_location import (
    DIRECTORY, observe_location, reject_quarantine_root)
from backend.implementations.root_folders import RootFolders


class QuarantineLocationTests(TestCase):
    def setUp(self):
        self.fixture = health_fixture.LibraryHealthTests('test_inventory_never_opens_archive_or_hashes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.source = self.fixture.comic()
        self.identity = 'a' * 32

    def observe(self, identity=None):
        return observe_location(self.db.cursor(), 1, identity or self.identity)

    def test_same_device_outside_root_no_directories_created(self):
        before = tuple(self.db.iterdump()), tuple(self.fixture.base.rglob('*'))
        result = self.observe()
        self.assertEqual(result['storage'], str(self.fixture.base / DIRECTORY / '1'))
        self.assertEqual(result['source'], str(self.source))
        self.assertEqual(result['target'], str(self.fixture.base / DIRECTORY / '1' / self.identity / self.source.name))
        self.assertEqual(result['device'], self.source.stat().st_dev)
        self.assertEqual(before, (tuple(self.db.iterdump()), tuple(self.fixture.base.rglob('*'))))

    def test_distinct_ids_do_not_share_basename_target(self):
        self.assertNotEqual(self.observe()['target'], self.observe('b' * 32)['target'])
        self.assertEqual(self.observe(), self.observe())

    def test_unknown_file_and_untrusted_identity_rejected(self):
        for identity in ('../escape', 'A' * 32, 'a' * 33, ''):
            with self.subTest(identity=identity), self.assertRaises(DuplicateReviewError):
                observe_location(self.db.cursor(), 1, identity)
        with self.assertRaisesRegex(DuplicateReviewError, 'active_file_required'):
            observe_location(self.db.cursor(), 999, self.identity)

    def test_root_at_filesystem_root_is_not_safe_storage(self):
        self.db.execute('UPDATE root_folders SET folder=?', (self.fixture.root.anchor,))
        with self.assertRaisesRegex(DuplicateReviewError, 'outside_managed_tree'):
            self.observe()

    def test_other_library_root_overlap_rejected(self):
        self.db.execute('INSERT INTO root_folders(id,folder) VALUES(2,?)', (str(self.fixture.base / DIRECTORY),))
        with self.assertRaisesRegex(DuplicateReviewError, 'overlaps_library_root'):
            self.observe()

    def test_foreign_registered_path_blocks_storage(self):
        target = self.fixture.base / DIRECTORY / '1' / 'foreign.cbz'
        self.db.execute('INSERT INTO files(filepath,size) VALUES(?,1)', (str(target),))
        with self.assertRaisesRegex(DuplicateReviewError, 'storage_registered'):
            self.observe()

    def test_nonwritable_and_occupied_target(self):
        with patch('backend.implementations.quarantine_location.os.access', return_value=False):
            with self.assertRaisesRegex(DuplicateReviewError, 'not_writable'):
                self.observe()
        target = self.fixture.base / DIRECTORY / '1' / self.identity / self.source.name
        target.parent.mkdir(parents=True)
        target.write_bytes(b'do not overwrite')
        with self.assertRaisesRegex(DuplicateReviewError, 'target_occupied'):
            self.observe()
        self.assertEqual(target.read_bytes(), b'do not overwrite')

    def test_cross_device_and_unknown_device(self):
        real = os.lstat
        parent = str(self.fixture.base)
        for device, reason in ((0, 'device_unknown'), (self.source.stat().st_dev + 1, 'cross_device')):
            def lstat(path, *args, **kwargs):
                value = real(path, *args, **kwargs)
                if str(path) == parent:
                    return SimpleNamespace(st_dev=device, st_ino=value.st_ino, st_mode=value.st_mode,
                                           st_file_attributes=getattr(value, 'st_file_attributes', 0))
                return value
            with self.subTest(device=device), patch('os.lstat', side_effect=lstat):
                with self.assertRaisesRegex(DuplicateReviewError, reason):
                    self.observe()

    def test_library_root_api_rejects_storage_namespace(self):
        target = self.fixture.base / DIRECTORY / '1'
        target.mkdir(parents=True)
        with patch('backend.implementations.root_folders.get_db', side_effect=self.db.cursor):
            with self.assertRaises(RootFolderInvalid):
                RootFolders().add(str(target))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM root_folders').fetchone(), (1,))

    def test_namespace_descendants_and_case_equivalence_rejected(self):
        for path in (self.fixture.base / DIRECTORY, self.fixture.base / DIRECTORY.upper() / '1' / 'child'):
            with self.subTest(path=path), self.assertRaisesRegex(DuplicateReviewError, 'not_library_root'):
                reject_quarantine_root(self.db.cursor(), str(path))
        reject_quarantine_root(self.db.cursor(), str(self.fixture.base / 'independent'))

    def test_root_rename_rejected_before_directory_creation(self):
        target = self.fixture.base / DIRECTORY / 'new-root'
        with patch('backend.implementations.root_folders.get_db', side_effect=self.db.cursor):
            with self.assertRaises(RootFolderInvalid):
                RootFolders().rename(1, str(target))
        self.assertFalse(target.exists())

    def test_read_bounds_fail_closed(self):
        with patch('backend.implementations.quarantine_location.MAX_ROOTS', 0):
            with self.assertRaisesRegex(DuplicateReviewError, 'root_snapshot_limit'):
                self.observe()
        with patch('backend.implementations.quarantine_location.MAX_FILES', 0):
            with self.assertRaisesRegex(DuplicateReviewError, 'file_snapshot_limit'):
                self.observe()

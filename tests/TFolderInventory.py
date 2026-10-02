"""Phase 8F read-only inventory foundation, not folder execution acceptance."""

import hashlib
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from backend.base.folder_inventory import (InventoryLimits, InventoryState,
                                           RegisteredTreeFile)
from backend.implementations.folder_inventory import inspect_folder


class FolderInventoryTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='kapowarr-folder-inventory-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'library'
        self.source = self.root / 'Example'
        self.source.mkdir(parents=True)
        self.comic = self.source / 'Issue 001.cbz'
        self.comic.write_bytes(b'archive bytes are never inspected')

    def scan(self, registrations=(), **kwargs):
        return inspect_folder(str(self.root), str(self.source), 1, registrations, **kwargs)

    def snapshot(self):
        return tuple((str(p.relative_to(self.root)), p.is_dir(), p.stat().st_mtime_ns,
                      None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest())
                     for p in sorted(self.root.rglob('*')))

    def test_complete_tree_includes_ancillary_general_and_nested_entries(self):
        nested = self.source / 'Covers'
        nested.mkdir()
        cover = nested / 'cover.jpg'
        cover.write_bytes(b'cover')
        (self.source / 'notes.nfo').write_text('preserve me')
        (self.source / 'empty').mkdir()
        registered = (RegisteredTreeFile(1, str(self.comic), ((1, 1, False),)),
                      RegisteredTreeFile(2, str(cover), general=((1, False, 'cover'),)))
        before = self.snapshot()
        with patch('builtins.open', side_effect=AssertionError('content read')), \
                patch('socket.socket', side_effect=AssertionError('network')):
            report = self.scan(registered)
        self.assertTrue(report.complete)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(report.entries), 5)
        self.assertEqual(report.inspected_entries, 10)
        self.assertEqual({e.classification for e in report.entries},
                         {'directory', 'registered_direct', 'registered_general', 'unregistered_regular_file'})
        self.assertFalse(report.page()['apply_available'])

    def test_deterministic_digest_and_detached_page(self):
        first, second = self.scan(), self.scan()
        self.assertEqual(first.digest, second.digest)
        first.page()['entries'][0]['relative'] = 'hostile replacement'
        self.assertEqual(first.digest, second.digest)
        with self.assertRaises(FrozenInstanceError):
            first.entries[0].relative = 'changed'

    def test_membership_and_file_stat_change_digest(self):
        original = self.scan()
        self.comic.write_bytes(b'different bytes')
        self.assertNotEqual(original.digest, self.scan().digest)
        current = self.scan()
        (self.source / 'notes.txt').write_text('new')
        self.assertNotEqual(current.digest, self.scan().digest)

    def test_missing_folder_is_not_empty_or_created(self):
        missing = self.root / 'missing'
        report = inspect_folder(str(self.root), str(missing), 1)
        self.assertEqual(report.state, InventoryState.INACCESSIBLE)
        self.assertFalse(missing.exists())
        self.assertFalse(report.complete)

    def test_empty_folder_complete(self):
        self.comic.unlink()
        self.assertTrue(self.scan().complete)
        self.assertEqual(self.scan().entries, ())

    def test_outside_and_root_itself_blocked(self):
        for path in (self.root, self.base, self.source / '..' / 'Example'):
            with self.subTest(path=path):
                report = inspect_folder(str(self.root), str(path), 1)
                self.assertEqual(report.state, InventoryState.UNSAFE)

    def test_missing_registered_file_and_foreign_owner(self):
        missing = RegisteredTreeFile(2, str(self.source / 'missing.cbz'), ((1, 2, False),))
        self.assertEqual(self.scan((missing,)).reason, 'registered_file_missing')
        foreign = RegisteredTreeFile(1, str(self.comic), ((2, 2, False),))
        self.assertEqual(self.scan((foreign,)).reason, 'foreign_volume_file_owner')

    def test_duplicate_and_outside_registered_paths(self):
        row = RegisteredTreeFile(1, str(self.comic))
        self.assertEqual(self.scan((row, replace(row, file_id=2))).reason, 'duplicate_registered_path_or_id')
        self.assertEqual(self.scan((replace(row, path=str(self.base / 'other')),)).reason,
                         'registered_path_outside_source')

    def test_registered_directory_is_not_file(self):
        folder = self.source / 'nested'
        folder.mkdir()
        self.assertEqual(self.scan((RegisteredTreeFile(1, str(folder)),)).reason,
                         'registered_path_type_or_case_conflict')

    def test_multiple_direct_issues_valid_not_coverage(self):
        row = RegisteredTreeFile(1, str(self.comic), ((1, 1, False), (1, 2, True)))
        report = self.scan((row,))
        self.assertTrue(report.complete)
        self.assertEqual(report.entries[0].registration, row)
        self.assertEqual(report.digest, self.scan((replace(row, direct=tuple(reversed(row.direct))),)).digest)

    def test_nonregular_entry_blocks_without_open(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO unavailable on this platform')
        os.mkfifo(self.source / 'pipe')
        self.assertEqual(self.scan().reason, 'unsupported_entry')

    def test_unknown_inode_blocks(self):
        real = os.lstat

        def no_inode(path):
            value = real(path)
            if str(path) == str(self.comic):
                fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
                fields['st_ino'] = 0
                return SimpleNamespace(**fields)
            return value

        with patch('os.lstat', side_effect=no_inode):
            self.assertEqual(self.scan().reason, 'unsupported_entry')

    def test_case_equivalent_entries_block_on_case_sensitive_filesystem(self):
        other = self.source / self.comic.name.upper()
        if other.exists():
            self.skipTest('filesystem is case insensitive')
        other.write_bytes(b'distinct')
        self.assertEqual(self.scan().reason, 'case_equivalent_entries')

    def test_entry_depth_path_and_registration_bounds(self):
        (self.source / 'two.txt').write_text('2')
        self.assertEqual(self.scan(limits=InventoryLimits(entries=1)).reason, 'entry_limit')
        self.assertEqual(self.scan(limits=InventoryLimits(path_bytes=1)).reason, 'path_bytes')
        (self.source / 'a' / 'b').mkdir(parents=True)
        self.assertEqual(self.scan(limits=InventoryLimits(depth=1)).reason, 'depth_limit')
        rows = (RegisteredTreeFile(1, str(self.comic)), RegisteredTreeFile(2, str(self.source / 'two.txt')))
        self.assertEqual(self.scan(rows, limits=InventoryLimits(entries=1)).reason, 'registration_limit')
        self.assertEqual(self.scan(rows, limits=InventoryLimits(path_bytes=1)).reason, 'registration_bytes')

    def test_deadline(self):
        self.assertEqual(self.scan(clock=iter((0.0, 31.0)).__next__).reason, 'deadline')

    def test_inaccessible_enumeration(self):
        with patch('os.scandir', side_effect=PermissionError('do not expose raw error')):
            report = self.scan()
        self.assertEqual(report.state, InventoryState.INACCESSIBLE)
        self.assertEqual(report.reason, 'filesystem_inspection_failed')

    def test_file_disappears_during_enumeration(self):
        real = os.scandir

        def remove_after_open(path):
            stream = real(path)
            children = list(stream)
            stream.close()
            self.comic.unlink()
            class Stream:
                def __enter__(self):
                    return iter(children)
                def __exit__(self, *args):
                    pass
            return Stream()

        with patch('os.scandir', side_effect=remove_after_open):
            self.assertEqual(self.scan().state, InventoryState.CHANGED)

    def test_second_pass_rejects_new_file(self):
        real = os.scandir
        calls = 0

        def observe(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.source / 'arrived.txt').write_text('new')
            return real(path)

        with patch('os.scandir', side_effect=observe):
            self.assertEqual(self.scan().state, InventoryState.CHANGED)

    def test_symlink_never_followed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        link = self.source / 'escape'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        report = self.scan()
        self.assertEqual(report.state, InventoryState.UNSAFE)
        self.assertEqual(report.reason, 'unsafe_path_or_reparse_point')

    def test_device_boundary(self):
        from backend.implementations.folder_inventory import _stamp
        original = _stamp

        def different_device(path):
            stamp = original(path)
            return replace(stamp, device=stamp.device + 1) if path == str(self.comic) else stamp

        with patch('backend.implementations.folder_inventory._stamp', side_effect=different_device):
            self.assertEqual(self.scan().reason, 'nested_device_boundary')

    def test_bounds_and_registration_contract_validation(self):
        for kwargs in ({'entries': 20001}, {'depth': 65}, {'seconds': 0}, {'entries': True}):
            with self.assertRaises(ValueError):
                InventoryLimits(**kwargs)
        with self.assertRaises(ValueError):
            RegisteredTreeFile(1, str(self.comic), direct=([1, 1, False],))
        with self.assertRaises(ValueError):
            self.scan().page(limit=101)

    def test_domain_dump_and_files_preserved(self):
        from tests.TLibraryHealth import LibraryHealthTests
        fixture = LibraryHealthTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        comic = fixture.comic()
        fid = fixture.db.execute('SELECT id FROM files').fetchone()[0]
        before = tuple(fixture.db.iterdump()), fixture.filesystem()
        report = inspect_folder(str(fixture.root), str(fixture.volume), 1,
                                (RegisteredTreeFile(fid, str(comic), ((1, 1, False),)),))
        self.assertTrue(report.complete)
        self.assertEqual(before, (tuple(fixture.db.iterdump()), fixture.filesystem()))
        self.assertEqual(fixture.db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(fixture.db.execute('PRAGMA foreign_key_check').fetchall(), [])

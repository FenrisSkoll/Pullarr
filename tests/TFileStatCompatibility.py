"""Path/fd timestamp compatibility must not weaken read-race detection."""
import os
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from backend.base.duplicate_review import DuplicateReviewError
from backend.implementations.duplicate_evidence import HashBudget
from backend.implementations.organization_filesystem import comparison_ctime


class FileStatCompatibilityTests(TestCase):
    def test_platform_timestamp_semantics(self):
        value = SimpleNamespace(st_birthtime_ns=10, st_ctime_ns=20)
        with patch('sys.platform', 'win32'):
            self.assertEqual(comparison_ctime(value), 10)
            self.assertEqual(comparison_ctime(SimpleNamespace(st_ctime_ns=30)), 30)
        with patch('sys.platform', 'linux'):
            self.assertEqual(comparison_ctime(value), 20)

    def test_rewritten_file_is_hashable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'file.cbz'
            path.write_bytes(b'original')
            path.write_bytes(b'rewritten payload')
            self.assertEqual(HashBudget().inspect(str(path))['digest'], sha256(path.read_bytes()).hexdigest())

    def test_descriptor_change_time_still_rejects_mutation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'file.cbz'
            path.write_bytes(b'payload')
            original = os.stat(path)
            fields = {name:getattr(original,name) for name in
                      ('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')}
            opened = SimpleNamespace(**fields, st_birthtime_ns=original.st_ctime_ns)
            finished = SimpleNamespace(**dict(fields, st_ctime_ns=original.st_ctime_ns+1),
                                       st_birthtime_ns=original.st_ctime_ns)
            with patch('backend.implementations.duplicate_evidence.os.fstat', side_effect=[opened,finished]), \
                    patch('backend.implementations.duplicate_evidence.comparison_ctime',
                          side_effect=lambda value:getattr(value,'st_birthtime_ns',value.st_ctime_ns)):
                with self.assertRaisesRegex(DuplicateReviewError, 'duplicate_hash_source_changed'):
                    HashBudget().inspect(str(path))

    def test_creation_and_change_time_can_differ_at_open(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'file.cbz'
            path.write_bytes(b'payload')
            original = os.stat(path)
            fields = {name:getattr(original,name) for name in
                      ('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')}
            descriptor = SimpleNamespace(**dict(fields,st_ctime_ns=original.st_ctime_ns+1),
                                         st_birthtime_ns=original.st_ctime_ns)
            with patch('backend.implementations.duplicate_evidence.os.fstat', return_value=descriptor), \
                    patch('backend.implementations.duplicate_evidence.comparison_ctime',
                          side_effect=lambda value:getattr(value,'st_birthtime_ns',value.st_ctime_ns)):
                self.assertEqual(HashBudget().inspect(str(path))['digest'], sha256(b'payload').hexdigest())

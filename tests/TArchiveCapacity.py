"""Generated streaming fixtures, never multi-gigabyte checked-in artifacts."""
import struct
import tempfile
import zlib
from hashlib import sha256
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from backend.implementations.archive_normalization import inspect, normalize
from backend.implementations.file_quality import analyze


class ArchiveCapacityTests(TestCase):
    def test_gib_member_and_expanded_size_are_not_admission_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'large.cbz'
            block = bytes(1024 * 1024)
            with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
                with archive.open('page.png', 'w', force_zip64=True) as page:
                    for _ in range(1025):
                        page.write(block)
            before = path.stat()
            content = sha256(path.read_bytes()).digest()
            facts = inspect(str(path))
            self.assertEqual(facts['expanded_bytes'], 1025 * len(block))
            self.assertEqual(facts['status'], 'healthy')
            after = path.stat()
            # Reading legitimately advances atime on Linux. Identity, size,
            # modification time and compressed container bytes must not change.
            self.assertEqual((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                             (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns))
            self.assertEqual(sha256(path.read_bytes()).digest(), content)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_many_pages_and_members_stream_without_raster_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'source.cbr', Path(directory)/'target.cbz'
            with ZipFile(source, 'w') as archive:
                for index in range(10001):
                    archive.writestr(f'{index}.png', b'unchanged payload')
            with patch('backend.implementations.file_quality.Image.open', side_effect=AssertionError('decode')):
                result = normalize(str(source), str(target))
            self.assertEqual(result['pages'], 10001)
            with ZipFile(target) as archive:
                self.assertEqual(archive.read('10000.png'), b'unchanged payload')

    def test_large_dimensions_and_pillow_bomb_threshold_do_not_reject(self):
        # A valid PNG with >50MP and >30000px edge, generated one scanline at
        # a time. Header/CRC analysis requires no full raster allocation.
        width, height = 50001, 4001
        def chunk(kind, value):
            return struct.pack('>L', len(value)) + kind + value + struct.pack('>L', zlib.crc32(kind + value))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'large-page.cbz'
            with ZipFile(path, 'w') as archive:
                with archive.open('page.png', 'w') as page:
                    page.write(b'\x89PNG\r\n\x1a\n')
                    page.write(chunk(b'IHDR', struct.pack('>2L5B', width, height, 8, 2, 0, 0, 0)))
                    compressor = zlib.compressobj()
                    row = bytes(1 + 3 * width)
                    for _ in range(height):
                        data = compressor.compress(row)
                        if data:
                            page.write(chunk(b'IDAT', data))
                    page.write(chunk(b'IDAT', compressor.flush()))
                    page.write(chunk(b'IEND', b''))
            result = analyze(str(path))
            self.assertEqual(result['pixel_area']['maximum'], width * height)
            self.assertEqual(result['long_edge']['maximum'], width)

    def test_cancellation_during_member_stream_retains_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'source.cbz', Path(directory)/'target.cbz'
            with ZipFile(source, 'w', ZIP_DEFLATED) as archive:
                archive.writestr('page.png', bytes(4 * 1024 * 1024))
            calls = 0
            def cancelled():
                nonlocal calls
                calls += 1
                return calls > 3
            from backend.implementations.archive_normalization import \
                ArchiveFailure
            with self.assertRaisesRegex(ArchiveFailure, 'cancelled'):
                normalize(str(source), str(target), cancelled=cancelled)
            self.assertTrue(source.exists())
            # Closed handles allow immediate exclusive reopening on Windows.
            with source.open('r+b'), target.open('r+b'):
                pass

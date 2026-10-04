"""Real, disposable staging preparation; originals are never removed."""

import io
import shutil
import stat
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from backend.base.acquisition_intake import IntakeFailure
from backend.implementations.acquisition_preparation import prepare_artifacts


class PreparationTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='kapowarr-preparation-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def archive(self, name='Comic 001 (2020).cbz', entries=None):
        path = self.root / name
        with ZipFile(path, 'w') as archive:
            for key, value in (entries or {'page.jpg': b'disposable image'}).items():
                archive.writestr(key, value)
        return path

    def prepare(self, path, settings=None):
        before = path.read_bytes()
        result = prepare_artifacts((str(path),), str(self.root), settings or {}, 'test-intake')
        self.assertEqual(path.read_bytes(), before)
        return tuple(Path(p) for p in result)

    def test_plain_comic_preserved_without_derivative(self):
        source = self.archive()
        self.assertEqual(self.prepare(source), (source,))
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_extension_repair_preserves_bytes_and_original(self):
        source = self.archive('Comic 001 (2020).cbr')
        result, = self.prepare(source)
        self.assertEqual(result.suffix, '.cbz')
        self.assertEqual(result.read_bytes(), source.read_bytes())
        self.assertTrue(result.is_relative_to(self.root))

    def test_package_extracts_only_comics_inside_owned_scope(self):
        comic = io.BytesIO()
        with ZipFile(comic, 'w') as archive:
            archive.writestr('page.jpg', b'image')
        source = self.archive('package.zip', {'nested/Comic 001 (2020).cbz': comic.getvalue(),
                                              'readme.txt': b'retain in original'})
        result, = self.prepare(source)
        self.assertEqual(result.read_bytes(), comic.getvalue())
        self.assertTrue(result.is_relative_to(self.root))
        self.assertEqual(list(self.root.rglob('readme.txt')), [])

    def test_unsafe_archive_entries_preserve_source(self):
        for name in ('../escape.jpg', '/absolute.jpg', 'C:/device.jpg', 'NUL.jpg', 'file:stream'):
            with self.subTest(name=name):
                source = self.archive(entries={name: b'image'})
                before = source.read_bytes()
                with self.assertRaises(IntakeFailure):
                    self.prepare(source)
                self.assertEqual(source.read_bytes(), before)

    def test_symlink_member_rejected(self):
        source = self.root / 'unsafe.cbz'
        link = ZipInfo('link.jpg')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with ZipFile(source, 'w') as archive:
            archive.writestr(link, '../outside')
        with self.assertRaises(IntakeFailure):
            self.prepare(source)
        self.assertTrue(source.exists())

    def test_preparation_has_no_expanded_capacity_policy(self):
        from backend.implementations import acquisition_preparation
        source = self.archive()
        self.assertFalse(hasattr(acquisition_preparation, 'MAX_OUTPUT'))
        self.assertFalse(hasattr(acquisition_preparation, 'MAX_INPUT'))
        self.assertFalse(hasattr(acquisition_preparation, 'MAX_MEMBERS'))
        self.assertEqual(self.prepare(source), (source,))
        self.assertTrue(source.exists())

    def test_ancillary_only_archive_is_not_a_comic(self):
        source = self.archive(entries={'readme.nfo': b'not a comic', 'ComicInfo.xml': b'<ComicInfo/>'})
        with self.assertRaises(IntakeFailure):
            self.prepare(source)
        self.assertTrue(source.exists())

    def test_rar_writer_is_optional_and_absence_preserves_source(self):
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (32, 64), 'white').save(image, format='JPEG')
        source = self.archive(entries={'page.jpg': image.getvalue()})
        before = source.read_bytes()
        with patch('backend.base.archive_tools.which', return_value=None), self.assertRaises(IntakeFailure):
            self.prepare(source, {'convert': '1', 'format_preference': 'cbr'})
        self.assertEqual(source.read_bytes(), before)

    def test_rar_reader_uses_staging_only(self):
        rar = self.root / 'source.cbr'
        shutil.copyfile(Path(__file__).resolve().parents[2] / 'fixtures/archives/synthetic-rar5.cbr', rar)
        converted, = self.prepare(rar, {'convert': '1', 'format_preference': 'cbz'})
        with ZipFile(converted) as archive:
            self.assertEqual(sha256(archive.read('1.png')).hexdigest(),
                             '4cc487c54dc29c7f6724beedb0712304091a6e872f2197ff2f7f55c30157c1df')
        self.assertTrue(rar.exists())

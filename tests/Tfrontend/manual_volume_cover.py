"""Manual artwork through the real authenticated API and disposable SQLite."""

import sqlite3
from contextlib import closing
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from fixtures.comicvine_fetch import COVER
from fixtures.comicvine_search import FAKE_APP_KEY
from PIL import Image
from Tfrontend.library_import_contract import ImportAPIHarness

from backend.features.manual_covers import (MAX_INPUT, CoverValidationError,
                                            normalize_cover)
from backend.implementations.volumes import Library


def image_bytes(format='PNG', color=(10, 80, 140)):
    image = Image.new('RGB', (90, 135), color)
    output = BytesIO()
    image.save(output, format=format)
    return output.getvalue()


class ManualVolumeCoverAPI(ImportAPIHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('frontend.api.get_db', side_effect=self.db.cursor)
        self.volume_id = self.add_volume()
        self.path = f'/api/volumes/{self.volume_id}/manual-cover'
        self.query = {'api_key': FAKE_APP_KEY}

    def upload(self, raw, filename='cover.png'):
        return self.client.post(self.path, data={'file': (BytesIO(raw), filename)},
                                content_type='multipart/form-data', query_string=self.query)

    def source(self):
        response = self.client.get(f'/api/volumes/{self.volume_id}',
                                   query_string={**self.query, 'metadata': 'true'})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['result']['cover_source']

    def cover(self):
        return self.client.get(f'/api/volumes/{self.volume_id}/cover', query_string=self.query)

    def test_upload_replace_refresh_remove_and_backup(self):
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder) VALUES(99,9999,'Other',1)")
        self.db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(99,?)', (b'other-cover',))
        self.assertEqual(self.source(), 'provider')
        self.assertEqual(self.cover().data, COVER)
        for format in ('JPEG', 'PNG', 'WEBP'):
            response = self.upload(image_bytes(format), f'cover.{format.lower()}')
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['result']['cover_source'], 'manual')
            self.assertTrue(response.get_json()['result']['provider_cover_available'])
            served = self.cover()
            self.assertEqual(served.headers['Cache-Control'], 'no-cache')
            with Image.open(BytesIO(served.data)) as image:
                self.assertEqual((image.format, image.size), ('JPEG', (90, 135)))
        self.assertEqual(self.source(), 'manual')
        selected = self.cover().data
        Library.get_volume(self.volume_id).update_cover(b'new-provider-cover')
        self.assertEqual(self.cover().data, selected)
        self.assertEqual(self.db.execute('SELECT cover FROM volumes_covers WHERE volume_id=?',
                                         (self.volume_id,)).fetchone()[0], b'new-provider-cover')
        self.db.commit()
        with closing(sqlite3.connect(':memory:')) as backup:
            self.db.backup(backup)
            self.assertEqual(backup.execute('SELECT cover FROM manual_volume_covers').fetchone()[0], selected)
            with closing(sqlite3.connect(':memory:')) as restored:
                backup.backup(restored)
                self.assertEqual(restored.execute('SELECT cover FROM manual_volume_covers').fetchone()[0], selected)
        removed = self.client.delete(self.path, query_string=self.query)
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.get_json()['result']['cover_source'], 'provider')
        self.assertEqual(self.cover().data, b'new-provider-cover')
        self.assertEqual(self.db.execute('SELECT cover FROM volumes_covers WHERE volume_id=99').fetchone()[0],
                         b'other-cover')

    def test_missing_provider_placeholder_and_cascade(self):
        self.db.execute('UPDATE volumes_covers SET cover=NULL WHERE volume_id=?', (self.volume_id,))
        self.assertEqual(self.source(), 'missing')
        self.assertEqual(self.cover().status_code, 302)
        self.assertIn('cover-placeholder.svg', self.cover().headers['Location'])
        self.db.execute('UPDATE volumes_covers SET cover=? WHERE volume_id=?', (b'', self.volume_id))
        self.assertEqual(self.source(), 'missing')
        self.assertEqual(self.cover().status_code, 302)
        uploaded = self.upload(image_bytes())
        self.assertEqual(uploaded.status_code, 200)
        self.assertFalse(uploaded.get_json()['result']['provider_cover_available'])
        self.assertEqual(self.source(), 'manual')
        self.db.execute('DELETE FROM volumes WHERE id=?', (self.volume_id,))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM manual_volume_covers').fetchone()[0], 0)

    def test_rejected_upload_preserves_existing_artwork(self):
        self.assertEqual(self.upload(image_bytes()).status_code, 200)
        before = self.cover().data
        cases = ((b'', 400), (b'not an image', 400),
                 (image_bytes('PNG')[:20], 400), (b'x' * (MAX_INPUT + 1), 413))
        for raw, expected in cases:
            with self.subTest(size=len(raw)):
                self.assertEqual(self.upload(raw).status_code, expected)
                self.assertEqual(self.cover().data, before)
        self.assertEqual(self.client.post(self.path, data={}, query_string=self.query).status_code, 400)
        self.assertEqual(self.client.post(self.path, data={'file': (BytesIO(image_bytes()), 'fake.svg')},
                                          content_type='multipart/form-data', query_string=self.query).status_code, 200)
        self.assertEqual(self.client.post(self.path, data={'file': (BytesIO(b'<svg/>'), 'fake.png')},
                                          content_type='multipart/form-data', query_string=self.query).status_code, 400)
        self.assertNotEqual(self.client.post(self.path, data={'file': (BytesIO(image_bytes()), 'x.png')},
                                             content_type='multipart/form-data').status_code, 200)
        self.assertEqual(self.client.post('/api/volumes/999999/manual-cover',
            data={'file': (BytesIO(image_bytes()), 'x.png')}, content_type='multipart/form-data',
            query_string=self.query).status_code, 404)

    def test_animated_and_bomb_rejected(self):
        frames = [Image.new('RGB', (10, 10), color) for color in ('red', 'blue')]
        output = BytesIO()
        frames[0].save(output, format='WEBP', save_all=True, append_images=frames[1:])
        self.assertEqual(self.upload(output.getvalue()).status_code, 400)
        with patch('backend.features.manual_covers.Image.open',
                   side_effect=Image.DecompressionBombError()):
            with self.assertRaises(CoverValidationError):
                normalize_cover(image_bytes())
        with patch('backend.features.manual_covers.Image.open', side_effect=SyntaxError()):
            with self.assertRaises(CoverValidationError):
                normalize_cover(image_bytes())

    def test_orientation_and_transparency_are_normalized_without_metadata(self):
        oriented = Image.new('RGB', (20, 40), 'blue')
        exif = Image.Exif()
        exif[274] = 6
        raw = BytesIO()
        oriented.save(raw, format='JPEG', exif=exif)
        with Image.open(BytesIO(normalize_cover(raw.getvalue()))) as result:
            self.assertEqual(result.size, (40, 20))
            self.assertNotIn(274, result.getexif())
        transparent = Image.new('RGBA', (20, 40), (0, 0, 0, 0))
        raw = BytesIO()
        transparent.save(raw, format='PNG')
        with Image.open(BytesIO(normalize_cover(raw.getvalue()))) as result:
            self.assertEqual(result.mode, 'RGB')
            self.assertTrue(all(channel > 240 for channel in result.getpixel((10, 10))))
        large = Image.new('RGB', (2000, 3000), 'teal')
        raw = BytesIO()
        large.save(raw, format='PNG')
        with Image.open(BytesIO(normalize_cover(raw.getvalue()))) as result:
            self.assertEqual(result.size, (1600, 2400))

    def test_failed_database_replace_rolls_back_old_cover(self):
        from backend.features import manual_covers

        self.assertEqual(self.upload(image_bytes()).status_code, 200)
        before = self.cover().data
        original = manual_covers.save_manual_cover

        def fail_after_write(cursor, volume_id, cover):
            original(cursor, volume_id, cover)
            raise RuntimeError('synthetic database failure')

        with patch('backend.features.manual_covers.save_manual_cover',
                   side_effect=fail_after_write), patch.object(self.app.logger, 'error'):
            self.assertEqual(self.upload(image_bytes(color=(200, 30, 20))).status_code, 500)
        self.assertEqual(self.cover().data, before)

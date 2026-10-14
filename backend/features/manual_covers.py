"""Bounded user artwork, stored separately from provider cover bytes."""

import warnings
from hashlib import sha256
from io import BytesIO
from time import time

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_INPUT = 10 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_OUTPUT = 4 * 1024 * 1024


class CoverValidationError(ValueError):
    pass


def normalize_cover(raw: bytes) -> bytes:
    """Decode actual JPEG/PNG/WebP pixels and discard source metadata."""
    if not raw:
        raise CoverValidationError('empty_cover')
    if len(raw) > MAX_INPUT:
        raise CoverValidationError('cover_too_large')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as probe:
                if (probe.format not in {'JPEG', 'PNG', 'WEBP'} or
                        getattr(probe, 'n_frames', 1) != 1):
                    raise CoverValidationError('unsupported_cover_format')
                width, height = probe.size
                if (width < 1 or height < 1 or width * height > MAX_PIXELS
                        or width > 12000 or height > 12000):
                    raise CoverValidationError('cover_dimensions_exceeded')
                probe.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
                oriented = ImageOps.exif_transpose(image)
                oriented.thumbnail((1600, 2400), Image.Resampling.LANCZOS)
                if oriented.mode in ('RGBA', 'LA') or 'transparency' in oriented.info:
                    rgba = oriented.convert('RGBA')
                    background = Image.new('RGB', rgba.size, (255, 255, 255))
                    background.paste(rgba, mask=rgba.getchannel('A'))
                    oriented = background
                else:
                    oriented = oriented.convert('RGB')
                output = BytesIO()
                oriented.save(output, format='JPEG', quality=88, optimize=True)
                result = output.getvalue()
                if len(result) > MAX_OUTPUT:
                    raise CoverValidationError('cover_output_too_large')
                return result
    except CoverValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise CoverValidationError('invalid_cover_image') from error


def save_manual_cover(cursor, volume_id: int, cover: bytes) -> str:
    digest = sha256(cover).hexdigest()
    cursor.execute('''INSERT INTO manual_volume_covers(volume_id,cover,digest,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(volume_id) DO UPDATE SET
        cover=excluded.cover,digest=excluded.digest,updated_at=excluded.updated_at''',
        (volume_id, cover, digest, int(time())))
    return digest


def remove_manual_cover(cursor, volume_id: int) -> None:
    cursor.execute('DELETE FROM manual_volume_covers WHERE volume_id=?', (volume_id,))

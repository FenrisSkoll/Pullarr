"""Bounded read-only raster archive observations. No paths from API callers."""

import warnings
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from statistics import median
from zipfile import BadZipFile, ZipFile

from PIL import Image, UnidentifiedImageError

from backend.base.comicinfo import ComicInfoError
from backend.base.quality import ANALYZER_VERSION, QualityError
from backend.implementations.comicinfo import MAX_XML, _tree, parse_comicinfo
from backend.implementations.comicinfo_archive import (_bounded_directory,
                                                       _members, _stamp)
from backend.implementations.organization_filesystem import safe_path

MAX_PAGES = 5000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_EDGE = 30000
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif'}


def aggregate(values):
    """Nearest-rank p10/p90; orientation-independent, no fabricated DPI."""
    if not values:
        return {}
    values = sorted(values)
    return dict(minimum=values[0], median=median(values),
                p10=values[max(0, (len(values)+9)//10-1)],
                p90=values[max(0, (9*len(values)+9)//10-1)], maximum=values[-1])


def analyze(path: str, *, cancelled=lambda: False, progress=lambda done, total: None,
            verify_pixels=False):
    """Only trusted service-admitted paths. Pixel validation is an import gate.

    Routine analysis uses lazy Image.open/verify and full archive-member CRCs,
    not raster rendering. Upgrade admission additionally validates decodability.
    One bounded image is held at a time; source bytes are never rewritten.
    """
    safe_path(path)
    before = _stamp(path)
    if cancelled():
        raise QualityError('cancelled')
    result = dict(analyzer=ANALYZER_VERSION, container=Path(path).suffix.lower().lstrip('.'),
                  integrity='unavailable', validation='pixels' if verify_pixels else 'headers_and_crc',
                  pages=0, readable=0, unreadable=0, codecs={}, metadata=[], spreads=0,
                  short_edge={}, long_edge={}, pixel_area={}, size=before.size, stamp=asdict(before))
    digest = sha256()
    with open(path, 'rb') as stream:
        while chunk := stream.read(1024*1024):
            if cancelled():
                raise QualityError('cancelled')
            digest.update(chunk)
    result['sha256'] = digest.hexdigest()
    if result['container'] not in ('cbz', 'zip'):
        if before != _stamp(path):
            raise QualityError('file_changed')
        return result
    short, long, area, codecs = [], [], [], Counter()
    try:
        _bounded_directory(path)
        with ZipFile(path) as archive:
            members, _ = _members(archive)
            if sum(m.file_size for m in members) > MAX_EXPANDED_BYTES:
                raise QualityError('analysis_bound')
            page_count = sum(Path(m.filename).suffix.casefold() in IMAGE_SUFFIXES for m in members if not m.is_dir())
            if not 1 <= page_count <= MAX_PAGES:
                raise QualityError('page_bound')
            result['pages'] = page_count
            for member in members:
                if cancelled():
                    raise QualityError('cancelled')
                if member.is_dir():
                    continue
                is_page = Path(member.filename).suffix.casefold() in IMAGE_SUFFIXES
                metadata = Path(member.filename).name.casefold()
                if member.file_size > MAX_MEMBER_BYTES:
                    raise QualityError('analysis_bound')
                # Full bounded member read validates CRC, including non-page data.
                with archive.open(member) as source:
                    data = source.read(MAX_MEMBER_BYTES+1)
                if len(data) != member.file_size or len(data) > MAX_MEMBER_BYTES:
                    raise QualityError('archive_invalid')
                if metadata in ('comicinfo.xml', 'metroninfo.xml'):
                    if len(data) > MAX_XML:
                        raise QualityError('metadata_bound')
                    if metadata == 'comicinfo.xml':
                        parse_comicinfo(data)
                    else:
                        # Diagnostic presence only, but the same encoding-aware
                        # declaration/depth guard validates XML before admission.
                        _tree(data, expected_root=None)
                    result['metadata'].append(metadata)
                if not is_page:
                    continue
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(BytesIO(data)) as image:
                        width, height = image.size
                        if min(width, height) < 1 or max(width, height) > MAX_EDGE or width*height > MAX_PIXELS:
                            raise QualityError('dimension_bound')
                        codec = image.format or 'unknown'
                        if codec not in ('JPEG', 'PNG', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
                            raise QualityError('unsupported_page')
                        if codec == 'JPEG' and not data.rstrip().endswith(b'\xff\xd9'):
                            raise QualityError('image_invalid')
                        if verify_pixels:
                            image.load()
                        else:
                            image.verify()
                short.append(min(width, height))
                long.append(max(width, height))
                area.append(width*height)
                codecs[codec] += 1
                result['spreads'] += int(width > height)
                progress(len(short), page_count)
        result.update(integrity='valid', readable=len(short), codecs=dict(sorted(codecs.items())),
                      short_edge=aggregate(short), long_edge=aggregate(long), pixel_area=aggregate(area))
    except QualityError:
        raise
    except (BadZipFile, ComicInfoError, UnidentifiedImageError, OSError, ValueError,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise QualityError('archive_invalid') from None
    if before != _stamp(path):
        raise QualityError('file_changed')
    return result

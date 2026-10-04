"""Bounded read-only raster archive observations. No paths from API callers."""

from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from statistics import median
from tempfile import TemporaryFile
from zipfile import BadZipFile, ZipFile

from PIL import Image, UnidentifiedImageError

from backend.base.comicinfo import ComicInfoError
from backend.base.quality import ANALYZER_VERSION, QualityError
from backend.implementations.comicinfo import MAX_XML, _tree, parse_comicinfo
from backend.implementations.comicinfo_archive import (_bounded_directory,
                                                       _members, _stamp)
from backend.implementations.organization_filesystem import safe_path

# Archive size is limited by available host resources, not Pillow's raster policy.
# Network artwork has its own explicit byte/dimension admission checks.
Image.MAX_IMAGE_PIXELS = None
CHUNK = 1024 * 1024
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
            verify_pixels=False, structural_only=False):
    """Only trusted service-admitted paths. Pixel validation is an import gate.

    Routine analysis uses lazy Image.open/verify and full archive-member CRCs,
    not raster rendering. Upgrade admission additionally validates decodability.
    Payloads are spooled to disk with fixed buffers; source bytes are never rewritten.
    Structural inspection does not decode pages. Explicit pixel validation may
    need raster memory proportional to an individual image.
    """
    safe_path(path)
    before = _stamp(path)
    if cancelled():
        raise QualityError('cancelled')
    result = dict(analyzer=ANALYZER_VERSION, container=Path(path).suffix.lower().lstrip('.'),
                  integrity='unavailable', validation='container_crc' if structural_only else 'pixels' if verify_pixels else 'headers_and_crc',
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
            page_count = sum(Path(m.filename).suffix.casefold() in IMAGE_SUFFIXES for m in members if not m.is_dir())
            if not page_count:
                raise QualityError('no_pages')
            result['pages'] = page_count
            for member in members:
                if cancelled():
                    raise QualityError('cancelled')
                if member.is_dir():
                    continue
                is_page = Path(member.filename).suffix.casefold() in IMAGE_SUFFIXES
                metadata = Path(member.filename).name.casefold()
                # CRC validation and spooling use fixed buffers. The seekable
                # disk spool avoids ZipExtFile/Pillow whole-member buffering.
                with TemporaryFile() as spool:
                    count = 0
                    tail = b''
                    with archive.open(member) as source:
                        while block := source.read(CHUNK):
                            if cancelled():
                                raise QualityError('cancelled')
                            count += len(block)
                            if count > member.file_size:
                                raise QualityError('archive_invalid')
                            if not structural_only and is_page:
                                spool.write(block)
                            if metadata in ('comicinfo.xml', 'metroninfo.xml') and member.file_size <= MAX_XML:
                                spool.write(block)
                            stripped = block.rstrip()
                            if stripped:
                                tail = (tail + stripped)[-2:]
                    if count != member.file_size:
                        raise QualityError('archive_invalid')
                    if metadata in ('comicinfo.xml', 'metroninfo.xml'):
                        # Large XML is retained byte-for-byte; optional metadata
                        # parsing remains bounded and cannot reject the container.
                        if member.file_size <= MAX_XML:
                            spool.seek(0)
                            data = spool.read(MAX_XML + 1)
                            if metadata == 'comicinfo.xml':
                                parse_comicinfo(data)
                            else:
                                _tree(data, expected_root=None)
                        result['metadata'].append(metadata)
                    if not is_page:
                        continue
                    if structural_only:
                        progress(sum(codecs.values()) + 1, page_count)
                        codecs['uninspected'] += 1
                        continue
                    spool.seek(0)
                    with Image.open(spool) as image:
                        width, height = image.size
                        if min(width, height) < 1:
                            raise QualityError('image_invalid')
                        codec = image.format or 'unknown'
                        if codec not in ('JPEG', 'PNG', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
                            raise QualityError('unsupported_page')
                        if codec == 'JPEG' and tail != b'\xff\xd9':
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
            SyntaxError):
        raise QualityError('archive_invalid') from None
    if before != _stamp(path):
        raise QualityError('file_changed')
    return result

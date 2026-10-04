"""Explicit ZIP inspection/rewrite. No extraction, conversion, DB or providers."""

import os
import stat
import struct
import zlib
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Optional, Tuple
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from backend.base.comicinfo import (ComicInfoCode, ComicInfoDiagnostic,
                                    ComicInfoDocument, ComicInfoError)
from backend.base.definitions import FileConstants
from backend.base.import_candidate import FileObservation, InspectionState
from backend.implementations.comicinfo import MAX_XML, parse_comicinfo


@dataclass(frozen=True)
class ArchiveStamp:
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class ComicInfoInspection:
    path: str
    state: InspectionState
    stamp: Optional[ArchiveStamp]
    member: Optional[str] = None
    document: Optional[ComicInfoDocument] = None
    diagnostics: Tuple[ComicInfoDiagnostic, ...] = ()
    failed_xml: Optional[bytes] = None
    page_count: Optional[int] = None  # Header observation, not page CRC validation.


def _stamp(path: str) -> ArchiveStamp:
    value = os.lstat(path)
    if not stat.S_ISREG(value.st_mode):
        raise ComicInfoError(ComicInfoCode.UNSAFE_MEMBER)
    return ArchiveStamp(value.st_size, value.st_mtime_ns, value.st_ctime_ns,
                        value.st_dev, value.st_ino)


def _bounded_directory(path: str) -> None:
    """Validate the end record, including ZIP64, without a capacity policy.

    ZipFile validates the directory and member offsets. Multi-disk archives are
    unsupported; ZIP64 size/count fields are format features, not admission limits.
    The tail buffer is fixed regardless of archive size.
    """
    with open(path, 'rb') as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
    offset = tail.rfind(b'PK\x05\x06')
    if offset < 0 or len(tail) - offset < 22:
        raise ComicInfoError(ComicInfoCode.ARCHIVE_UNREADABLE)
    _, disk, start_disk, disk_count, count, directory_size, directory_offset, comment = struct.unpack(
        '<4s4H2LH', tail[offset:offset + 22])
    if len(tail) - offset != 22 + comment:
        raise ComicInfoError(ComicInfoCode.ARCHIVE_UNREADABLE)
    if disk or start_disk or disk_count != count:
        raise ComicInfoError(ComicInfoCode.UNSUPPORTED_FORMAT)
    if offset >= 20 and tail[offset - 20:offset - 16] == b'PK\x06\x07':
        _, zip_disk, record_offset, disks = struct.unpack('<4sLQL', tail[offset-20:offset])
        if zip_disk or disks != 1:
            raise ComicInfoError(ComicInfoCode.UNSUPPORTED_FORMAT)
        with open(path, 'rb') as source:
            source.seek(record_offset)
            record = source.read(56)
        if len(record) != 56 or record[:4] != b'PK\x06\x06':
            raise ComicInfoError(ComicInfoCode.ARCHIVE_UNREADABLE)
        _, length, _, _, disk, start_disk, disk_count, count, directory_size, directory_offset = struct.unpack('<4sQ2H2L4Q', record)
        if length < 44 or disk or start_disk or disk_count != count:
            raise ComicInfoError(ComicInfoCode.UNSUPPORTED_FORMAT)
    elif count == 65535 or directory_size == 0xffffffff or directory_offset == 0xffffffff:
        raise ComicInfoError(ComicInfoCode.ARCHIVE_UNREADABLE)
    if directory_offset + directory_size > size:
        raise ComicInfoError(ComicInfoCode.ARCHIVE_UNREADABLE)


def _members(archive: ZipFile) -> Tuple[Tuple[ZipInfo, ...], Optional[ZipInfo]]:
    members = tuple(archive.infolist())
    seen = set()
    metadata = []
    for member in members:
        name = member.filename
        normalized = name.replace('\\', '/')
        parts = normalized.rstrip('/').split('/')
        if (not name or member.orig_filename != name or normalized.startswith('/')
                or ':' in normalized or any(p in ('', '.', '..') for p in parts)
                or stat.S_ISLNK(member.external_attr >> 16)):
            raise ComicInfoError(ComicInfoCode.UNSAFE_MEMBER)
        key = normalized.casefold()
        if key in seen:
            raise ComicInfoError(ComicInfoCode.DUPLICATE_MEMBER)
        seen.add(key)
        if member.flag_bits & 1:
            raise ComicInfoError(ComicInfoCode.ENCRYPTED)
        if member.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
            raise ComicInfoError(ComicInfoCode.UNSUPPORTED_FORMAT)
        if parts[-1].casefold() == 'comicinfo.xml' and not member.is_dir():
            metadata.append(member)
    if len(metadata) > 1:
        raise ComicInfoError(ComicInfoCode.MULTIPLE_DOCUMENTS)
    chosen = metadata[0] if metadata else None
    return members, chosen


def inspect_comicinfo(path: str, observation: Optional[FileObservation] = None) -> ComicInfoInspection:
    stamp = None
    raw = None
    try:
        if Path(path).suffix.lower() not in ('.zip', '.cbz'):
            raise ComicInfoError(ComicInfoCode.UNSUPPORTED_FORMAT)
        stamp = _stamp(path)
        if observation is not None and (
            observation.path != path or observation.stat_state != InspectionState.PRESENT
            or (observation.size, observation.mtime_ns) != (stamp.size, stamp.mtime_ns)
        ):
            raise ComicInfoError(ComicInfoCode.STALE)
        _bounded_directory(path)
        with ZipFile(path) as archive:
            members, member = _members(archive)
            page_count = sum(not entry.is_dir() and entry.filename.lower().endswith(
                FileConstants.IMAGE_EXTENSIONS) for entry in members)
            document = None
            if member is not None:
                with archive.open(member) as source:
                    raw = source.read(MAX_XML + 1)
                document = parse_comicinfo(raw)
        if _stamp(path) != stamp:
            raise ComicInfoError(ComicInfoCode.STALE)
        return ComicInfoInspection(path, InspectionState.PRESENT if document else InspectionState.ABSENT,
                                   stamp, member.filename if member else None, document,
                                   document.diagnostics if document else (), page_count=page_count)
    except ComicInfoError as error:
        code = error.code
    except (OSError, BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
        code = ComicInfoCode.ARCHIVE_UNREADABLE
    return ComicInfoInspection(path, InspectionState.FAILED, stamp,
                               diagnostics=(ComicInfoDiagnostic(code),),
                               failed_xml=raw if raw is not None and len(raw) <= MAX_XML else None)


def write_comicinfo(inspection: ComicInfoInspection, xml: bytes, *,
                   temporary_path: Optional[str] = None,
                   checkpoint: Optional[Callable[[str, str], None]] = None,
                   replacement_guard=None) -> ComicInfoInspection:
    """Explicit bounded rewrite, requiring a successful prior inspection.

    Atomic single-path replacement where the filesystem supports it; not a
    history transaction or protection from an adversarial concurrent writer.
    """
    if (Path(inspection.path).suffix.lower() not in ('.cbz', '.zip')
            or inspection.state not in (InspectionState.PRESENT, InspectionState.ABSENT)
            or inspection.stamp is None):
        raise ComicInfoError(ComicInfoCode.WRITE_UNSUPPORTED)
    parse_comicinfo(xml)
    path = inspection.path
    if temporary_path is not None and (
        Path(temporary_path).parent != Path(path).parent or temporary_path == path
    ):
        raise ComicInfoError(ComicInfoCode.WRITE_UNSUPPORTED)
    temporary = None
    try:
        if _stamp(path) != inspection.stamp:
            raise ComicInfoError(ComicInfoCode.STALE)
        _bounded_directory(path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        with ZipFile(path) as source:
            members, metadata = _members(source)
            with (open(temporary_path, 'xb') if temporary_path is not None else
                  NamedTemporaryFile(prefix='.kapowarr-comicinfo-', suffix='.tmp',
                                     dir=str(Path(path).parent), delete=False)) as output:
                temporary = output.name
                if checkpoint is not None:
                    checkpoint('allocated', temporary)
                with ZipFile(output, 'w') as target:
                    target.comment = source.comment
                    for member in members:
                        if metadata is not None and member.filename == metadata.filename:
                            target.writestr(copy(member), xml)
                            continue
                        with source.open(member) as reader, target.open(copy(member), 'w') as writer:
                            count = 0
                            while True:
                                block = reader.read(1024 * 1024)
                                if not block:
                                    break
                                count += len(block)
                                if count > member.file_size:
                                    raise ComicInfoError(ComicInfoCode.LIMIT_EXCEEDED)
                                writer.write(block)
                    if metadata is None:
                        info = ZipInfo('ComicInfo.xml', (1980, 1, 1, 0, 0, 0))
                        info.compress_type = ZIP_DEFLATED
                        target.writestr(info, xml)
                output.flush()
                os.fsync(output.fileno())
        # Read CRCs before replacing, not after destroying the old source.
        _stamp(temporary)
        _bounded_directory(temporary)
        with ZipFile(temporary) as check:
            _members(check)
            if check.testzip() is not None:
                raise ComicInfoError(ComicInfoCode.WRITE_FAILED)
        os.chmod(temporary, mode)
        if _stamp(path) != inspection.stamp:
            raise ComicInfoError(ComicInfoCode.STALE)
        if checkpoint is not None:
            checkpoint('prepared', temporary)
        from contextlib import nullcontext
        with replacement_guard() if replacement_guard is not None else nullcontext():
            # Recheck after potentially waiting for the caller's write guard.
            if _stamp(path) != inspection.stamp:
                raise ComicInfoError(ComicInfoCode.STALE)
            os.replace(temporary, path)
        temporary = None
    except ComicInfoError:
        raise
    except OSError as error:
        raise ComicInfoError(ComicInfoCode.WRITE_FAILED, os_error=error.errno) from None
    except (BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
        raise ComicInfoError(ComicInfoCode.WRITE_FAILED) from None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return inspect_comicinfo(path)

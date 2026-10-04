"""Bounded container maintenance over the existing intake/archive primitives.

Trusted callers admit file identities. No extraction destination reaches RAR;
members stream into a new ZIP, never into the source or arbitrary member paths.
"""
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

import rarfile

from backend.base.acquisition_intake import IntakeFailure
from backend.base.comicinfo import ComicInfoError
from backend.base.quality import QualityError
from backend.implementations.acquisition_preparation import (_BoundedHeaders,
                                                             _member,
                                                             _rar_copy)
from backend.implementations.comicinfo_archive import (_bounded_directory,
                                                       _members, _stamp)
from backend.implementations.file_quality import IMAGE_SUFFIXES, analyze
from backend.implementations.organization_filesystem import artifact, safe_path

MAX_PATH = 1024
PAGE_TYPES = IMAGE_SUFFIXES
CHILD_TYPES = {'.cbr', '.rar', '.cbz', '.zip'}
CHUNK = 1024 * 1024


class ArchiveFailure(ValueError):
    """Safe fixed reason; no tool stderr or external member names."""


@contextmanager
def members(path):
    safe_path(path)
    _stamp(path)
    with open(path, 'rb') as source:
        magic = source.read(8)
        source.seek(0)
        if magic.startswith(b'PK'):
            _bounded_directory(path)
            with ZipFile(source) as archive:
                entries, _ = _members(archive)
                yield 'cbz', [(m.filename, m.file_size, m.is_dir(), m) for m in entries], archive
        elif magic.startswith(b'Rar!\x1a\x07'):
            def admission(info):
                # rarfile also reports main/end headers without a filename.
                if info.filename is not None:
                    _member(info.filename)
                    if len(info.filename)>MAX_PATH:
                        raise ArchiveFailure('unsafe_member')
            with rarfile.RarFile(_BoundedHeaders(source), info_callback=admission, errors='strict') as archive:
                if archive.needs_password():
                    raise ArchiveFailure('encrypted')
                if (len(archive.volumelist()) != 1 or archive._file_parser._main is None
                        or archive._file_parser._main.flags & rarfile.RAR_MAIN_VOLUME):
                    raise ArchiveFailure('multipart_unsupported')
                entries = archive.infolist()
                if any(m.is_symlink() or m.file_redir or m.needs_password() for m in entries):
                    raise ArchiveFailure('unsafe_member')
                yield 'cbr', [(m.filename, m.file_size, m.is_dir(), m) for m in entries], archive
        else:
            raise ArchiveFailure('unsupported_container')


def _admit(entries):
    if not entries:
        raise ArchiveFailure('archive_empty')
    seen, total, pages, metadata = set(), 0, 0, set()
    for name, size, directory, info in entries:
        normalized = _member(name)
        if ('\\' in name or len(name) > MAX_PATH or normalized.casefold() in seen
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or size < 0):
            raise ArchiveFailure('unsafe_member')
        seen.add(normalized.casefold())
        mode = getattr(info, 'mode', 0) or getattr(info, 'external_attr', 0) >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ArchiveFailure('unsafe_member')
        total += size
        if directory:
            continue
        suffix = Path(name).suffix.casefold()
        if suffix in CHILD_TYPES:
            raise ArchiveFailure('complete_issue_package_review')
        if suffix in PAGE_TYPES:
            pages += 1
        elif suffix == '.pdf':
            raise ArchiveFailure('unsupported_page')
        if Path(name).name.casefold() in ('comicinfo.xml', 'metroninfo.xml'):
            key = Path(name).name.casefold()
            if key in metadata:
                raise ArchiveFailure('metadata_invalid')
            metadata.add(key)
    if not pages:
        raise ArchiveFailure('no_pages')
    return total, pages


class _PayloadWriter:
    def __init__(self, output, limit, cancelled):
        self.output, self.limit, self.cancelled = output, limit, cancelled
        self.hash, self.count = sha256(), 0

    def write(self, data):
        if self.cancelled():
            raise ArchiveFailure('cancelled')
        self.count += len(data)
        if self.count > self.limit:
            raise ArchiveFailure('member_incomplete')
        self.hash.update(data)
        return self.output.write(data)


def normalize(source, target, *, cancelled=lambda: False):
    """Create an exclusive target; prove every retained member's exact bytes.

    Member order is preserved (not a newly invented page sort). Directory entries
    are retained. ZIP timestamps/permissions/compression are deterministic. Safe
    unknown members and metadata are preserved; no junk removal or XML rewriting.
    Caller owns temporary cleanup. Original is never renamed, written or removed.
    """
    if cancelled():
        raise ArchiveFailure('cancelled')
    safe_path(target)
    _stamp(source)
    before = artifact(source, cancelled=cancelled)
    manifest = []
    try:
        with members(source) as (container, entries, archive):
            expanded, pages = _admit(entries)
            if shutil.disk_usage(Path(target).parent).free < expanded + 64 * CHUNK:
                raise ArchiveFailure('insufficient_space')
            with open(target, 'xb') as output:
                with ZipFile(output, 'w', ZIP_STORED, allowZip64=True) as result:
                    for name, size, directory, member in entries:
                        if cancelled():
                            raise ArchiveFailure('cancelled')
                        info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                        info.create_system = 3
                        info.external_attr = ((stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o644)) << 16
                        info.file_size = size
                        with result.open(info, 'w') as destination:
                            writer = _PayloadWriter(destination, size, cancelled)
                            if not directory:
                                if container == 'cbz':
                                    with archive.open(member) as stream:
                                        while data := stream.read(CHUNK):
                                            writer.write(data)
                                else:
                                    _rar_copy(source, name, writer, size)
                        if writer.count != size:
                            raise ArchiveFailure('member_incomplete')
                        manifest.append((name, size, writer.hash.hexdigest()))
                output.flush()
                os.fsync(output.fileno())
        facts = analyze(target, cancelled=cancelled, structural_only=True)
        with ZipFile(target) as check:
            actual = []
            for member in check.infolist():
                digest = sha256()
                with check.open(member) as stream:
                    while data := stream.read(CHUNK):
                        if cancelled():
                            raise ArchiveFailure('cancelled')
                        digest.update(data)
                actual.append((member.filename, member.file_size, digest.hexdigest()))
        if manifest != actual or facts['pages'] != pages or artifact(source, cancelled=cancelled) != before:
            raise ArchiveFailure('verification_failed')
        from backend.internals.organization_jobs import canonical
        return dict(source_container=container, target_container='cbz', pages=pages,
                    members=len(manifest), expanded_bytes=expanded,
                    payload_digest=sha256(canonical(manifest).encode()).hexdigest(),
                    pages_preserved=True, metadata_action='preserved_bytes',
                    member_order='source_order_preserved', facts=facts, old=before,
                    incoming=artifact(target, cancelled=cancelled))
    except ArchiveFailure:
        raise
    except (IntakeFailure, ComicInfoError, QualityError, rarfile.Error, BadZipFile, OSError, ValueError, RuntimeError, SyntaxError, subprocess.SubprocessError):
        raise ArchiveFailure('archive_unreadable_or_unsafe') from None


def inspect(path, *, cancelled=lambda: False):
    """Observational check; CBR uses a bounded disposable verification workspace."""
    safe_path(path)
    _stamp(path)
    before = artifact(path, cancelled=cancelled)
    with members(path) as (container, entries, archive):
        expanded, pages = _admit(entries)
    if container == 'cbz' and Path(path).suffix.casefold() in ('.cbz','.zip'):
        facts = analyze(path, cancelled=cancelled, structural_only=True)
        return dict(container=container, pages=pages, members=len(entries), metadata=facts['metadata'],
                    expanded_bytes=expanded, old=before, status='healthy', facts=facts)
    with tempfile.TemporaryDirectory(prefix='pullarr-archive-check-') as directory:
        result = normalize(path, str(Path(directory) / 'verified.cbz'), cancelled=cancelled)
    return dict(container=container, pages=pages, members=len(entries), metadata=result['facts']['metadata'],
                expanded_bytes=expanded, old=before, status='convertible', facts=result['facts'])


def extract_legacy(source, destination):
    """Harden the existing explicit folder converter, without choosing identity.

    The caller retains its established matcher. Validate the entire member list
    before creating an exclusive workspace. No archive tool extracts paths.
    """
    safe_path(destination)
    _stamp(source)
    before = artifact(source)
    with members(source) as (container, entries, archive):
        seen, expanded = set(), 0
        for name, size, directory, info in entries:
            normalized = _member(name)
            mode = getattr(info, 'mode', 0) or getattr(info, 'external_attr', 0) >> 16
            if (normalized.casefold() in seen or '\\' in name or len(name)>MAX_PATH
                    or size<0
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ArchiveFailure('unsafe_member')
            seen.add(normalized.casefold()); expanded += size
        if shutil.disk_usage(Path(destination).parent).free < expanded+64*CHUNK:
            raise ArchiveFailure('insufficient_space')
        Path(destination).mkdir(exist_ok=False)
        try:
            for name, size, directory, member in entries:
                target = Path(destination)/_member(name)
                if directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    writer = _PayloadWriter(output, size, lambda: False)
                    if container=='cbz':
                        with archive.open(member) as stream:
                            while data := stream.read(CHUNK): writer.write(data)
                    else:
                        _rar_copy(source, name, writer, size)
                    if writer.count!=size:
                        raise ArchiveFailure('member_incomplete')
            if artifact(source)!=before:
                raise ArchiveFailure('stale_preview')
        except BaseException:
            # Only the exclusive directory created above; never source bytes.
            shutil.rmtree(destination)
            raise

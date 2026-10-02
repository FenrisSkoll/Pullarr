"""Bounded staging-only archive preparation. Never chooses library identity.

Original payloads are retained. Native RAR reads stream to application-controlled
files; extraction tools never receive an extraction destination. No shell, legacy
converter, matcher, FilesDB, scan or final-folder helper is used here.
"""

import os
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from threading import Timer
from zipfile import ZIP_DEFLATED, ZipFile

import rarfile

from backend.base.acquisition_intake import IntakeErrorCode as E, IntakeFailure
from backend.base.archive_tools import archive_executable
from backend.base.definitions import RAR_EXECUTABLES, FileConstants
from backend.base.files import folder_path
from backend.base.helpers import get_os_type
from backend.implementations.acquisition_paths import (COMIC_EXTENSIONS,
                                                       _portable_component,
                                                       contained)
from backend.implementations.comicinfo_archive import _bounded_directory

MAX_INPUT = 8 * 1024 ** 3
MAX_OUTPUT = 16 * 1024 ** 3
MAX_MEMBERS = 10000
CHUNK = 1024 * 1024
MAX_HEADER_BYTES = 16 * CHUNK
TOOL_TIMEOUT = 300


def _member(name: str) -> str:
    path = PurePosixPath(name.rstrip('/'))
    if (not name or len(name) > 2048 or path.is_absolute() or len(path.parts) > 32
            or any(p in ('.', '..') or not _portable_component(p) or any(ord(c) < 32 for c in p) for p in path.parts)):
        raise IntakeFailure(E.UNSAFE_PATH)
    return str(path)


class _BoundedHeaders:
    """RAR listing reads headers, never an unbounded advertised header allocation."""
    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_HEADER_BYTES

    def read(self, size=-1):
        if not 0 <= size <= min(CHUNK, self.remaining):
            raise IntakeFailure(E.PREPARATION)
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data

    def seek(self, *args):
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()


def _copy(stream, output, maximum: int) -> None:
    count = 0
    while True:
        block = stream.read(min(CHUNK, maximum - count + 1))
        if not block:
            break
        count += len(block)
        if count > maximum:
            raise IntakeFailure(E.PREPARATION)
        output.write(block)
    if count != maximum:
        raise IntakeFailure(E.PREPARATION)


def _rar_copy(source: str, member: str, output, size: int) -> None:
    executable = archive_executable()
    with subprocess.Popen([executable, 'p', '-inul', '-p-', '--', source, member],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL) as process:
        timer = Timer(TOOL_TIMEOUT, process.kill)
        timer.start()
        try:
            _copy(process.stdout, output, size)
            if process.wait(timeout=5) != 0:
                raise IntakeFailure(E.PREPARATION)
        finally:
            if process.poll() is None:
                process.kill()
            timer.cancel()
            timer.join()


def prepare_artifacts(paths: tuple[str, ...], root: str, settings: dict, identifier: str,
                      *, _depth: int = 0, _budget: dict | None = None) -> tuple[str, ...]:
    """Preserve comics, unwrap packages, repair extension or convert in staging.

    Whole preparation failure preserves originals and any owned derivatives for
    review. It never admits a partially extracted package. Output count/expanded
    bytes are operation-wide, including native RAR streams.
    """
    outputs = []
    budget = _budget if _budget is not None else {'expanded': 0, 'entries': 0}
    convert = str(settings.get('convert', '0')).lower() in ('1', 'true')
    preferences = str(settings.get('format_preference', '')).split(',') if convert else []
    staging = None

    def destination(index: int, relative: str) -> Path:
        nonlocal staging
        if staging is None:
            staging = tempfile.mkdtemp(prefix='.kapowarr-intake-' + identifier[:12] + '-', dir=root)
        path = Path(staging) / str(index) / _member(relative)
        contained(str(path), root)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    for index, source in enumerate(paths):
        contained(source, root)
        before = os.stat(source, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT:
            raise IntakeFailure(E.PREPARATION)
        try:
            with open(source, 'rb') as stream:
                magic = stream.read(8)
            kind = 'zip' if magic.startswith(b'PK') else 'rar' if magic.startswith(b'Rar!\x1a\x07') else 'pdf' if magic.startswith(b'%PDF-') else None
            if kind == 'pdf':
                if Path(source).suffix.lower() != '.pdf':
                    raise IntakeFailure(E.PREPARATION)
                outputs.append(source)
                continue
            if kind is None:
                raise IntakeFailure(E.PREPARATION)
            archive = None
            rar_stream = None
            try:
                if kind == 'zip':
                    _bounded_directory(source)
                    archive = ZipFile(source)
                    members = archive.infolist()
                    facts = [(m.filename, m.file_size, m.is_dir(),
                              stat.S_IFMT(m.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
                              or bool(m.flag_bits & 1), m) for m in members]
                else:
                    def admission(info):
                        budget['entries'] += 1
                        if budget['entries'] > MAX_MEMBERS:
                            raise IntakeFailure(E.PREPARATION)
                    rar_stream = open(source, 'rb')
                    archive = rarfile.RarFile(_BoundedHeaders(rar_stream), info_callback=admission, errors='strict')
                    if archive.needs_password() or len(archive.volumelist()) > 1:
                        raise IntakeFailure(E.PREPARATION)
                    facts = [(m.filename, m.file_size, m.is_dir(),
                              m.is_symlink() or bool(m.file_redir) or m.needs_password(), m) for m in archive.infolist()]
                budget['entries'] += len(facts) if kind == 'zip' else 0
                names = set()
                for name, size, directory, unsafe, member in facts:
                    normalized = _member(name)
                    if unsafe or normalized.casefold() in names or size < 0:
                        raise IntakeFailure(E.UNSAFE_PATH)
                    names.add(normalized.casefold())
                    budget['expanded'] += size
                if budget['entries'] > MAX_MEMBERS or budget['expanded'] > MAX_OUTPUT:
                    raise IntakeFailure(E.PREPARATION)
                files = [f for f in facts if not f[2]]
                if not files:
                    raise IntakeFailure(E.PREPARATION)
                packaged = [f for f in files if Path(f[0]).suffix.lower() in COMIC_EXTENSIONS]
                if len(packaged) + len(outputs) > 1000:
                    raise IntakeFailure(E.PREPARATION)

                def copy_member(fact, output):
                    if kind == 'zip':
                        with archive.open(fact[4]) as member_stream:
                            _copy(member_stream, output, fact[1])
                    else:
                        _rar_copy(source, fact[0], output, fact[1])

                if packaged:
                    if _depth:
                        raise IntakeFailure(E.PREPARATION)
                    # Packages are not comics merely because their outer name
                    # matches. Retain ancillary/unmatched data in the original.
                    extracted = []
                    for fact in packaged:
                        target = destination(index, fact[0])
                        with target.open('xb') as output:
                            copy_member(fact, output)
                        extracted.append(str(target))
                    outputs.extend(prepare_artifacts(tuple(extracted), root, settings, identifier,
                                                     _depth=1, _budget=budget))
                    continue
                if not any(Path(f[0]).suffix.lower() in FileConstants.IMAGE_EXTENSIONS for f in files):
                    # A named archive containing only ancillary text/XML is not
                    # evidence of a comic payload. Do not let its name grant it
                    # a library association.
                    raise IntakeFailure(E.UNSUPPORTED_ARTIFACT)
                current = Path(source).suffix.lower().lstrip('.')
                desired = next((p for p in preferences if p in ('zip', 'cbz', 'rar', 'cbr', 'folder')), None)
                actual = kind if current in ('zip', 'rar') else 'cbz' if kind == 'zip' else 'cbr'
                desired = desired or actual
                if desired == 'folder':
                    # Loose-image grouping is not an established ImportCandidate
                    # capability. Never invent one from a representative image.
                    raise IntakeFailure(E.UNSUPPORTED_ARTIFACT)
                if desired == current and actual == current:
                    outputs.append(source)
                    continue
                target = destination(index, Path(source).stem + '.' + desired)
                target_kind = 'zip' if desired in ('zip', 'cbz') else 'rar'
                if target_kind == kind:
                    with open(source, 'rb') as original, target.open('xb') as output:
                        _copy(original, output, before.st_size)
                elif target_kind == 'zip':
                    from backend.implementations.archive_normalization import \
                        normalize
                    normalize(source, str(target))
                else:
                    tree = destination(index, 'rar-input/.sentinel').parent
                    for fact in files:
                        output_path = tree / _member(fact[0])
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        with output_path.open('xb') as output:
                            copy_member(fact, output)
                    executable = archive_executable(write=True)
                    result = subprocess.run([executable, 'a', '-r', '-ep1', '-o-', '-inul', str(target), '.'],
                        cwd=str(tree), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=TOOL_TIMEOUT)
                    if result.returncode or not target.is_file():
                        raise IntakeFailure(E.PREPARATION)
                outputs.append(str(target))
            finally:
                if archive is not None:
                    archive.close()
                if rar_stream is not None:
                    rar_stream.close()
        except IntakeFailure:
            raise
        except Exception:
            raise IntakeFailure(E.PREPARATION) from None
        finally:
            after = os.stat(source, follow_symlinks=False)
            if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (
                    after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev):
                raise IntakeFailure(E.UNSTABLE)
    return tuple(sorted(outputs))

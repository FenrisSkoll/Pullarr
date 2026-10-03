"""Small no-clobber primitives and cross-process serial execution gate.

Unsupported kernels/filesystems fail closed. No copy/delete fallback, recursive
cleanup, alternate target calculation or directory enumeration is performed.
"""

import ctypes
import errno
import os
import stat
import sys
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Iterator

from backend.base.organization_job import ExecutionCode, OrganizationError


def comparison_ctime(value: os.stat_result) -> int:
    """Comparable path/fd timestamp; Windows fstat ctime may be ChangeTime.

    CPython #157671: Windows path stat retains creation time while fstat uses
    change time. Birth time preserves the historical path-stamp contract. Raw
    descriptor ctime must additionally be compared before/after a long read.
    """
    if sys.platform == 'win32':
        return getattr(value, 'st_birthtime_ns', value.st_ctime_ns)
    return value.st_ctime_ns


def safe_path(path: str) -> None:
    target = Path(path)
    if not target.is_absolute() or '..' in target.parts:
        raise OrganizationError(ExecutionCode.UNSAFE_PATH)
    for part in (*reversed(target.parents), target):
        try:
            value = os.lstat(part)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
            raise OrganizationError(ExecutionCode.UNSAFE_PATH)


def artifact(path: str) -> dict:
    safe_path(path)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_ino == 0:
        raise OrganizationError(ExecutionCode.UNSAFE_PATH)
    content = sha256()
    with open(path, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OrganizationError(ExecutionCode.SOURCE)
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            content.update(block)
    after = os.stat(path, follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise OrganizationError(ExecutionCode.SOURCE)
    return dict(device=after.st_dev, inode=after.st_ino, size=after.st_size,
                mtime_ns=after.st_mtime_ns, sha256=content.hexdigest())


def matches(path: str, expected: dict) -> bool:
    try:
        return artifact(path) == expected
    except FileNotFoundError:
        return False


def sync_directory(path: str) -> None:
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def rename_no_replace(source: str, target: str) -> None:
    safe_path(source)
    safe_path(target)
    if os.path.lexists(target):
        raise OrganizationError(ExecutionCode.OCCUPIED)
    if os.stat(source).st_dev != os.stat(os.path.dirname(target)).st_dev:
        raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Cross-filesystem transition')
    if os.name == 'nt':
        # Unlike POSIX rename, Windows rename fails if destination exists.
        os.rename(source, target)
    elif sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        operation = getattr(libc, 'renameat2', None)
        if operation is None:
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'No exclusive rename primitive')
        operation.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        operation.restype = ctypes.c_int
        if operation(-100, os.fsencode(source), -100, os.fsencode(target), 1):
            number = ctypes.get_errno()
            if number in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EXDEV):
                raise OrganizationError(ExecutionCode.UNSUPPORTED)
            raise OSError(number, os.strerror(number))
    else:
        raise OrganizationError(ExecutionCode.UNSUPPORTED, 'No verified exclusive rename primitive')
    sync_directory(os.path.dirname(target))
    if os.path.dirname(source) != os.path.dirname(target):
        sync_directory(os.path.dirname(source))


@contextmanager
def execution_gate(database: str) -> Iterator[None]:
    """OS-owned lock survives neither process exit nor a closed descriptor.

    All new organizer jobs sharing this DB serialize through this gate. Durable
    claims remain in SQLite for inspection; the OS lock proves a worker is live.
    This is not a lock on legacy executors or arbitrary external applications.
    """
    path = database + '.organizer.lock'
    safe_path(path)
    with open(path, 'a+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise OrganizationError(ExecutionCode.BUSY) from None
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

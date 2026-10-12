"""Apply configured permissions only to newly managed paths.

This deliberately does not walk an existing library.  The caller owns the
creation or a separately reviewed repair of the exact path.
"""

import errno
import os
import sqlite3
import stat
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.internals.settings import Settings


def _permission_settings(database: sqlite3.Connection | str | None = None,
                         settings=None):
    """Read the executing journal's policy without requiring a Flask context."""
    if settings is not None:
        return settings
    if database is None:
        return Settings().sv
    if isinstance(database, sqlite3.Connection):
        connection, owned = database, False
    else:
        connection = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
        owned = True
    try:
        try:
            values = dict(connection.execute('''SELECT key,value FROM config
                WHERE key IN ('chmod_folder','chown_group')'''))
        except sqlite3.OperationalError as error:
            if 'no such table: config' not in str(error):
                raise
            # Standalone journal stores without app configuration have no
            # configured permission policy. A normal Pullarr DB has config.
            values = {}
        return SimpleNamespace(chmod_folder=values.get('chmod_folder', ''),
                               chown_group=values.get('chown_group', ''))
    finally:
        if owned:
            connection.close()


def _configured_group(name: str) -> int:
    if name.isdecimal():
        return int(name)
    import grp
    return grp.getgrnam(name).gr_gid


def _open_nofollow(path: str, *, directory: bool) -> int:
    """Open each POSIX path component without following an ancestor link."""
    if os.name == 'nt':
        raise OrganizationError(ExecutionCode.UNSUPPORTED)
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | os.O_NOFOLLOW
    directory_flags = flags | getattr(os, 'O_DIRECTORY', 0)
    absolute = os.path.abspath(path)
    components = [component for component in absolute.split(os.sep) if component]
    descriptor = os.open(os.sep, directory_flags)
    try:
        for index, component in enumerate(components):
            next_fd = os.open(component, directory_flags if index < len(components) - 1
                              or directory else flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def mkdir_nofollow(path: str) -> None:
    """Create one child through a verified parent descriptor on POSIX."""
    if os.name == 'nt':
        os.mkdir(path)
        return
    parent_fd = _open_nofollow(os.path.dirname(path), directory=True)
    try:
        os.mkdir(os.path.basename(path), dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _effective_group_bits(path: str | int, mode: int) -> int:
    """Read the owning-group ACL entry and mask when Linux exposes an ACL."""
    fallback = (stat.S_IMODE(mode) >> 3) & 7
    if not sys.platform.startswith('linux') or not hasattr(os, 'getxattr'):
        return fallback
    try:
        raw = (os.getxattr(path, 'system.posix_acl_access') if isinstance(path, int)
               else os.getxattr(path, 'system.posix_acl_access', follow_symlinks=False))
    except OSError as error:
        if error.errno in (errno.ENODATA, getattr(errno, 'ENOATTR', errno.ENODATA),
                           errno.ENOTSUP, errno.EOPNOTSUPP):
            return fallback
        raise
    if len(raw) < 4 or (len(raw) - 4) % 8 or struct.unpack_from('<I', raw)[0] != 2:
        raise ValueError('Malformed POSIX access ACL')
    entries = {tag: permissions for tag, permissions, _ in
               struct.iter_unpack('<HHI', raw[4:])}
    if 4 not in entries:
        raise ValueError('POSIX ACL lacks owning group entry')
    return entries[4] & entries.get(16, 7)


def _grant_owning_group_acl(path: str | int, required: int) -> None:
    """Amend only the owning-group access entry; leave defaults and named ACLs."""
    if not sys.platform.startswith('linux') or not hasattr(os, 'getxattr'):
        return
    try:
        raw = bytearray(os.getxattr(path, 'system.posix_acl_access') if isinstance(path, int)
                        else os.getxattr(path, 'system.posix_acl_access', follow_symlinks=False))
    except OSError as error:
        if error.errno in (errno.ENODATA, getattr(errno, 'ENOATTR', errno.ENODATA),
                           errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise
    if len(raw) < 4 or (len(raw) - 4) % 8 or struct.unpack_from('<I', raw)[0] != 2:
        raise ValueError('Malformed POSIX access ACL')
    for offset in range(4, len(raw), 8):
        tag, permissions, _ = struct.unpack_from('<HHI', raw, offset)
        if tag == 4:
            if permissions & required != required:
                struct.pack_into('<H', raw, offset + 2, permissions | required)
                if isinstance(path, int):
                    os.setxattr(path, 'system.posix_acl_access', raw)
                else:
                    os.setxattr(path, 'system.posix_acl_access', raw, follow_symlinks=False)
            return
    raise ValueError('POSIX ACL lacks owning group entry')


def managed_directory_problem(path: str, *, database: sqlite3.Connection | str | None = None) -> str | None:
    """Read-only diagnostic for an exact existing managed destination."""
    if os.name == 'nt' or not sys.platform.startswith('linux'):
        return None
    settings = _permission_settings(database)
    if not settings.chmod_folder and not settings.chown_group:
        return None
    try:
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            return 'destination_not_directory'
        if settings.chown_group and info.st_gid != _configured_group(settings.chown_group):
            return 'destination_group_mismatch'
        if settings.chmod_folder:
            required = (int(settings.chmod_folder, 8) >> 3) & 7
            if (_effective_group_bits(path, info.st_mode) & required) != required:
                return 'destination_group_access_denied'
    except (OSError, ValueError, KeyError):
        return 'destination_permission_unavailable'
    return None


def managed_file_problem(path: str, *, database: sqlite3.Connection | str | None = None) -> str | None:
    """Read-only effective group policy check for an imported regular file."""
    if os.name == 'nt' or not sys.platform.startswith('linux'):
        return None
    settings = _permission_settings(database)
    if not settings.chmod_folder and not settings.chown_group:
        return None
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return 'destination_not_regular_file'
        if settings.chown_group and info.st_gid != _configured_group(settings.chown_group):
            return 'destination_group_mismatch'
        if settings.chmod_folder:
            required = (int(settings.chmod_folder, 8) >> 3) & 6
            if (_effective_group_bits(path, info.st_mode) & required) != required:
                return 'destination_group_access_denied'
            if info.st_mode & stat.S_IXUSR or info.st_mode & stat.S_IXGRP or info.st_mode & stat.S_IXOTH:
                return 'destination_file_executable'
    except (OSError, ValueError, KeyError):
        return 'destination_permission_unavailable'
    return None


def worker_permission_context() -> dict:
    """Identity of this application worker, not a separate docker exec shell."""
    if os.name == 'nt':
        return dict(platform='windows')
    value = dict(platform=sys.platform, uid=os.geteuid(), gid=os.getegid(),
                 groups=os.getgroups())
    if sys.platform.startswith('linux'):
        try:
            with open('/proc/self/status', encoding='ascii') as stream:
                for line in stream:
                    if line.startswith('Umask:'):
                        value['umask'] = line.split(':', 1)[1].strip()
                        break
        except OSError:
            pass
    return value


def create_managed_folder(path: str, *, settings=None) -> None:
    """Create only missing components and apply policy to those exact inodes."""
    missing = []
    current = os.path.abspath(path)
    while not os.path.lexists(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            raise OrganizationError(ExecutionCode.UNSAFE_PATH)
        current = parent
    if not stat.S_ISDIR(os.lstat(current).st_mode):
        raise OrganizationError(ExecutionCode.UNSAFE_PATH)
    created = []
    try:
        for part in reversed(missing):
            try:
                mkdir_nofollow(part)
            except FileExistsError:
                if not stat.S_ISDIR(os.lstat(part).st_mode):
                    raise OrganizationError(ExecutionCode.UNSAFE_PATH) from None
                continue
            info = os.lstat(part)
            created.append((part, info.st_dev, info.st_ino))
            apply_managed_permissions(part, directory=True, settings=settings)
    except BaseException:
        for part, device, inode in reversed(created):
            try:
                current_info = os.lstat(part)
                if (current_info.st_dev, current_info.st_ino) == (device, inode):
                    os.rmdir(part)
            except OSError:
                pass  # A real entry appeared; leave it for guarded recovery.
        raise


def apply_managed_permissions(path: str, *, directory: bool, repair: bool = False,
                              database: sqlite3.Connection | str | None = None,
                              settings=None) -> None:
    """Set and verify mode/group without following a replaced symlink.

    ``repair`` is reserved for a specifically reviewed existing destination;
    ordinary callers invoke this immediately after creating their own path.
    POSIX ACL group masks are reflected in stat's group mode bits.  A narrower
    named ACL entry can still restrict a particular SMB identity; that needs an
    access test from the actual SMB account.
    """
    if os.name == 'nt' or not (sys.platform.startswith('linux') or sys.platform == 'darwin'):
        return
    try:
        values = _permission_settings(database, settings)
        mode_text, group_name = values.chmod_folder, values.chown_group
        if not mode_text and not group_name:
            return
        if not hasattr(os, 'O_NOFOLLOW'):
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'No symlink-safe permission operation')
        descriptor = _open_nofollow(path, directory=directory)
        try:
            before = os.fstat(descriptor)
            if not (stat.S_ISDIR(before.st_mode) if directory else stat.S_ISREG(before.st_mode)):
                raise OrganizationError(ExecutionCode.UNSAFE_PATH)
            group = _configured_group(group_name) if group_name else before.st_gid
            if group != before.st_gid:
                os.fchown(descriptor, -1, group)
            if mode_text:
                configured = int(mode_text, 8)
                if directory:
                    parent = os.stat(os.path.dirname(path), follow_symlinks=False)
                    mode = configured | ((before.st_mode | parent.st_mode) & stat.S_ISGID)
                else:
                    mode = configured & ~0o111 & 0o777
                os.fchmod(descriptor, mode)
                _grant_owning_group_acl(descriptor, (mode >> 3) & 7)
            after = os.fstat(descriptor)
            path_after = os.lstat(path)
            same_kind = stat.S_ISDIR(after.st_mode) if directory else stat.S_ISREG(after.st_mode)
            if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or (after.st_dev, after.st_ino) != (path_after.st_dev, path_after.st_ino)
                    or after.st_gid != group or not same_kind):
                raise OrganizationError(ExecutionCode.CONFLICT, 'Managed path changed during permission application')
            if mode_text and (stat.S_IMODE(after.st_mode) & 0o777) != (mode & 0o777):
                raise OrganizationError(ExecutionCode.PERMISSION, 'Configured mode was not applied')
            if mode_text and (_effective_group_bits(descriptor, after.st_mode) & ((mode >> 3) & 7)) != ((mode >> 3) & 7):
                raise OrganizationError(ExecutionCode.PERMISSION, 'Owning group ACL denies configured access')
        finally:
            os.close(descriptor)
    except OrganizationError:
        raise
    except (OSError, ValueError, KeyError, NotImplementedError,
            sqlite3.Error, RuntimeError) as error:
        raise OrganizationError(ExecutionCode.PERMISSION, type(error).__name__) from None

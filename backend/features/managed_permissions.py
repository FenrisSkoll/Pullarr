"""Apply configured permissions only to newly managed paths.

This deliberately does not walk an existing library.  The caller owns the
creation or a separately reviewed repair of the exact path.
"""

import errno
import os
import stat
import struct
import sys

from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.internals.settings import Settings


def _configured_group(name: str) -> int:
    if name.isdecimal():
        return int(name)
    import grp
    return grp.getgrnam(name).gr_gid


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


def managed_directory_problem(path: str) -> str | None:
    """Read-only diagnostic for an exact existing managed destination."""
    if os.name == 'nt' or not sys.platform.startswith('linux'):
        return None
    settings = Settings().sv
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


def apply_managed_permissions(path: str, *, directory: bool, repair: bool = False) -> None:
    """Set and verify mode/group without following a replaced symlink.

    ``repair`` is reserved for a specifically reviewed existing destination;
    ordinary callers invoke this immediately after creating their own path.
    POSIX ACL group masks are reflected in stat's group mode bits.  A narrower
    named ACL entry can still restrict a particular SMB identity; that needs an
    access test from the actual SMB account.
    """
    if os.name == 'nt' or not (sys.platform.startswith('linux') or sys.platform == 'darwin'):
        return
    settings = Settings().sv
    mode_text, group_name = settings.chmod_folder, settings.chown_group
    if not mode_text and not group_name:
        return
    try:
        if not hasattr(os, 'O_NOFOLLOW'):
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'No symlink-safe permission operation')
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | os.O_NOFOLLOW
        if directory:
            flags |= getattr(os, 'O_DIRECTORY', 0)
        descriptor = os.open(path, flags)
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
    except (OSError, ValueError, KeyError, NotImplementedError) as error:
        raise OrganizationError(ExecutionCode.PERMISSION, type(error).__name__) from None

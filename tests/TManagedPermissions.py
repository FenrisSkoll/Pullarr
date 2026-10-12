"""Scoped permission policy, including owning-group ACL entry and mask."""

import os
import stat
import struct
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.features import managed_permissions as policy


def acl(group=5, mask=7):
    return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, bits, 0xffffffff)
        for tag, bits in ((1, 7), (4, group), (16, mask), (32, 5)))


class ManagedPermissionTests(TestCase):
    def test_new_setgid_directory_grants_group_access_without_stripping_acl(self):
        state = SimpleNamespace(mode=stat.S_IFDIR | 0o2755, gid=100,
                                access=acl(), default=acl(7, 7))
        def info():
            return SimpleNamespace(st_mode=state.mode, st_gid=state.gid,
                                   st_dev=1, st_ino=17)
        opened = []
        fake = SimpleNamespace(name='posix', path=SimpleNamespace(dirname=lambda _: '/root',
            isdir=lambda _: True), O_RDONLY=0, O_CLOEXEC=64, O_NOFOLLOW=128, O_DIRECTORY=256,
            open=lambda path, flags: opened.append(flags) or 9,
            close=lambda descriptor: None, fstat=lambda descriptor: info(),
            lstat=lambda _: info(), stat=lambda *a, **k: info(),
            fchown=lambda *a: None,
            fchmod=lambda descriptor, mode: setattr(state, 'mode', stat.S_IFDIR | mode),
            getxattr=lambda path, key, **kw: state.access if key.endswith('access') else state.default,
            setxattr=lambda path, key, value, **kw: setattr(state, 'access', bytes(value)))
        with patch.object(policy, 'os', fake), patch.object(policy, 'sys', SimpleNamespace(platform='linux')), \
                patch.object(policy, 'Settings', return_value=SimpleNamespace(
                    sv=SimpleNamespace(chmod_folder='775', chown_group='100'))):
            policy.apply_managed_permissions('/root/folder', directory=True)
            self.assertEqual(stat.S_IMODE(state.mode), 0o2775)
            self.assertEqual(policy._effective_group_bits('/root/folder', state.mode), 7)
            self.assertEqual(state.default, acl(7, 7))
            self.assertEqual(opened, [64 | 128 | 256])

    def test_acl_update_denied_is_a_permission_failure(self):
        state = SimpleNamespace(mode=stat.S_IFDIR | 0o2755, gid=100)
        info = lambda: SimpleNamespace(st_mode=state.mode, st_gid=state.gid,
                                       st_dev=1, st_ino=17)
        fake = SimpleNamespace(name='posix', path=SimpleNamespace(dirname=lambda _: '/root',
            isdir=lambda _: True), O_RDONLY=0, O_CLOEXEC=64, O_NOFOLLOW=128, O_DIRECTORY=256,
            open=lambda path, flags: 9, close=lambda descriptor: None,
            fstat=lambda descriptor: info(), lstat=lambda _: info(),
            stat=lambda *a, **k: info(), fchown=lambda *a: None,
            fchmod=lambda descriptor, mode: setattr(state, 'mode', stat.S_IFDIR | mode),
            getxattr=lambda *a, **kw: acl(),
            setxattr=lambda *a, **kw: (_ for _ in ()).throw(PermissionError('denied')))
        with patch.object(policy, 'os', fake), patch.object(policy, 'sys', SimpleNamespace(platform='linux')), \
                patch.object(policy, 'Settings', return_value=SimpleNamespace(
                    sv=SimpleNamespace(chmod_folder='775', chown_group='100'))):
            with self.assertRaises(OrganizationError) as raised:
                policy.apply_managed_permissions('/root/folder', directory=True)
        self.assertEqual(raised.exception.code, ExecutionCode.PERMISSION)

    def test_windows_uses_native_access_control(self):
        with patch.object(policy, 'Settings', side_effect=AssertionError('POSIX settings used')):
            policy.apply_managed_permissions('C:\\Comics\\Volume', directory=True)

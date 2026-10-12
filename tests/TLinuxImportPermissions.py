"""Real Linux filesystem acceptance. Skips explicitly on Windows/no ACL tools."""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, skipUnless
from zipfile import ZipFile

from fixtures.library_import import ImportHarness
from Tbackend.features import local_organization as old_fixture

from backend.base.organization_job import OrganizationError
from backend.features.import_draft import (apply_draft, resume_draft,
                                           stage_import)
from backend.features.managed_permissions import (create_managed_folder,
                                                  managed_directory_problem,
                                                  managed_file_problem)

LINUX_ACL = sys.platform.startswith('linux') and shutil.which('setfacl') and shutil.which('getfacl')


@skipUnless(LINUX_ACL, 'real Linux filesystem with setfacl/getfacl required')
class LinuxImportPermissionTests(ImportHarness, TestCase):
    file_database = old_fixture.RegistrationBoundaryTests.file_database

    def setUp(self):
        super().setUp()
        import grp
        self.database = self.file_database()
        self.response.json.side_effect = list(self.response.json.side_effect) * 2
        self.settings.chmod_folder = '775'
        try:
            users = grp.getgrnam('users')
            use_users = os.geteuid() == 0 or users.gr_gid in os.getgroups() or users.gr_gid == os.getegid()
            self.group_id = users.gr_gid if use_users else os.getegid()
            group_name = 'users' if use_users else str(self.group_id)
        except KeyError:
            self.group_id = os.getegid()
            group_name = str(self.group_id)
        self.settings.chown_group = group_name
        permissions = self.start_patch('backend.features.managed_permissions.Settings').return_value
        permissions.sv = self.settings
        self.db.execute("UPDATE config SET value=? WHERE key='volume_folder_naming'",
                        (self.settings.volume_folder_naming,))
        self.db.commit()
        os.chown(self.root, -1, self.group_id)
        os.chmod(self.root, 0o2775)
        subprocess.run(['setfacl','-m','d:u::rwx,d:g::rwx,d:o::r-x,d:m::rwx',str(self.root)],
                       check=True, capture_output=True)

    def acl(self, path):
        return subprocess.run(['getfacl','-cp',str(path)], check=True,
                              capture_output=True,text=True).stdout

    def test_real_import_under_setgid_default_acl_with_restrictive_umask(self):
        for mask in (0o022, 0o027):
            with self.subTest(umask=oct(mask)):
                folder = self.root / ('case-' + oct(mask)[2:])
                old = os.umask(mask)
                try:
                    create_managed_folder(str(folder))
                finally:
                    os.umask(old)
                mode = stat.S_IMODE(os.lstat(folder).st_mode)
                self.assertEqual(mode & 0o2775, 0o2775)
                self.assertIsNone(managed_directory_problem(str(folder)))
                self.assertIn('group::rwx', self.acl(folder))
                self.assertIn('mask::rwx', self.acl(folder))

        incoming = self.root / 'Incoming'
        incoming.mkdir()
        source = incoming / 'Example Hero v2 #1 (2021).cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'disposable Linux ACL page')
        payload = source.read_bytes()
        old = os.umask(0o027)
        try:
            stage = stage_import(self.database, [dict(filepath=str(source),
                provider='comicvine', provider_id='2127', issue_ids=['301'])], False)
            result = apply_draft(self.database, stage['id'], stage['revision'])
        finally:
            os.umask(old)
        self.assertEqual(result['jobs'][0]['state'], 'completed', result)
        target = Path(result['jobs'][0]['target'])
        self.assertEqual(target.read_bytes(), payload)
        folder = target.parent
        self.assertEqual(stat.S_IMODE(os.lstat(folder).st_mode) & 0o2775, 0o2775)
        self.assertIsNone(managed_directory_problem(str(folder)))
        self.assertIn('group::rwx', self.acl(folder))
        self.assertIn('mask::rwx', self.acl(folder))
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode) & 0o111, 0)
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode) & 0o660, 0o660)
        self.assertIsNone(managed_file_problem(str(target)))
        self.assertEqual(os.lstat(target).st_gid, self.group_id)
        create_managed_folder(str(folder))
        self.assertIsNone(managed_directory_problem(str(folder)))

    def test_named_acl_survives_and_existing_directory_is_not_reowned(self):
        candidate = self.root / 'New'
        named_uid = os.geteuid() + 10000
        subprocess.run(['setfacl','-m',f'd:u:{named_uid}:r-x,d:m::r-x',str(self.root)],
                       check=True,capture_output=True)
        create_managed_folder(str(candidate))
        access = self.acl(candidate)
        self.assertIn(f'user:{named_uid}:r-x',access)
        self.assertIn('group::rwx',access)
        self.assertIn('mask::rwx',access)
        self.assertIsNone(managed_directory_problem(str(candidate)))
        existing = self.root / 'User folder'; existing.mkdir()
        os.chmod(existing,0o2755)
        before = os.lstat(existing)
        create_managed_folder(str(existing))
        after = os.lstat(existing)
        self.assertEqual((before.st_uid,before.st_gid,stat.S_IMODE(before.st_mode)),
                         (after.st_uid,after.st_gid,stat.S_IMODE(after.st_mode)))

    def test_pending_import_journal_replays_with_group_write_intact(self):
        from unittest.mock import patch
        source = self.root / 'Incoming' / 'Example Hero v2 #1 (2021).cbz'
        source.parent.mkdir()
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'disposable replay page')
        stage = stage_import(self.database, [dict(filepath=str(source),
            provider='comicvine', provider_id='2127', issue_ids=['301'])], False)
        with patch('backend.features.organization_execution.OrganizationExecutor.apply_job',
                   side_effect=RuntimeError('synthetic worker restart')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic worker restart'):
                apply_draft(self.database, stage['id'], stage['revision'])
        from backend.features.import_draft import _applying
        _applying.clear()
        result = resume_draft(self.database, stage['id'], stage['revision'] + 1)
        self.assertEqual(result['jobs'][0]['state'], 'completed', result)
        target = Path(result['jobs'][0]['target'])
        self.assertIsNone(managed_directory_problem(str(target.parent)))
        self.assertIsNone(managed_file_problem(str(target)))
        self.assertTrue(target.is_file())

    def test_symlink_destination_is_rejected_without_changing_outside(self):
        outside = self.root.parent / (self.root.name + '-outside')
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        link = self.root / 'Escape'; link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OrganizationError):
            create_managed_folder(str(link / 'New'))
        self.assertFalse((outside / 'New').exists())

    def test_group_mismatch_fails_for_unprivileged_worker(self):
        if os.geteuid() == 0:
            self.skipTest('root can chgrp; run under the actual non-root worker too')
        original = self.settings.chown_group
        self.settings.chown_group = str(os.getegid() + 10000)
        try:
            with self.assertRaises(OrganizationError):
                create_managed_folder(str(self.root / 'Denied'))
            self.assertFalse((self.root / 'Denied').exists())
        finally:
            self.settings.chown_group = original

    def test_authorized_nonroot_group_member_can_create_and_delete(self):
        folder = self.root / 'Shared'; create_managed_folder(str(folder))
        marker = folder / 'member-write-test'
        if os.geteuid() != 0:
            marker.write_bytes(b'test')
            marker.unlink()
            return
        import pwd
        account = pwd.getpwnam('nobody')
        if account.pw_uid == 0:
            self.skipTest('no non-root test identity')
        script = "from pathlib import Path; p=Path(__import__('os').environ['TEST_PATH']); p.write_bytes(b'test'); p.unlink()"
        def demote():
            os.setgroups([self.group_id])
            os.setgid(self.group_id)
            os.setuid(account.pw_uid)
        result = subprocess.run([sys.executable,'-c',script],
                                env={**os.environ,'TEST_PATH':str(marker)},
                                preexec_fn=demote,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

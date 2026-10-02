"""Shadow synchronization through real add/refresh SQL, no external network."""

import sqlite3
from unittest import TestCase

from fixtures.comicvine_fetch import issue_response
from fixtures.comicvine_search import volume_response
from fixtures.metadata_refresh import RefreshHarness

from backend.base.definitions import RootFolder
from backend.features.library_import import import_library
from backend.implementations.volumes import Issue, Volume
from backend.internals.provider_identity import (ExternalIdentity,
                                                 ProviderIdentityDB)


class IdentityShadows(RefreshHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('backend.internals.provider_identity.get_db',
                         side_effect=self.db.cursor)

    def test_add_and_selected_source_and_public_shape(self):
        self.assertEqual(ProviderIdentityDB.audit(), [])
        self.assertEqual(
            ProviderIdentityDB.selected_provider(
                self.volume_id), 'comicvine')
        self.assertEqual(ProviderIdentityDB.volume_identities(self.volume_id), [
            ExternalIdentity(self.volume_id, 'comicvine', '2127', 'legacy', 1234567890)])
        self.assertEqual(ProviderIdentityDB.issue_identities(1, 'comicvine'), [
            ExternalIdentity(1, 'comicvine', '301', 'legacy')])
        self.assertNotIn(
            'metadata_provider', Volume(
                self.volume_id).get_public_data())

    def test_refresh_new_issues_and_deleted_shadows(self):
        self.prepare_refresh([volume_response(count_of_issues=2)], [
            issue_response(), issue_response(id=303, issue_number='3')])
        self.refresh()
        self.assertEqual(ProviderIdentityDB.audit(), [])
        self.assertEqual(self.db.execute(
            '''SELECT provider_id FROM issue_external_ids
            ORDER BY provider_id''').fetchall(), [('301',), ('303',)])
        self.assertEqual(ProviderIdentityDB.issue_identities(2), [])

    def test_partial_issue_insert_failure_rolls_back_guarded_issue_stage(
            self):
        self.prepare_refresh([volume_response(count_of_issues=4)], [
            issue_response(id=303), issue_response(id=304)])
        self.db.execute(
            '''CREATE TEMP TRIGGER reject_four BEFORE INSERT ON issues
            WHEN NEW.comicvine_id=304 BEGIN SELECT RAISE(ABORT,'test failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.refresh()
        self.assertFalse(self.db.in_transaction)
        # Generation-aware stage owns rollback; earlier volume commit remains.
        self.assertEqual(ProviderIdentityDB.audit(), [])
        self.assertEqual(self.db.execute(
            '''SELECT provider_id FROM issue_external_ids
            WHERE provider_id IN ('303','304')''').fetchall(), [])

    def test_cross_references_are_opaque_and_do_not_change_dispatch(self):
        identity = ExternalIdentity(
            self.volume_id, 'metron', 'volume:ABC', 'user', 42)
        ProviderIdentityDB.put_volume_identity(identity)
        ProviderIdentityDB.put_issue_identity(
            ExternalIdentity(1, 'gcd', '0007/A', 'user'))
        self.db.commit()
        self.refresh()
        self.assertEqual(
            ProviderIdentityDB.volume_identities(
                self.volume_id, 'metron'), [identity])
        self.assertEqual(
            ProviderIdentityDB.issue_identities(
                1, 'gcd')[0].provider_id, '0007/A')
        self.assertTrue(
            self.session.get.await_args_list[0].args[0].endswith('/volumes/'))
        self.assertEqual(
            ProviderIdentityDB.selected_provider(
                self.volume_id), 'comicvine')
        self.assertEqual(ProviderIdentityDB.audit(), [])

    def test_assertion_update_and_conflict_are_explicit(self):
        ProviderIdentityDB.put_volume_identity(
            ExternalIdentity(1, 'gcd', 'a', 'user'))
        ProviderIdentityDB.put_volume_identity(
            ExternalIdentity(1, 'gcd', 'a', 'verified', 8))
        self.assertEqual(
            ProviderIdentityDB.volume_identities(
                1, 'gcd')[0].provenance, 'verified')
        with self.assertRaises(ValueError):
            ProviderIdentityDB.put_volume_identity(
                ExternalIdentity(1, 'gcd', 'b', 'user'))
        self.assertEqual(
            ProviderIdentityDB.volume_identities(
                1, 'gcd')[0].provider_id, 'a')

    def test_storage_validation_and_selection_gate(self):
        for identity in (
            ExternalIdentity(1, 'comicvine', '999', 'user'),
            ExternalIdentity(1, 'Metron', 'a', 'user'),
            ExternalIdentity(1, 'metron', '', 'user'),
            ExternalIdentity(1, 'metron', 123, 'user'),
            ExternalIdentity(1, 'metron', 'a', ''),
        ):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                ProviderIdentityDB.put_volume_identity(identity)
        with self.assertRaises(ValueError):
            ProviderIdentityDB.set_selected_provider(1, 'metron')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE volumes SET metadata_provider='Invalid Key'")
        ProviderIdentityDB.set_selected_provider(1, 'comicvine')
        with self.assertRaises(KeyError):
            ProviderIdentityDB.selected_provider(999)

    def test_issue_uniqueness_is_namespaced(self):
        ProviderIdentityDB.put_issue_identity(
            ExternalIdentity(1, 'metron', 'x', 'user'))
        ProviderIdentityDB.put_issue_identity(
            ExternalIdentity(2, 'gcd', 'x', 'user'))
        with self.assertRaises(sqlite3.IntegrityError):
            ProviderIdentityDB.put_issue_identity(
                ExternalIdentity(2, 'metron', 'x', 'user'))
        with self.assertRaises(sqlite3.IntegrityError):
            ProviderIdentityDB.put_issue_identity(
                ExternalIdentity(999, 'metron', 'y', 'user'))

    def test_legacy_identity_updates_do_not_overwrite_other_namespaces(self):
        ProviderIdentityDB.put_issue_identity(
            ExternalIdentity(1, 'metron', 'x', 'user'))
        Issue(1).update({'comicvine_id': 987})
        Volume(1).update({'comicvine_id': 654})
        self.assertEqual(
            ProviderIdentityDB.issue_identities(
                1, 'comicvine')[0].provider_id, '987')
        self.assertEqual(
            ProviderIdentityDB.volume_identities(
                1, 'comicvine')[0].provider_id, '654')
        self.assertEqual(
            ProviderIdentityDB.issue_identities(
                1, 'metron')[0].provider_id, 'x')
        self.assertEqual(ProviderIdentityDB.audit(), [])

    def test_corrupt_shadow_fails_loudly_without_repair(self):
        self.db.execute("UPDATE volume_external_ids SET provider_id='wrong'")
        before = self.rows('volumes')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'shadow conflict'):
            self.refresh()
        self.assertEqual(self.rows('volumes'), before)
        self.assertEqual(len(ProviderIdentityDB.audit()), 1)
        self.db.execute(
            "UPDATE issue_external_ids SET provider_id='wrong' WHERE issue_id=1")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'shadow conflict'):
            Issue(1).update({'comicvine_id': 555})
        self.assertEqual(Issue(1).get_data().comicvine_id, 301)

    def test_missing_shadow_diagnostic_and_update_failure(self):
        self.db.execute('DELETE FROM volume_external_ids')
        self.db.execute('DELETE FROM issue_external_ids WHERE issue_id=1')
        self.assertEqual(len(ProviderIdentityDB.audit()), 2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'shadow conflict'):
            Volume(1).update({'last_cv_fetch': 12})

    def test_domain_deletion_cascades_all_namespaces_but_keeps_other_volume(
            self):
        second = self.add_second_volume()
        ProviderIdentityDB.put_volume_identity(
            ExternalIdentity(1, 'metron', 'v', 'user'))
        ProviderIdentityDB.put_issue_identity(
            ExternalIdentity(1, 'metron', 'i', 'user'))
        self.db.execute('DELETE FROM volumes WHERE id=1')
        self.assertEqual(ProviderIdentityDB.volume_identities(1), [])
        self.assertEqual(ProviderIdentityDB.issue_identities(1), [])
        self.assertEqual(len(ProviderIdentityDB.volume_identities(second)), 1)
        self.assertEqual(ProviderIdentityDB.audit(), [])
        self.assertEqual(self.db.execute(
            'PRAGMA foreign_key_check').fetchall(), [])

    def test_existing_file_links_and_monitor_flags_survive(self):
        self.link_file((1, 2))
        before = self.db.execute(
            'SELECT * FROM issues_files ORDER BY issue_id').fetchall()
        self.db.execute('UPDATE issues SET monitored=0 WHERE id=1')
        self.db.commit()
        self.refresh()
        self.assertEqual(self.db.execute(
            'SELECT * FROM issues_files ORDER BY issue_id').fetchall(), before)
        self.assertFalse(Issue(1).get_data().monitored)
        self.assertEqual(ProviderIdentityDB.audit(), [])

    def test_library_import_naturally_shadow_writes(self):
        self.prepare_fetch(
            volume_response(
                id=9001), [
                issue_response(
                    id=901, volume={
                        'id': 9001})])
        roots = self.start_patch(
            'backend.features.library_import.RootFolders').return_value
        roots.get_all.return_value = [RootFolder(1, str(self.root), None)]
        self.start_patch(
            'backend.features.library_import.commit',
            side_effect=self.db.commit)
        self.start_patch('backend.features.library_import.scan_files')
        import_library(
            [{'id': 9001, 'filepath': str(self.root / 'import' / 'issue.cbz')}])
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM volume_external_ids').fetchone()[0], 2)
        self.assertEqual(ProviderIdentityDB.audit(), [])

    def test_orphan_diagnostic_with_foreign_keys_deliberately_disabled(self):
        self.db.commit()
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute(
            "INSERT INTO issue_external_ids VALUES (999,'metron','orphan','test')")
        self.db.execute(
            "INSERT INTO volume_external_ids VALUES (999,'gcd','orphan','test',NULL)")
        self.db.commit()
        self.db.execute('PRAGMA foreign_keys=ON')
        self.assertEqual(ProviderIdentityDB.audit(), [
            ('issue', 999, 'orphan external identity'),
            ('volume', 999, 'orphan external identity')])

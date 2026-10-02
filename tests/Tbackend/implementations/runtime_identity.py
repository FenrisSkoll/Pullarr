"""Pre-cutover characterization of identity ownership and legacy compatibility."""

import sqlite3
from unittest import TestCase

from fixtures.comicvine_fetch import issue_response
from fixtures.comicvine_search import volume_response
from fixtures.metadata_refresh import NOW, RefreshHarness

from backend.implementations.naming import get_issue_naming_keys
from backend.implementations.volumes import Issue, Volume, refresh_and_scan
from backend.internals.provider_identity import (ExternalIdentity,
                                                 ProviderIdentityDB)


class RuntimeIdentityCharacterization(RefreshHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('backend.internals.provider_identity.get_db',
                         side_effect=self.db.cursor)

    def attach_references(self, issue_id):
        for provider in ('metron', 'gcd'):
            ProviderIdentityDB.put_issue_identity(
                ExternalIdentity(issue_id, provider, 'ref:' + str(issue_id), 'test'))
        self.db.commit()

    def test_retained_issue_keeps_both_cross_provider_references(self):
        self.attach_references(1)
        references = ProviderIdentityDB.issue_identities(1)
        self.refresh()
        self.assertEqual(ProviderIdentityDB.issue_identities(1), references)
        self.assertEqual(Issue(1).get_data().comicvine_id, 301)

    def test_deleted_issue_cascades_all_owned_identities(self):
        self.attach_references(2)
        self.prepare_refresh([volume_response(count_of_issues=1)], [issue_response()])
        self.refresh()
        self.assertEqual(self.db.execute('SELECT id FROM issues WHERE id=2').fetchall(), [])
        self.assertEqual(ProviderIdentityDB.issue_identities(2), [])
        self.assertEqual(ProviderIdentityDB.audit(), [])

    def test_add_refresh_new_issue_preserves_keys_and_public_ids(self):
        before = Issue(1).get_data()
        self.prepare_refresh([volume_response(count_of_issues=3)], [
            issue_response(name='Updated'), issue_response(id=302), issue_response(id=303)])
        self.refresh()
        self.assertEqual(Issue(1).get_data().id, before.id)
        self.assertEqual(Issue(1).get_data().comicvine_id, 301)
        self.assertEqual(ProviderIdentityDB.issue_identities(3)[0].provider_id, '303')
        public = Volume(1).get_public_data()
        self.assertEqual(public['comicvine_id'], 2127)
        self.assertNotIn('metadata_provider', public)
        self.assertEqual(ProviderIdentityDB.audit(), [])

    def test_legacy_naming_ids_remain_integers(self):
        keys = get_issue_naming_keys(Volume(1).get_data(), Issue(1).get_data())
        self.assertEqual(keys.comicvine_id, 2127)
        self.assertEqual(keys.issue_comicvine_id, 301)

    def test_scheduled_recent_volume_is_not_fetched(self):
        self.set_timestamp(1, NOW.timestamp())
        refresh_and_scan(allow_skipping=True)
        self.session.get.assert_not_awaited()

    def test_legal_duplicate_volume_identity_collapses_request_as_before(self):
        self.db.execute('''INSERT INTO volumes(id,comicvine_id,title,root_folder,last_cv_fetch)
            VALUES (9,2127,'Duplicate',1,1234567891)''')
        self.db.execute('INSERT INTO volumes_covers VALUES (9,NULL)')
        self.db.commit()
        refresh_and_scan(allow_skipping=False)
        self.assertEqual(self.session.get.await_args_list[0].kwargs['params']['filter'], 'id:2127')
        self.assertEqual(Volume(1).get_data().last_cv_fetch, 1234567890)
        self.assertEqual(Volume(9).get_data().last_cv_fetch, NOW.timestamp())
        self.assertEqual(Issue(1).get_data().volume_id, 1)

    def test_selection_gate_and_issue_identity_uniqueness(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE volumes SET metadata_provider='Invalid Key'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE issue_external_ids SET provider_id='301' WHERE issue_id=2")

    def test_empty_volume_identity_set_is_detected_by_diagnostics(self):
        self.db.execute('DELETE FROM volume_external_ids')
        self.assertEqual(len(ProviderIdentityDB.audit()), 1)

    def test_missing_issue_identity_is_detected_by_diagnostics(self):
        self.db.execute('DELETE FROM issue_external_ids WHERE issue_id=1')
        self.assertEqual(len(ProviderIdentityDB.audit()), 1)

    def test_mismatched_both_legacy_shadows_detected_without_repair(self):
        self.db.execute("UPDATE volume_external_ids SET provider_id='999'")
        self.db.execute("UPDATE issue_external_ids SET provider_id='999' WHERE issue_id=1")
        self.assertEqual(len(ProviderIdentityDB.audit()), 2)
        self.assertEqual(Volume(1).get_data().comicvine_id, 2127)
        self.assertEqual(Issue(1).get_data().comicvine_id, 301)

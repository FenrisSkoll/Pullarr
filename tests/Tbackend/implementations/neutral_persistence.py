"""Real SQL lifecycle without ComicVine IDs, alongside unchanged CV fixtures."""

from unittest import TestCase
from unittest.mock import patch

from fixtures.metadata_refresh import NOW, RefreshHarness
from fixtures.neutral_provider import NeutralProvider

from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.naming import _fill_format, get_issue_naming_keys
from backend.implementations.volumes import (Issue, Library, Volume,
                                             refresh_and_scan)
from backend.internals.provider_identity import (ExternalIdentity,
                                                 MetadataIdentityError,
                                                 ProviderIdentityDB)


class NeutralPersistence(RefreshHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.provider = NeutralProvider()
        p = patch.dict(PROVIDERS, {'test_provider': lambda: self.provider})
        p.start()
        self.addCleanup(p.stop)

    def add_neutral(self):
        return Library.add_metadata(ProviderVolumeIdentity('test_provider', 'V:alpha'), 1, True)

    def test_full_lifecycle_retained_deleted_references_and_local_keys(self):
        local = self.add_neutral()
        volume = Volume(local)
        self.assertIsNone(volume.get_data().comicvine_id)
        issues = volume.get_issues()
        first, second = [i.id for i in issues]
        for identity in (first, second):
            for provider in ('metron', 'gcd'):
                ProviderIdentityDB.put_issue_identity(ExternalIdentity(
                    identity, provider, str(identity), 'verified'))
        self.db.commit()
        references = ProviderIdentityDB.issue_identities(first)
        self.provider.title = 'Changed'
        self.provider.issues = [self.provider.issue(
            'I:one', '1', 'Still TPB'), self.provider.issue('I:new', '3')]
        refresh_and_scan(local)
        self.assertEqual(volume.get_data().title, 'Changed')
        self.assertEqual(Issue(first).get_data().title, 'Still TPB')
        self.assertEqual(
            ProviderIdentityDB.issue_identities(first), references)
        self.assertEqual(ProviderIdentityDB.issue_identities(second), [])
        self.assertEqual(self.db.execute(
            'SELECT id FROM issues WHERE id=?', (second,)).fetchall(), [])
        self.assertTrue(
            all(i.comicvine_id is None for i in volume.get_issues()))
        self.assertEqual(ProviderIdentityDB.volume_identities(
            local, 'comicvine'), [])
        self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])
        self.assertIn(('volumes', ('V:alpha',)), self.provider.calls)
        self.assertIsNone(volume.get_data().last_cv_fetch)
        self.assertEqual(ProviderIdentityDB.resolve_volume_metadata_identity(
            local, PROVIDERS.keys()).last_fetch, NOW.timestamp())
        self.assertEqual(self.db.execute(
            'PRAGMA foreign_key_check').fetchall(), [])

    def test_absent_cv_naming_tokens_are_empty_not_none_unknown_or_sentinel(self):
        local = self.add_neutral()
        vd = Volume(local).get_data()
        issue = Volume(local).get_issues()[0]
        keys = get_issue_naming_keys(vd, issue)
        self.assertEqual(keys.comicvine_id, '')
        self.assertEqual(keys.issue_comicvine_id, '')
        self.assertEqual(_fill_format(
            'A{comicvine_id}B{issue_comicvine_id}C', keys), 'ABC')
        self.assertEqual(_fill_format(
            'A{comicvine_id:08d}B{issue_comicvine_id:04d}C', keys), 'ABC')

    def test_verified_cv_references_never_change_authority_or_refresh_ids(self):
        local = self.add_neutral()
        issue = Volume(local).get_issues()[0].id
        ProviderIdentityDB.put_comicvine_reference(
            ExternalIdentity(local, 'comicvine', '8765', 'verified'), True)
        ProviderIdentityDB.put_comicvine_reference(
            ExternalIdentity(issue, 'comicvine', '9876', 'verified'), False)
        self.db.commit()
        refresh_and_scan(local)
        self.assertEqual(Volume(local).get_data().comicvine_id, 8765)
        self.assertEqual(Issue(issue).get_data().comicvine_id, 9876)
        self.assertEqual(ProviderIdentityDB.selected_provider(
            local), 'test_provider')
        self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])
        self.assertIn(('volumes', ('V:alpha',)), self.provider.calls)
        with self.assertRaises(ValueError):
            ProviderIdentityDB.put_comicvine_reference(
                ExternalIdentity(local, 'comicvine', '555', 'verified'), True)
        self.assertEqual(Volume(local).get_data().comicvine_id, 8765)

    def test_missing_selected_identity_does_not_use_verified_cv_reference(self):
        local = self.add_neutral()
        ProviderIdentityDB.put_comicvine_reference(
            ExternalIdentity(local, 'comicvine', '8765', 'verified'), True)
        self.db.execute(
            "DELETE FROM volume_external_ids WHERE volume_id=? AND provider='test_provider'", (local,))
        with self.assertRaises(MetadataIdentityError):
            refresh_and_scan(local)
        self.assertTrue(ProviderIdentityDB.audit(PROVIDERS.keys()))

    def test_unregistered_provider_cannot_add(self):
        before = self.snapshot()
        with self.assertRaises(KeyError):
            Library.add_metadata(ProviderVolumeIdentity(
                'not_registered', 'opaque'), 1, True)
        self.assertEqual(self.snapshot(), before)

    def test_no_provider_is_shipped_for_gcd_or_test_provider(self):
        # test_provider is injected by this fixture; only CV and Metron ship.
        self.assertEqual(set(PROVIDERS), {'comicvine', 'metron', 'gcd', 'test_provider'})

    def test_new_issue_failure_does_not_leave_missing_identity(self):
        local = self.add_neutral()
        self.db.execute('''CREATE TRIGGER fail_neutral_identity BEFORE INSERT ON issue_external_ids
            WHEN NEW.provider_id='I:new' BEGIN SELECT RAISE(ABORT,'injected'); END''')
        self.db.commit()
        self.provider.issues.append(self.provider.issue('I:new', '3'))
        with self.assertRaisesRegex(Exception, 'injected'):
            refresh_and_scan(local)
        self.assertEqual(len(Volume(local).get_issues()), 2)
        self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])

    def test_diagnostics_distinguish_valid_absence_from_missing_selected_issue(self):
        local = self.add_neutral()
        self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])
        self.assertTrue(
            any(r[2] == 'unregistered selected provider' for r in ProviderIdentityDB.audit()))
        issue = Volume(local).get_issues()[0].id
        self.db.execute(
            "DELETE FROM issue_external_ids WHERE issue_id=? AND provider='test_provider'", (issue,))
        self.db.commit()
        self.assertIn(('issue', issue, 'missing selected identity'),
                      ProviderIdentityDB.audit(PROVIDERS.keys()))
        with self.assertRaises(MetadataIdentityError):
            refresh_and_scan(local)

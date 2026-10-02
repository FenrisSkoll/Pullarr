"""Provider-qualified imports using real add, files, mappings and Metron adapter."""

from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from fixtures.comicvine_fetch import issue_response
from fixtures.comicvine_search import volume_response
from fixtures.library_import import ImportHarness
from Tbackend.implementations.metron_lifecycle import MetronHarness

from backend.base.custom_exceptions import InvalidKeyValue
from backend.features.library_import import (import_library,
                                             propose_library_import)
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.identity_enrichment import \
    IdentityEnrichmentConflict
from backend.implementations.metadata.provider import MetadataSearchProvider
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.volumes import Volume
from backend.internals.provider_identity import (ExternalIdentity,
                                                 ProviderIdentityDB)


class ProviderImportHarness(ImportHarness, MetronHarness):
    def metron_file(self, folder='metron-incoming', number=1):
        return self.comic_file(folder, 'Example Collection v2 #%d (2020).cbz' % number)

    def mapping(self, path, provider='metron', identity='700'):
        return {'filepath': path, 'provider': provider, 'provider_id': identity}


class ProviderImport(ProviderImportHarness, TestCase):
    def test_metron_plain_import_null_cv_and_real_issue_bindings(self):
        path = self.metron_file()
        import_library([self.mapping(path)])
        self.assertEqual(self.bindings(), [(path, 1, 901)])
        self.assertIsNone(Volume(1).get_data().comicvine_id)
        self.assertEqual(ProviderIdentityDB.selected_provider(1), 'metron')
        self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])
        self.session.get.assert_not_called()
        self.assertIsNone(self.db.execute("SELECT value FROM config WHERE key='metron_background_usage'").fetchone())

    def test_metron_import_and_rename_preserves_content_and_null_cv_naming(self):
        path = self.metron_file(number=2)
        content = Path(path).read_bytes()
        self.settings.file_naming = '{series_name} #{issue_number} {comicvine_id} {issue_comicvine_id}'
        import_library([self.mapping(path)], rename_files=True)
        renamed, local, cv_id = self.bindings()[0]
        self.assertIsNone(cv_id)
        self.assertEqual(local, 2)
        self.assertNotEqual(renamed, path)
        self.assertFalse(Path(path).exists())
        self.assertEqual(Path(renamed).read_bytes(), content)
        self.assertNotIn('None', renamed)
        self.assertNotIn('null', renamed)
        self.session.get.assert_not_called()

    def test_mixed_same_numeric_ids_remain_independent(self):
        cv = self.comic_file('cv-incoming')
        metron = self.metron_file()
        self.prepare_fetch(volume_response(id=700), [
            issue_response(volume={'id': 700}),
            issue_response(id=302, issue_number='2', volume={'id': 700})])
        import_library([self.mapping(cv, 'comicvine'), self.mapping(metron)])
        self.assertEqual(self.db.execute('SELECT metadata_provider,comicvine_id FROM volumes ORDER BY id').fetchall(),
                         [('comicvine', 700), ('metron', None)])
        self.assertEqual(len(self.bindings()), 2)
        self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])

    def test_same_metron_identity_groups_files(self):
        paths = [self.metron_file(number=n) for n in (1, 2)]
        import_library([self.mapping(p) for p in paths])
        self.assertEqual(len(self.rows('volumes')), 1)
        self.assertEqual(len(self.bindings()), 2)

    def test_existing_selected_metron_import_keeps_refs_without_fetch(self):
        local = self.add_metron()
        ProviderIdentityDB.put_volume_identity(ExternalIdentity(local, 'other_reference', 'opaque', 'verified'))
        before = ProviderIdentityDB.volume_identities(local)
        issue_refs = ProviderIdentityDB.issue_identities(Volume(local).get_issues()[0].id)
        path = self.metron_file()
        self.http.get.reset_mock()
        import_library([self.mapping(path)])
        self.assertEqual(len(self.rows('volumes')), 1)
        self.assertEqual(len(self.bindings()), 1)
        self.assertEqual(ProviderIdentityDB.volume_identities(local), before)
        self.assertEqual(ProviderIdentityDB.issue_identities(Volume(local).get_issues()[0].id), issue_refs)
        self.http.get.assert_not_called()

    def test_genuine_cv_enrichment_does_not_switch_authority(self):
        self.series['cv_id'] = 4567
        import_library([self.mapping(self.metron_file())])
        self.assertEqual(Volume(1).get_data().comicvine_id, 4567)
        self.assertEqual(ProviderIdentityDB.selected_provider(1), 'metron')

    def test_cross_provider_cv_collision_leaves_files_unchanged(self):
        self.add_volume()
        self.series['cv_id'] = 2127
        path = self.metron_file()
        before = self.state()
        with self.assertRaises(IdentityEnrichmentConflict):
            import_library([self.mapping(path)], rename_files=True)
        self.assertEqual(self.state(), before)
        self.assertTrue(Path(path).exists())

    def test_reference_only_existing_identity_is_not_import_authority(self):
        local = self.add_volume()
        ProviderIdentityDB.put_volume_identity(ExternalIdentity(local, 'metron', '700', 'verified'))
        self.db.commit()
        path = self.metron_file()
        with self.assertRaises(IdentityEnrichmentConflict):
            import_library([self.mapping(path)])
        self.http.get.assert_not_called()
        self.assertTrue(Path(path).exists())
        self.assertEqual(ProviderIdentityDB.selected_provider(local), 'comicvine')

    def test_missing_credentials_and_http_failures_do_not_move_files(self):
        path = self.metron_file()
        self.settings.metron_api_token = ''
        with self.assertRaises(MetadataProviderError):
            import_library([self.mapping(path)], rename_files=True)
        self.http.get.assert_not_called()
        self.settings.metron_api_token = 'fake-unit-token'
        for status in (401, 403, 404, 429, 500):
            with patch.dict('backend.implementations.metadata.metron_client.RATE_STATE', {}, clear=True):
                self.http.get.side_effect = None
                self.http.get.return_value = Mock(status_code=status, headers={'Retry-After': '60'})
                with self.assertRaises(MetadataProviderError):
                    import_library([self.mapping(path)], rename_files=True)
                self.assertTrue(Path(path).is_file())
                self.assertEqual(self.rows('volumes'), [])
        self.session.get.assert_not_called()

    def test_manual_only_proposal_scan_needs_no_comicvine_credentials(self):
        path = self.metron_file()
        with patch('backend.features.library_import.ComicVine') as cv:
            rows = propose_library_import(auto_match=False)
        cv.assert_not_called()
        self.assertEqual(rows[0]['filepath'], path)
        self.assertIsNone(rows[0]['metadata_source'])
        self.assertIsNone(rows[0]['cv']['id'])

    def test_invalid_ambiguous_or_unsupported_identity_rejected_before_fetch(self):
        path = self.metron_file()
        for data in ({'filepath': path, 'provider_id': '700'},
                     {'filepath': path, 'provider': 'metron'},
                     {'filepath': path, 'provider': 'metron', 'provider_id': 700},
                     dict(self.mapping(path), id=700),
                     self.mapping(path, 'unregistered_fixture')):
            with self.assertRaises(InvalidKeyValue):
                import_library([data])
        self.http.get.assert_not_called()

    def test_search_only_provider_cannot_import(self):
        class SearchOnly(MetadataSearchProvider):
            async def search_volumes(self, query):
                return []
        with patch.dict(PROVIDERS, {'search_only': SearchOnly}):
            with self.assertRaises(MetadataProviderError) as caught:
                import_library([self.mapping(self.metron_file(), 'search_only')])
        self.assertEqual(caught.exception.reason, 'unsupported_capability')

    def test_later_failure_keeps_earlier_committed_group_and_failed_files(self):
        cv_path = self.comic_file('cv-incoming')
        metron_path = self.metron_file()
        self.settings.metron_api_token = ''
        with self.assertRaises(MetadataProviderError):
            import_library([{'filepath': cv_path, 'id': 2127}, self.mapping(metron_path)])
        self.assertEqual(self.bindings(), [(cv_path, 1, 301)])
        self.assertTrue(Path(metron_path).is_file())
        self.assertEqual(len(self.rows('volumes')), 1)

    def test_legal_duplicate_cv_selected_ids_keep_first_local_lookup(self):
        first = self.add_volume()
        self.db.execute("INSERT INTO volumes(comicvine_id,title,root_folder) VALUES (2127,'Duplicate',1)")
        self.db.commit()
        self.assertEqual(ProviderIdentityDB.find_selected_volume('comicvine', '2127'), first)
        self.assertEqual(len(self.rows('volumes')), 2)

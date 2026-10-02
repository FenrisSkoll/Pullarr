"""Library Import API uses the same Metron search/add integration as Add Volume."""

from unittest import TestCase
from unittest.mock import patch

from fixtures.comicvine_search import FAKE_APP_KEY
from Tbackend.implementations.metron_lifecycle import MetronHarness
from Tfrontend.library_import_contract import ImportAPIHarness

from backend.implementations.metadata.provider import MetadataVolumeProvider
from backend.implementations.metadata.registry import PROVIDERS


class ProviderImportAPI(ImportAPIHarness, MetronHarness, TestCase):
    def test_manual_metron_search_to_import_without_comicvine(self):
        path = self.comic_file(name='Example Collection v2 #1 (2020).cbz')
        response = self.client.get('/api/volumes/search', query_string={
            'api_key': FAKE_APP_KEY, 'provider': 'metron', 'query': 'Example'})
        self.assertEqual(response.status_code, 200)
        match = response.get_json()['result'][0]['metadata_source']
        response = self.post_import([{'filepath': path, 'provider': match['provider'],
                                      'provider_id': match['id']}])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.bindings(), [(path, 1, 901)])
        self.session.get.assert_not_called()

    def test_manual_only_scan_no_cv_and_strict_option_validation(self):
        path = self.comic_file()
        response = self.client.get('/api/libraryimport', query_string={
            'api_key': FAKE_APP_KEY, 'auto_match': 'false'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['result'][0]['filepath'], path)
        self.assertIsNone(response.get_json()['result'][0]['metadata_source'])
        self.session.get.assert_not_called()
        response = self.client.get('/api/libraryimport', query_string={
            'api_key': FAKE_APP_KEY, 'auto_match': 'invalid'})
        self.assertEqual(response.status_code, 400)

    def test_missing_metron_token_surfaces_provider_error(self):
        self.settings.metron_api_token = ''
        response = self.post_import([{'filepath': self.comic_file(), 'provider': 'metron',
                                     'provider_id': '700'}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'MetadataProviderError')
        self.assertEqual(response.get_json()['result']['provider'], 'metron')
        self.session.get.assert_not_called()

    def test_cross_provider_reference_conflict_returns_409(self):
        self.add_volume()
        self.series['cv_id'] = 2127
        response = self.post_import([{'filepath': self.comic_file(), 'provider': 'metron',
                                     'provider_id': '700'}])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error'], 'IdentityEnrichmentConflict')
        self.assertEqual(len(self.rows('volumes')), 1)

    def test_search_capability_missing_fails_explicitly(self):
        class VolumeOnly(MetadataVolumeProvider):
            async def fetch_volume(self, provider_id):
                raise AssertionError('Search must never invoke fetch')
        with patch.dict(PROVIDERS, {'volume_only': VolumeOnly}):
            response = self.client.get('/api/volumes/search', query_string={
                'api_key': FAKE_APP_KEY, 'provider': 'volume_only', 'query': 'Example'})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()['result']['reason'], 'unsupported_capability')

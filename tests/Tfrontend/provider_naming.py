"""Unpersisted Add Volume folder previews retain explicit source identity."""

from unittest import TestCase

from fixtures.comicvine_search import FAKE_APP_KEY
from Tfrontend.library_import_contract import ImportAPIHarness


class NamingPreviewAPI(ImportAPIHarness, TestCase):
    def test_legacy_and_qualified_folder_previews(self):
        self.settings.volume_folder_naming = '{series_name} [{metadata_provider}-{provider_id}] CV[{comicvine_id}]'
        base = {'title': 'Example', 'year': 2020, 'volume_number': 1, 'publisher': None}
        for identity, expected in (
            ({'comicvine_id': 123}, 'Example [comicvine-123] CV[123]'),
            ({'comicvine_id': None, 'provider': 'metron', 'provider_id': '700'}, 'Example [metron-700] CV[]'),
            ({'comicvine_id': 999, 'provider': 'metron', 'provider_id': '700'}, 'Example [metron-700] CV[999]')
        ):
            response = self.client.post('/api/volumes/search', json={**base, **identity},
                                        query_string={'api_key': FAKE_APP_KEY})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['result']['folder'], expected)
        self.assertEqual(self.rows('volumes'), [])
        self.session.get.assert_not_called()

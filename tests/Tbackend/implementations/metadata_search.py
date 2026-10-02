"""Test the adapter from raw HTTP fixtures through the neutral result."""

import unittest
from asyncio import run
from dataclasses import asdict
from unittest.mock import patch

from fixtures.comicvine_search import (ComicVineSearchHarness,
                                       public_result, volume_response)

from backend.base.custom_exceptions import (InvalidKeyValue,
                                            MetadataSourceRateLimitReached)
from backend.base.definitions import StatusType
from backend.implementations.metadata.comicvine import \
    ComicVineMetadataProvider
from backend.implementations.metadata.models import VolumeSearchResult
from backend.implementations.metadata.provider import MetadataSearchProvider
from backend.implementations.metadata.registry import get_search_provider


class ComicVineProviderSearch(ComicVineSearchHarness, unittest.TestCase):
    def test_maps_all_search_fields_and_namespaces_identity(self):
        self.db.execute('INSERT INTO volumes VALUES (73, 2127);')
        result = run(ComicVineMetadataProvider().search_volumes('Example Hero'))
        expected = public_result(already_added=73)
        del expected['comicvine_id']
        del expected['issues']
        expected.update(provider='comicvine', provider_id='2127')
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], VolumeSearchResult)
        self.assertEqual(asdict(result[0]), expected)
        self.status.clear.assert_called_once_with(
            StatusType.CV_RATE_LIMIT, 'search_volumes'
        )

    def test_preserves_translation_and_result_order(self):
        self.respond([
            volume_response(id=9001, description='<p>French publication.</p>'),
            volume_response()
        ])
        results = run(ComicVineMetadataProvider().search_volumes('Example'))
        self.assertEqual([r.provider_id for r in results], ['9001', '2127'])
        self.assertEqual([r.translated for r in results], [True, False])

    def test_empty_search_stays_empty(self):
        self.respond([])
        self.assertEqual(
            run(ComicVineMetadataProvider().search_volumes('x')), [])

    def test_direct_id_query_is_forwarded(self):
        self.respond(volume_response())
        results = run(ComicVineMetadataProvider().search_volumes('cv:2127'))
        self.assertEqual(results[0].provider_id, '2127')
        self.assertTrue(
            self.session.get.call_args[0][0].endswith('/4050-2127/'))

    def test_existing_exceptions_pass_through_without_wrapping(self):
        for error in (
            InvalidKeyValue('comicvine_api_key', 'test-only-key'),
            MetadataSourceRateLimitReached(),
            KeyError('image')
        ):
            with self.subTest(error=type(error).__name__):
                with patch(
                    'backend.implementations.comicvine.ComicVine.search_volumes',
                    side_effect=error
                ) as search:
                    with self.assertRaises(type(error)) as raised:
                        run(ComicVineMetadataProvider().search_volumes('x'))
                    self.assertIs(raised.exception, error)
                    search.assert_awaited_once_with('x')

    def test_missing_credentials_still_fail_at_search(self):
        self.settings.comicvine_api_key = ''
        with self.assertRaises(InvalidKeyValue):
            run(ComicVineMetadataProvider().search_volumes('x'))
        self.session.get.assert_not_awaited()


class SearchProviderResolution(unittest.TestCase):
    def test_default_and_explicit_comicvine_provider(self):
        for provider in (
            get_search_provider(),
            get_search_provider('comicvine')):
            self.assertIsInstance(provider, MetadataSearchProvider)
            self.assertIsInstance(provider, ComicVineMetadataProvider)

    def test_resolution_does_not_cache_a_client_or_read_credentials(self):
        with patch('backend.implementations.comicvine.Settings') as settings:
            self.assertIsNot(get_search_provider(), get_search_provider())
            settings.assert_not_called()

    def test_unknown_provider_is_not_silently_replaced(self):
        with self.assertRaises(KeyError):
            get_search_provider('not-registered')

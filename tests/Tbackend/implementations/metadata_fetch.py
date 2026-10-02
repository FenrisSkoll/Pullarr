"""Neutral fetch identity, legacy parity and independent capabilities."""

import unittest
from asyncio import run
from dataclasses import asdict, replace
from unittest.mock import AsyncMock, Mock, patch

from fixtures.comicvine_fetch import (ComicVineFetchHarness,
                                      envelope, issue_response)
from fixtures.comicvine_search import volume_response

from backend.base.custom_exceptions import (InvalidKeyValue,
                                            MetadataSourceRateLimitReached,
                                            VolumeNotMatched)
from backend.base.definitions import DateType
from backend.implementations.comicvine import ComicVine
from backend.implementations.metadata.comicvine import \
    ComicVineMetadataProvider
from backend.implementations.metadata.legacy import legacy_volume_metadata
from backend.implementations.metadata.provider import (MetadataSearchProvider,
                                                       MetadataVolumeProvider)
from backend.implementations.metadata.registry import (PROVIDERS,
                                                       get_search_provider,
                                                       get_volume_provider)


class MetadataFetch(ComicVineFetchHarness, unittest.TestCase):
    def test_neutral_volume_and_issue_identity_and_fields(self):
        metadata = run(ComicVineMetadataProvider().fetch_volume('2127'))
        self.assertEqual(
            (metadata.provider, metadata.provider_id),
            ('comicvine', '2127'))
        self.assertEqual(
            asdict(metadata.issues[0]),
            {'provider': 'comicvine', 'provider_id': '301',
             'volume_provider_id': '2127', 'issue_number': '1',
             'calculated_issue_number': 1.0, 'title': 'The Beginning',
             'date': '2021-01-01', 'description': '<p>First issue.</p>'})
        self.assertNotIn('comicvine_id', asdict(metadata))
        self.assertNotIn('already_added', asdict(metadata))
        self.assertEqual(metadata.aliases, ['Alternate Hero', 'Another Hero'])
        self.assertEqual(metadata.issue_count, 12)
        self.assertEqual(len(metadata.issues), 1)

    def test_exact_legacy_roundtrip_for_real_transforms(self):
        cases = (
            (volume_response(), [issue_response(id=302, issue_number='2'), issue_response()]),
            (volume_response(count_of_issues=1), [issue_response(name='TPB')]),
            (volume_response(description='<p>French translation of Example Hero.</p>'),
             [issue_response(issue_number='1/2')]),
            (volume_response(name=None, start_year=None, deck=None, aliases=None,
                             publisher=None, description=None, image={'small_url': None}),
             [issue_response(name=None, cover_date=None, description=None)]),
            (volume_response(), [])
        )
        for raw_volume, raw_issues in cases:
            with self.subTest(volume=raw_volume, issues=raw_issues):
                self.prepare_fetch(raw_volume, raw_issues)
                old = run(ComicVine().fetch_volume(2127))
                self.prepare_fetch(raw_volume, raw_issues)
                new = run(get_volume_provider().fetch_volume('2127'))
                self.assertEqual(legacy_volume_metadata(new), old)

    def test_missing_cover_and_store_date_roundtrip(self):
        self.settings.date_type = DateType.STORE_DATE
        self.session.get_content.return_value = None
        old = run(ComicVine().fetch_volume(2127))
        self.prepare_fetch()
        new = run(get_volume_provider().fetch_volume('2127'))
        self.assertEqual(legacy_volume_metadata(new), old)

    def test_partial_issue_page_roundtrip(self):
        responses = [envelope(volume_response()),
                     envelope([issue_response()], number_of_total_results=101),
                     envelope([], 107)]
        self.response.json.side_effect = responses
        old = run(ComicVine().fetch_volume(2127))
        self.response.json.side_effect = responses
        new = run(get_volume_provider().fetch_volume('2127'))
        self.assertEqual(legacy_volume_metadata(new), old)

    def test_delegates_string_id_without_rewriting(self):
        legacy = run(ComicVine().fetch_volume(2127))
        fetch = self.start_patch(
            'backend.implementations.metadata.comicvine.ComicVine')
        fetch.return_value.fetch_volume = AsyncMock(return_value=legacy)
        result = run(ComicVineMetadataProvider().fetch_volume('4050-2127'))
        fetch.return_value.fetch_volume.assert_awaited_once_with('4050-2127')
        self.assertEqual(legacy_volume_metadata(result), legacy)

    def test_exceptions_are_the_same_objects_without_wrapping(self):
        fetch = self.start_patch(
            'backend.implementations.metadata.comicvine.ComicVine')
        for error in (
            VolumeNotMatched(),
            InvalidKeyValue(
                'comicvine_api_key',
                'test-only-key'),
            MetadataSourceRateLimitReached(),
            KeyError('image')):
            with self.subTest(error=type(error)):
                fetch.return_value.fetch_volume = AsyncMock(side_effect=error)
                with self.assertRaises(type(error)) as caught:
                    run(ComicVineMetadataProvider().fetch_volume('2127'))
                self.assertIs(caught.exception, error)

    def test_non_comicvine_volume_is_rejected_even_with_numeric_id(self):
        result = run(get_volume_provider().fetch_volume('2127'))
        with self.assertRaisesRegex(ValueError, 'ComicVine volume and issue identities'):
            legacy_volume_metadata(replace(result, provider='other'))

    def test_non_comicvine_issue_is_rejected_even_with_numeric_id(self):
        result = run(get_volume_provider().fetch_volume('2127'))
        result.issues[0] = replace(result.issues[0], provider='other')
        with self.assertRaisesRegex(ValueError, 'ComicVine volume and issue identities'):
            legacy_volume_metadata(result)

    def test_neutral_ids_can_be_non_numeric_but_legacy_mapper_rejects_them(
        self):
        result = run(get_volume_provider().fetch_volume('2127'))
        for changed in (
            replace(result, provider_id='opaque-volume'),
            replace(
                result,
                issues=[
                        replace(
                            result.issues[0],
                            provider_id='opaque-issue')]),
            replace(
                result,
                issues=[
                        replace(
                            result.issues[0],
                            volume_provider_id='opaque-parent')])):
            with self.subTest(metadata=changed):
                with self.assertRaises(ValueError):
                    legacy_volume_metadata(changed)


class FetchProviderResolution(unittest.TestCase):
    def test_one_registry_supports_both_independent_capabilities(self):
        self.assertEqual(set(PROVIDERS), {'comicvine', 'metron', 'gcd'})
        self.assertIsInstance(get_search_provider(), MetadataSearchProvider)
        self.assertIsInstance(get_volume_provider(), MetadataVolumeProvider)
        self.assertIsInstance(get_volume_provider(), ComicVineMetadataProvider)
        self.assertIsNot(get_volume_provider(), get_volume_provider())

    def test_unknown_provider_rejected(self):
        with self.assertRaises(KeyError):
            get_volume_provider('unregistered')

    def test_wrong_capability_rejected(self):
        with patch.dict(PROVIDERS, {'search-only': Mock(return_value=Mock(spec=MetadataSearchProvider)),
                                    'fetch-only': Mock(return_value=Mock(spec=MetadataVolumeProvider))}):
            with self.assertRaisesRegex(TypeError, 'volume fetch'):
                get_volume_provider('search-only')
            with self.assertRaisesRegex(TypeError, 'search'):
                get_search_provider('fetch-only')

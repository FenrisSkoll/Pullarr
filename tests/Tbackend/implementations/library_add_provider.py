"""Prove the add caller uses the capability before any persistence writes."""

import unittest
from asyncio import run
from dataclasses import replace
from unittest.mock import AsyncMock

from fixtures.comicvine_fetch import LibraryAddHarness

from backend.base.custom_exceptions import (MetadataSourceRateLimitReached,
                                            VolumeAlreadyAdded)
from backend.implementations.metadata.registry import get_volume_provider


class LibraryAddProvider(LibraryAddHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.metadata = run(get_volume_provider().fetch_volume('2127'))
        self.resolver = self.start_patch(
            'backend.implementations.volumes.get_volume_provider')
        self.fetch = AsyncMock(return_value=self.metadata)
        self.resolver.return_value.fetch_volume = self.fetch
        self.direct = self.start_patch(
            'backend.implementations.comicvine.ComicVine.fetch_volume',
            side_effect=AssertionError('Add must resolve the provider'))

    def test_add_resolves_default_provider_and_passes_string_id(self):
        self.add_volume()
        self.resolver.assert_called_once_with('comicvine')
        self.fetch.assert_awaited_once_with('2127')
        self.direct.assert_not_called()
        self.assertEqual(self.rows('volumes')[0]['comicvine_id'], 2127)
        self.assertEqual(self.rows('issues')[0]['comicvine_id'], 301)

    def test_duplicate_still_precedes_provider_resolution(self):
        self.add_volume()
        self.resolver.reset_mock()
        self.fetch.reset_mock()
        with self.assertRaises(VolumeAlreadyAdded):
            self.add_volume()
        self.resolver.assert_not_called()
        self.fetch.assert_not_awaited()

    def test_provider_exception_propagates_without_writes(self):
        error = MetadataSourceRateLimitReached()
        self.fetch.side_effect = error
        with self.assertRaises(MetadataSourceRateLimitReached) as caught:
            self.add_volume()
        self.assertIs(caught.exception, error)
        self.assert_empty_library()

    def test_other_provider_volume_rejected_before_writes(self):
        self.fetch.return_value = replace(self.metadata, provider='other')
        with self.assertRaisesRegex(ValueError, 'ComicVine volume and issue identities'):
            self.add_volume()
        self.assert_empty_library()

    def test_other_provider_issue_rejected_before_writes(self):
        self.metadata.issues[0] = replace(
            self.metadata.issues[0], provider='other')
        with self.assertRaisesRegex(ValueError, 'ComicVine volume and issue identities'):
            self.add_volume()
        self.assert_empty_library()

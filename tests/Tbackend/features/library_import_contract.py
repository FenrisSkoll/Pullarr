"""Characterization of the pre-2B ComicVine importer, before refactoring."""

from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures.comicvine_fetch import issue_response
from fixtures.library_import import ImportHarness

from backend.base.custom_exceptions import (InvalidKeyValue,
                                            MetadataSourceRateLimitReached,
                                            VolumeNotMatched)
from backend.features.library_import import (import_library,
                                             propose_library_import)
from backend.implementations.volumes import Volume


class ImportContract(ImportHarness, TestCase):
    def proposals(self, unmatched=False):
        async def match(groups, only_english):
            return {group: {'id': None if unmatched else 2127 + group - 1,
                            'title': None if unmatched else 'Example Hero (2021)',
                            'issue_count': None if unmatched else 12,
                            'link': None if unmatched else 'https://example.invalid/cv'}
                    for group in groups}
        return patch('backend.features.library_import.ComicVine.filenames_to_cvs',
                     new=AsyncMock(side_effect=match))

    def test_single_proposal_preserves_remote_id_and_filepath(self):
        path = self.comic_file()
        with self.proposals() as matcher:
            result = propose_library_import()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['filepath'], path)
        self.assertEqual(result[0]['cv']['id'], 2127)
        matcher.assert_awaited_once()

    def test_multiple_files_grouped_and_other_series_separate(self):
        first = self.comic_file()
        second = self.comic_file(name='Example Hero v2 #2 (2021).cbz')
        third = self.comic_file(name='Different Series #1 (2021).cbz')
        with self.proposals():
            result = {r['filepath']: r for r in propose_library_import()}
        self.assertEqual(result[first]['group_number'], result[second]['group_number'])
        self.assertNotEqual(result[first]['group_number'], result[third]['group_number'])

    def test_unmatched_proposal_is_editable_null_match(self):
        self.comic_file()
        with self.proposals(True):
            self.assertEqual(propose_library_import()[0]['cv'], {
                'id': None, 'title': None, 'issue_count': None, 'link': None})

    def test_proposal_auth_error_propagates(self):
        self.comic_file()
        with patch('backend.features.library_import.ComicVine.filenames_to_cvs',
                   new=AsyncMock(side_effect=InvalidKeyValue('comicvine_api_key', 'fake'))):
            with self.assertRaises(InvalidKeyValue):
                propose_library_import()

    def test_retired_mutating_import_requires_review_for_every_legacy_shape(self):
        path = self.comic_file()
        for payload in ([{'id': 2127, 'filepath': path}],
                        [{'id': '2127', 'filepath': path}],
                        [{'id': 2127, 'filepath': path}] * 2):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                    ValueError, 'legacy_import_requires_review'):
                import_library(payload)
        with self.assertRaisesRegex(ValueError, 'legacy_import_requires_review'):
            import_library([{'id': 2127, 'filepath': path}], rename_files=True)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.rows('volumes'), [])
        self.assertEqual(self.bindings(), [])
        self.session.get.assert_not_called()

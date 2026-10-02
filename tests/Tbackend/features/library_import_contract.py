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

    def test_plain_import_keeps_file_and_maps_local_issue(self):
        path = self.comic_file()
        import_library([{'id': 2127, 'filepath': path}])
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.bindings(), [(path, 1, 301)])
        self.assertEqual(Path(Volume(1).get_data().folder), Path(path).parent)

    def test_rename_moves_and_maps_file_without_changing_contents(self):
        path = self.comic_file()
        content = Path(path).read_bytes()
        import_library([{'id': 2127, 'filepath': path}], rename_files=True)
        binding = self.bindings()[0]
        self.assertEqual(binding[1:], (1, 301))
        self.assertNotEqual(binding[0], path)
        self.assertEqual(Path(binding[0]).read_bytes(), content)
        self.assertFalse(Path(path).exists())

    def test_same_remote_id_groups_two_files_into_one_volume(self):
        paths = [self.comic_file(), self.comic_file(name='Example Hero v2 #2 (2021).cbz')]
        self.prepare_fetch(issues=[issue_response(), issue_response(id=302, issue_number='2')])
        import_library([{'id': 2127, 'filepath': p} for p in paths])
        self.assertEqual(len(self.rows('volumes')), 1)
        self.assertEqual(len(self.bindings()), 2)

    def test_duplicate_identical_path_retains_existing_common_folder_failure(self):
        path = self.comic_file()
        # common_folder sees identical paths as the common path, not its parent.
        with self.assertRaises(FileExistsError):
            import_library([{'id': 2127, 'filepath': path}] * 2)
        self.assertEqual(len(self.rows('volumes')), 0)
        self.assertTrue(Path(path).is_file())

    def test_existing_volume_moves_new_file_without_metadata_fetch(self):
        local = self.add_volume()
        path = self.comic_file()
        self.session.get.reset_mock()
        import_library([{'id': 2127, 'filepath': path}])
        self.assertFalse(Path(path).exists())
        self.assertEqual(len(self.rows('volumes')), 1)
        self.assertEqual(Path(self.bindings()[0][0]).parent, Path(Volume(local).get_data().folder))
        self.session.get.assert_not_called()

    def test_legacy_decimal_string_is_comicvine_identity(self):
        path = self.comic_file()
        import_library([{'id': '2127', 'filepath': path}])
        self.assertEqual(Volume(1).get_data().comicvine_id, 2127)

    def test_cv_not_found_skips_and_rate_limit_stops_without_moving(self):
        path = self.comic_file()
        for error in (VolumeNotMatched(), MetadataSourceRateLimitReached()):
            with patch('backend.implementations.metadata.comicvine.ComicVine.fetch_volume',
                       new=AsyncMock(side_effect=error)):
                import_library([{'id': 2127, 'filepath': path}], rename_files=True)
            self.assertTrue(Path(path).is_file())
            self.assertEqual(self.rows('volumes'), [])

    def test_outside_root_is_skipped(self):
        path = str(self.root.parent / 'outside' / 'test.cbz')
        import_library([{'id': 2127, 'filepath': path}])
        self.session.get.assert_not_called()

    def test_automatic_proposal_can_be_imported_unchanged(self):
        self.comic_file()
        with self.proposals():
            rows = propose_library_import()
        import_library([{'id': r['cv']['id'], 'filepath': r['filepath']} for r in rows])
        self.assertEqual(len(self.bindings()), 1)

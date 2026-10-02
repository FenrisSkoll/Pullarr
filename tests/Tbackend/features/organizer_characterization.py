"""Phase 4A: observable legacy discovery, association and failure boundaries.

These are not desired future organizer policies. All files are disposable.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fixtures.library_import import ImportHarness

from backend.base.files import list_files
from backend.features.library_import import (import_library,
                                             propose_library_import)
from backend.implementations.naming import same_name_indexing


class DiscoveryAndCollisionContract(TestCase):
    def test_hidden_directories_are_traversed_but_dotfiles_are_excluded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            hidden = root / '.hidden'
            hidden.mkdir()
            visible = hidden / 'issue.CBZ'
            visible.touch()
            (root / '.issue.cbz').touch()
            (root / 'issue.txt').touch()
            self.assertEqual(list_files(directory, ['cbz']), [str(visible)])

    def test_temporary_suffix_is_filtered_but_tilde_prefix_is_not(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            accepted = root / '~issue.cbz'
            accepted.touch()
            (root / 'issue.cbz.part').touch()
            self.assertEqual(list_files(directory, ['.CBZ']), [str(accepted)])

    def test_traversal_permission_error_propagates(self):
        with patch('backend.base.files.scandir', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                list_files('not-accessed', ['cbz'])

    def test_existing_and_planned_collision_names_are_reserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'issue.cbz'
            target.touch()
            indexed = root / 'issue (1).cbz'
            indexed.touch()
            plan = {str(root / 'a.cbz'): str(target),
                    str(root / 'b.cbz'): str(target)}
            self.assertEqual(list(same_name_indexing(directory, plan).values()),
                             [str(root / 'issue (2).cbz'),
                              str(root / 'issue (3).cbz')])
            self.assertTrue(target.exists())
            self.assertFalse((root / 'issue (2).cbz').exists())

    def test_unchanged_path_is_not_indexed(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / 'issue.cbz')
            Path(path).touch()
            self.assertEqual(same_name_indexing(directory, {path: path}),
                             {path: path})

    def test_collision_comparison_is_string_case_sensitive(self):
        # Freeze the comparison, independent of host filesystem case behavior.
        with patch('backend.implementations.naming.isdir', return_value=True), \
                patch('backend.implementations.naming.list_files',
                      return_value=['/volume/Issue.cbz']):
            plan = {'/source/file.cbz': '/volume/issue.cbz'}
            self.assertEqual(same_name_indexing('/volume', plan), plan)
            self.assertEqual(plan['/source/file.cbz'], '/volume/issue.cbz')


class OrganizerImportContract(ImportHarness, TestCase):
    def test_root_level_comics_are_not_proposed(self):
        self.comic_file(folder='')
        with patch('backend.features.library_import.ComicVine') as provider:
            self.assertEqual(propose_library_import(auto_match=False), [])
        provider.assert_not_called()

    def test_manual_only_discovery_does_not_open_comic_archives(self):
        # The harness writes placeholder bytes, not a valid archive. Discovery
        # still returns the file: this is path parsing, not archive validation.
        path = self.comic_file()
        with patch('backend.features.library_import.ComicVine') as provider:
            result = propose_library_import(auto_match=False)
        self.assertEqual([row['filepath'] for row in result], [path])
        self.assertIsNone(result[0]['metadata_source'])
        provider.assert_not_called()

    def test_one_range_file_associates_with_two_local_issues(self):
        path = self.comic_file(name='Example Hero v2 #1-2 (2021).cbz')
        import_library([{'id': 2127, 'filepath': path}])
        self.assertEqual(self.bindings(), [(path, 1, 301), (path, 2, 302)])
        self.assertEqual(len(self.rows('files')), 1)

    def test_move_failure_can_follow_committed_metadata(self):
        # Known recovery gap, NOT a promise that the future organizer should
        # preserve this ordering. No retry is attempted after the failure.
        path = self.comic_file()
        with patch('backend.features.library_import.rename_file',
                   side_effect=PermissionError('synthetic move failure')):
            with self.assertRaises(PermissionError):
                import_library([{'id': 2127, 'filepath': path}], rename_files=True)
        self.assertEqual(len(self.rows('volumes')), 1)
        self.assertEqual(len(self.rows('issues')), 2)
        self.assertEqual(self.bindings(), [])
        self.assertTrue(Path(path).is_file())

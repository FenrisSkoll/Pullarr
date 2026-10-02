"""Disposable import filesystem, real SQLite, parser, scan and rename."""

from os import sep

from fixtures.comicvine_fetch import LibraryAddHarness, issue_response

from backend.base.definitions import RootFolder


class ImportHarness(LibraryAddHarness):
    def setUp(self):
        super().setUp()
        self.prepare_fetch(issues=[issue_response(), issue_response(id=302, issue_number='2')])
        self.settings.delete_empty_folders = False
        folder = str(self.root) + sep
        roots = self.start_patch('backend.features.library_import.RootFolders').return_value
        roots.get_folder_list.return_value = [folder]
        roots.get_all.return_value = [RootFolder(1, folder, None)]
        self.start_patch('backend.features.library_import.commit', side_effect=self.db.commit)
        self.start_patch('backend.features.quality.get_db', side_effect=self.db.cursor)
        self.start_patch('backend.implementations.file_matching.get_db', side_effect=self.db.cursor)
        settings = self.start_patch('backend.implementations.file_matching.Settings').return_value
        settings.get_settings.return_value = self.settings
        self.start_patch('backend.implementations.naming.RootFolders').return_value.__getitem__.return_value = folder
        self.start_patch('backend.implementations.naming.mass_process_files')

    def comic_file(self, folder='incoming', name='Example Hero v2 #1 (2021).cbz'):
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'deterministic disposable comic placeholder')
        return str(path)

    def bindings(self):
        return self.db.execute('''SELECT f.filepath,i.id,i.comicvine_id FROM files f
            JOIN issues_files b ON b.file_id=f.id JOIN issues i ON i.id=b.issue_id
            ORDER BY i.id,f.filepath''').fetchall()

"""Frozen pre-2C naming behavior on real disposable persisted objects."""

from pathlib import Path
from unittest import TestCase

from fixtures.library_import import ImportHarness
from Tbackend.implementations.metron_lifecycle import MetronHarness

from backend.base.definitions import SpecialVersion
from backend.features.library_import import import_library
from backend.implementations.naming import (_fill_format, check_format,
                                            generate_issue_name,
                                            generate_volume_folder_name,
                                            get_issue_naming_keys,
                                            mass_rename, preview_mass_rename,
                                            same_name_indexing)
from backend.implementations.volumes import Volume


class NamingIdentityContract(ImportHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.local = self.add_volume()
        self.volume = Volume(self.local)

    def test_folder_and_normal_issue_exact(self):
        self.settings.volume_folder_naming = '{series_name} ({year}) v{volume_number} [{comicvine_id:06d}]'
        self.settings.file_naming = '{series_name} Issue {issue_number} [{issue_comicvine_id:05d}]'
        vd = self.volume.get_data()
        self.assertEqual(generate_volume_folder_name(vd), 'Example Hero (2021) v02 [002127]')
        self.assertEqual(generate_issue_name(vd, 1.0), 'Example Hero Issue 001 [00301]')

    def test_special_versions_exact(self):
        self.settings.file_naming_special_version = '{series_name} {special_version} [{comicvine_id}]'
        self.settings.long_special_version = False
        vd = self.volume.get_data()
        for special, label in ((SpecialVersion.TPB, 'TPB'), (SpecialVersion.ONE_SHOT, 'OS'),
                               (SpecialVersion.HARD_COVER, 'HC'), (SpecialVersion.OMNIBUS, 'Omnibus')):
            with self.subTest(special=special):
                vd.special_version = special
                self.assertEqual(generate_issue_name(vd, None), 'Example Hero ' + label + ' [2127]')

    def test_absent_cv_ids_numeric_specs_and_text(self):
        vd = self.volume.get_data()
        issue = self.volume.get_issues()[0]
        vd.comicvine_id = issue.comicvine_id = None
        keys = get_issue_naming_keys(vd, issue)
        self.assertEqual(_fill_format('CV[{comicvine_id:05d}] issue[{issue_comicvine_id:05d}]', keys),
                         'CV[] issue[]')

    def test_range_keeps_first_legacy_identity(self):
        self.settings.file_naming = 'Issue {issue_number} [{issue_comicvine_id}]'
        self.assertEqual(generate_issue_name(self.volume.get_data(), (1.0, 2.0)),
                         'Issue 001 - 002 [301]')

    def test_unknown_values_and_invalid_key(self):
        vd = self.volume.get_data()
        vd.year = vd.publisher = None
        issue = self.volume.get_issues()[0]
        issue.date = issue.title = None
        keys = get_issue_naming_keys(vd, issue)
        self.assertEqual(_fill_format('{year} {publisher} {issue_release_date} {issue_title}', keys),
                         'Unknown Year Unknown Publisher Unknown Date Unknown')
        self.assertFalse(check_format('{does_not_exist}', 'file_naming'))

    def test_collision_suffixes(self):
        first = self.comic_file(name='keep.cbz')
        target = str(Path(first).with_name('target.cbz'))
        Path(target).write_bytes(b'occupied')
        result = same_name_indexing(str(Path(first).parent), {first: target, 'other': target})
        self.assertEqual(result[first], str(Path(target).with_name('target (1).cbz')))
        self.assertEqual(result['other'], str(Path(target).with_name('target (2).cbz')))

    def test_preview_and_real_rename_agree(self):
        source = self.comic_file()
        import_library([{'filepath': source, 'id': 2127}])
        current = self.bindings()[0][0]
        self.settings.file_naming = 'Issue {issue_number} [{issue_comicvine_id:05d}]'
        before = Path(current).read_bytes()
        planned, _ = preview_mass_rename(self.local)
        self.assertTrue(Path(current).exists())
        result = mass_rename(self.local, process_individual_files=False)
        self.assertEqual(result, list(planned.values()))
        self.assertEqual(Path(result[0]).name, 'Issue 001 [00301].cbz')
        self.assertEqual(Path(result[0]).read_bytes(), before)


class MetronLegacyNamingContract(MetronHarness, TestCase):
    def test_existing_templates_without_cv_volume_or_issue(self):
        local = self.add_metron()
        volume = Volume(local)
        keys = get_issue_naming_keys(volume.get_data(), volume.get_issues()[1])
        self.assertEqual(_fill_format('CV[{comicvine_id}] issue[{issue_comicvine_id}]', keys),
                         'CV[] issue[]')
        self.assertNotIn('None', generate_volume_folder_name(volume.get_data()))

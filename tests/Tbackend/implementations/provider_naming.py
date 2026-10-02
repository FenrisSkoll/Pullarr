"""Selected-authority tokens, real rename/import, and bounded query cost."""

from pathlib import Path
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from fixtures.library_import import ImportHarness
from Tbackend.features.library_import_providers import ProviderImportHarness

from backend.base.definitions import SpecialVersion
from backend.features.library_import import import_library
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.naming import (check_format, check_mock_filename,
                                            generate_issue_name,
                                            generate_volume_folder_name,
                                            mass_rename, preview_mass_rename)
from backend.implementations.volumes import Issue, Volume
from backend.internals.provider_identity import (ExternalIdentity,
                                                 MetadataIdentityError,
                                                 ProviderIdentityDB)

FILE = '{series_name} Issue {issue_number} [{metadata_provider}-{provider_id}-{issue_provider_id}]'
FOLDER = '{series_name} ({year}) [{metadata_provider}-{provider_id}]'


class ProviderNaming(ProviderImportHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.settings.volume_folder_naming = FOLDER
        self.settings.file_naming = self.settings.file_naming_empty = FILE
        self.local = self.add_metron()
        self.volume = Volume(self.local)

    def test_metron_add_folder_and_selected_issue(self):
        self.assertEqual(Path(self.volume.get_data().folder).name, 'Example Collection (2020) [metron-700]')
        self.assertEqual(generate_issue_name(self.volume.get_data(), 1.0),
                         'Example Collection Issue 001 [metron-700-701]')
        self.assertEqual(generate_issue_name(self.volume.get_data(), 2.0),
                         'Example Collection Issue 002 [metron-700-702]')

    def test_cv_gcd_references_do_not_replace_authority(self):
        ProviderIdentityDB.put_comicvine_reference(
            ExternalIdentity(self.local, 'comicvine', '999', 'test'), True)
        self.settings.file_naming += ' CV[{comicvine_id}-{issue_comicvine_id}]'
        self.assertEqual(generate_issue_name(self.volume.get_data(), 1.0),
                         'Example Collection Issue 001 [metron-700-701] CV[999-901]')
        self.assertEqual(ProviderIdentityDB.selected_provider(self.local), 'metron')

    def test_missing_volume_identity_fails_without_cv_fallback(self):
        self.db.execute("DELETE FROM volume_external_ids WHERE provider='metron'")
        with self.assertRaises(MetadataIdentityError):
            generate_volume_folder_name(self.volume.get_data())

    def test_missing_issue_identity_fails_despite_cv_reference(self):
        self.db.execute("DELETE FROM issue_external_ids WHERE provider='metron' AND provider_id='701'")
        with self.assertRaises(MetadataIdentityError):
            generate_issue_name(self.volume.get_data(), 1.0)

    def test_unknown_selected_provider_never_falls_back(self):
        self.db.execute("UPDATE volumes SET metadata_provider='unknown' WHERE id=?", (self.local,))
        with self.assertRaises(KeyError):
            generate_volume_folder_name(self.volume.get_data())

    def test_old_templates_do_not_read_identity_or_rename_files(self):
        path = self.metron_file()
        self.settings.volume_folder_naming = '{series_name}'
        self.settings.file_naming = self.settings.file_naming_empty = 'Issue {issue_number}'
        with patch.object(ProviderIdentityDB, 'naming_identities', side_effect=AssertionError('Extra read')):
            self.assertEqual(generate_issue_name(self.volume.get_data(), 2.0), 'Issue 002')
        self.assertTrue(Path(path).exists())

    def test_multi_issue_file_preview_execution_and_special_version(self):
        path = self.metron_file()
        import_library([self.mapping(path)])
        file_id = self.db.execute('SELECT id FROM files').fetchone()[0]
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES (?,?)',
                        (file_id, self.volume.get_issues()[1].id))
        planned, _ = preview_mass_rename(self.local)
        self.assertEqual(Path(next(iter(planned.values()))).name,
                         'Example Collection Issue 001 - 002 [metron-700-].cbz')
        self.assertEqual(mass_rename(self.local, process_individual_files=False), list(planned.values()))
        self.volume.update({'special_version': SpecialVersion.TPB})
        self.settings.file_naming_special_version = '{series_name} {special_version} [{metadata_provider}-{provider_id}]'
        planned, _ = preview_mass_rename(self.local)
        self.assertEqual(Path(next(iter(planned.values()))).name, 'Example Collection TPB [metron-700].cbz')
        self.assertEqual(mass_rename(self.local, process_individual_files=False), list(planned.values()))

    def test_collected_coverage_does_not_change_direct_naming_or_file_api(self):
        from hashlib import sha256

        from backend.base.content_claims import ClaimKind, PublicationRef
        from backend.internals.content_claims import (apply_coverage,
                                                      claim_preview,
                                                      confirm_claim,
                                                      coverage_preview)
        path = self.metron_file()
        import_library([self.mapping(path)])
        file_id, filepath = self.db.execute('SELECT id,filepath FROM files').fetchone()
        before_hash = sha256(Path(filepath).read_bytes()).hexdigest()
        before = preview_mass_rename(self.local)
        direct = self.db.execute('SELECT * FROM issues_files').fetchall()
        target_id = self.db.execute('SELECT issue_id FROM issues_files WHERE file_id=?', (file_id,)).fetchone()[0]
        source = PublicationRef('metron', '702')
        cursor = self.db.cursor()
        preview = claim_preview(cursor, target_id, source, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(cursor, target_id, source, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(cursor, target_id, file_id, [claim])
        apply_coverage(cursor, target_id, file_id, [claim], preview['preview_token'])
        self.assertEqual(preview_mass_rename(self.local), before)
        self.assertEqual(self.db.execute('SELECT * FROM issues_files').fetchall(), direct)
        self.assertEqual(sha256(Path(filepath).read_bytes()).hexdigest(), before_hash)
        source_id = self.db.execute("SELECT issue_id FROM issue_external_ids WHERE provider='metron' AND provider_id='702'").fetchone()[0]
        self.assertEqual(Issue(source_id).get_files(), [])
        from backend.features.direct_downloads import load_target
        with patch('backend.features.direct_downloads.get_db', side_effect=self.db.cursor), \
                patch('backend.internals.identification.get_db', side_effect=self.db.cursor):
            wanted = load_target(self.local, source_id)
        self.assertTrue(next(issue for issue in wanted.catalog if issue.id == source_id).owned)

    def test_opaque_fake_provider_and_identity_sanitization(self):
        self.db.execute("UPDATE volumes SET metadata_provider='fixture' WHERE id=?", (self.local,))
        ProviderIdentityDB.put_volume_identity(ExternalIdentity(self.local, 'fixture', 'opaque:ABC-001', 'test'))
        for issue in self.volume.get_issues():
            ProviderIdentityDB.put_issue_identity(ExternalIdentity(issue.id, 'fixture', 'part/\\%d' % issue.id, 'test'))
        self.settings.replace_illegal_characters = False
        with patch.dict(PROVIDERS, {'fixture': object}):
            name = generate_issue_name(self.volume.get_data(), 1.0)
        self.assertEqual(name, 'Example Collection Issue 001 [fixture-opaqueABC-001-part1]')
        self.assertEqual(Path(name).name, name)

    def test_string_formatting_and_integer_specs_rejected(self):
        self.assertTrue(check_format('{provider_id:>12s}', 'volume_folder_naming'))
        for key in ('provider_id', 'issue_provider_id', 'metadata_provider'):
            self.assertFalse(check_format('{' + key + ':05d}', 'file_naming'))
        self.settings.file_naming = '{provider_id:05d}'
        with self.assertRaises(ValueError):
            generate_issue_name(self.volume.get_data(), 1.0)

    def test_special_versions_only_volume_tokens(self):
        self.settings.file_naming_special_version = '{metadata_provider}-{provider_id} {special_version}'
        vd = self.volume.get_data()
        for special in (SpecialVersion.TPB, SpecialVersion.ONE_SHOT,
                        SpecialVersion.HARD_COVER, SpecialVersion.OMNIBUS):
            vd.special_version = special
            self.assertTrue(generate_issue_name(vd, None).startswith('metron-700 '))
        for setting in ('file_naming_special_version', 'volume_folder_naming'):
            self.assertFalse(check_format('{issue_provider_id}', setting))

    def test_multi_issue_and_vai_range_empty_identity(self):
        vd = self.volume.get_data()
        self.assertEqual(generate_issue_name(vd, (1.0, 2.0)),
                         'Example Collection Issue 001 - 002 [metron-700-]')
        vd.special_version = SpecialVersion.VOLUME_AS_ISSUE
        self.settings.file_naming_vai = 'Volume {issue_number} [{issue_provider_id}]'
        self.assertEqual(generate_issue_name(vd, 1.0), 'Volume 001 [701]')
        self.assertEqual(generate_issue_name(vd, (1.0, 2.0)), 'Volume 001 - 002 []')

    def test_preview_execution_collision_and_file_bindings(self):
        path = self.metron_file()
        import_library([self.mapping(path)])
        current = self.bindings()[0][0]
        self.settings.file_naming += ' changed'
        target = Path(current).with_name('Example Collection Issue 001 [metron-700-701] changed.cbz')
        target.write_bytes(b'occupied')
        planned, _ = preview_mass_rename(self.local)
        self.assertTrue(Path(current).exists())
        result = mass_rename(self.local, process_individual_files=False)
        self.assertEqual(result, list(planned.values()))
        self.assertIn('changed (1).cbz', result[0])
        self.assertEqual(self.bindings()[0][0], result[0])
        self.assertEqual(target.read_bytes(), b'occupied')

    def test_import_and_rename_uses_selected_tokens(self):
        path = self.metron_file(number=2)
        import_library([self.mapping(path)], rename_files=True)
        self.assertEqual(Path(self.bindings()[0][0]).name,
                         'Example Collection Issue 002 [metron-700-702].cbz')
        self.session.get.assert_not_called()

    def test_mock_settings_validation_needs_no_identity_database(self):
        with patch.object(ProviderIdentityDB, 'naming_identities', side_effect=AssertionError('No DB')):
            check_mock_filename(FOLDER, FILE, FILE,
                                '{series_name} ({year}) {special_version} [{metadata_provider}-{provider_id}]',
                                '{series_name} ({year}) Volume {issue_number} [{issue_provider_id}]')


class ComicVineProviderNaming(ImportHarness, TestCase):
    def test_cv_selected_identity_and_legacy_numeric_format(self):
        local = self.add_volume()
        self.settings.file_naming = FILE + ' CV[{comicvine_id:06d}-{issue_comicvine_id:05d}]'
        self.assertEqual(generate_issue_name(Volume(local).get_data(), 1.0),
                         'Example Hero Issue 001 [comicvine-2127-301] CV[002127-00301]')

    def test_cv_shadow_mismatch_fails_without_repair(self):
        local = self.add_volume()
        self.db.execute("UPDATE volume_external_ids SET provider_id='wrong' WHERE volume_id=?", (local,))
        self.settings.volume_folder_naming = FOLDER
        with self.assertRaises(MetadataIdentityError):
            generate_volume_folder_name(Volume(local).get_data())

    def test_cv_issue_shadow_mismatch_fails(self):
        local = self.add_volume()
        self.db.execute("UPDATE issue_external_ids SET provider_id='wrong' WHERE issue_id=1")
        self.settings.file_naming = FILE
        with self.assertRaises(MetadataIdentityError):
            generate_issue_name(Volume(local).get_data(), 1.0)

    def test_thousand_issue_preview_adds_only_two_selects(self):
        local = self.add_volume()
        folder = Path(Volume(local).get_data().folder)
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(1, 1001):
            if index > 2:
                self.db.execute('''INSERT INTO issues(volume_id,comicvine_id,issue_number,
                    calculated_issue_number,title,monitored) VALUES (?,?,?,?,?,1)''',
                                (local, 10000 + index, str(index), index, 'Example'))
            issue_id = self.db.execute('SELECT id FROM issues WHERE volume_id=? AND calculated_issue_number=?',
                                       (local, index)).fetchone()[0]
            path = folder / ('original-%d.cbz' % index)
            path.write_bytes(b'comic')
            file_id = self.db.execute('INSERT INTO files(filepath,size) VALUES (?,5)', (str(path),)).lastrowid
            self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES (?,?)', (file_id, issue_id))
        counts = []
        for template in ('Issue {issue_number}', 'Issue {issue_number} [{metadata_provider}-{provider_id}-{issue_provider_id}]'):
            self.settings.file_naming = self.settings.file_naming_empty = template
            Issue.from_volume_and_calc_number.cache_clear()
            statements = []
            self.db.set_trace_callback(statements.append)
            start = perf_counter()
            planned, _ = preview_mass_rename(local)
            elapsed = perf_counter() - start
            self.db.set_trace_callback(None)
            self.assertEqual(len(planned), 1000)
            counts.append(sum(s.lstrip().upper().startswith('SELECT') for s in statements))
            print('1000-issue preview: %d SELECTs, %.3fs' % (counts[-1], elapsed))
        self.assertEqual(counts[1] - counts[0], 2)


class NewImportProviderNaming(ProviderImportHarness, TestCase):
    def test_new_mixed_import_and_rename_with_new_tokens(self):
        self.settings.volume_folder_naming = FOLDER
        self.settings.file_naming = self.settings.file_naming_empty = FILE
        cv = self.comic_file()
        metron = self.metron_file(number=2)
        import_library([self.mapping(cv, 'comicvine', '2127'), self.mapping(metron)], rename_files=True)
        names = [Path(row[0]).name for row in self.bindings()]
        self.assertEqual(names, ['Example Hero Issue 001 [comicvine-2127-301].cbz',
                                 'Example Collection Issue 002 [metron-700-702].cbz'])
        self.assertEqual(ProviderIdentityDB.audit(PROVIDERS.keys()), [])

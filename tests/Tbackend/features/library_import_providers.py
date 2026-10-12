"""Provider-qualified imports using real add, files, mappings and Metron adapter."""

from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from zipfile import ZipFile

from fixtures.comicvine_fetch import issue_response
from fixtures.comicvine_search import volume_response
from fixtures.library_import import ImportHarness
from Tbackend.implementations.metron_lifecycle import MetronHarness

from backend.base.custom_exceptions import InvalidKeyValue
from backend.features.import_draft import apply_draft, stage_import
from backend.features.library_import import (import_library,
                                             propose_library_import)
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.identity_enrichment import \
    IdentityEnrichmentConflict
from backend.implementations.metadata.provider import MetadataSearchProvider
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.volumes import Volume
from backend.internals.provider_identity import (ExternalIdentity,
                                                 ProviderIdentityDB)


class ProviderImportHarness(ImportHarness, MetronHarness):
    def metron_file(self, folder='metron-incoming', number=1):
        return self.comic_file(folder, 'Example Collection v2 #%d (2020).cbz' % number)

    def mapping(self, path, provider='metron', identity='700'):
        return {'filepath': path, 'provider': provider, 'provider_id': identity}


class ProviderImport(ProviderImportHarness, TestCase):
    def test_reviewed_metron_import_uses_exact_selected_issue(self):
        from Tbackend.features.local_organization import \
            RegistrationBoundaryTests
        database = RegistrationBoundaryTests.file_database(self)
        source = Path(self.metron_file())
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'disposable provider page')
        payload = source.read_bytes()
        stage = stage_import(database, [dict(filepath=str(source), provider='metron',
            provider_id='700', issue_ids=['701'], issue_override=True)], False)
        self.assertEqual(stage['plans'][0]['status'], 'ready', stage)
        applied = apply_draft(database, stage['id'], stage['revision'])
        receipt = applied['jobs'][0]
        self.assertEqual(receipt['state'], 'completed', applied)
        self.assertEqual(Path(receipt['target']).read_bytes(), payload)
        self.assertFalse(source.exists())
        self.assertEqual(ProviderIdentityDB.selected_provider(receipt['volume_id']), 'metron')
        self.assertEqual(self.db.execute('''SELECT e.provider_id FROM issues_files b
            JOIN issue_external_ids e ON e.issue_id=b.issue_id AND e.provider='metron'
            WHERE b.file_id=?''', (receipt['file_id'],)).fetchall(), [('701',)])

    def test_provider_qualified_legacy_mutation_requires_review(self):
        cv = self.comic_file('cv-incoming')
        metron = self.metron_file()
        for payload in ([self.mapping(cv, 'comicvine')],
                        [self.mapping(metron)],
                        [self.mapping(cv, 'comicvine'), self.mapping(metron)]):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                    ValueError, 'legacy_import_requires_review'):
                import_library(payload)
        self.assertEqual(self.rows('volumes'), [])
        self.assertTrue(Path(cv).is_file())
        self.assertTrue(Path(metron).is_file())
        self.session.get.assert_not_called()
        self.http.get.assert_not_called()

    def test_manual_only_proposal_scan_needs_no_comicvine_credentials(self):
        path = self.metron_file()
        with patch('backend.features.library_import.ComicVine') as cv:
            rows = propose_library_import(auto_match=False)
        cv.assert_not_called()
        self.assertEqual(rows[0]['filepath'], path)
        self.assertIsNone(rows[0]['metadata_source'])
        self.assertIsNone(rows[0]['cv']['id'])

    def test_legacy_mutation_does_not_parse_or_fetch_invalid_provider_identity(self):
        path = self.metron_file()
        for payload in ({'filepath': path, 'provider_id': '700'},
                        {'filepath': path, 'provider': 'metron'},
                        {'filepath': path, 'provider': 'metron', 'provider_id': 700},
                        dict(self.mapping(path), id=700),
                        self.mapping(path, 'unregistered_fixture')):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                    ValueError, 'legacy_import_requires_review'):
                import_library([payload])
        self.http.get.assert_not_called()
        self.assertEqual(self.rows('volumes'), [])

    def test_legal_duplicate_cv_selected_ids_keep_first_local_lookup(self):
        first = self.add_volume()
        self.db.execute("INSERT INTO volumes(comicvine_id,title,root_folder) VALUES (2127,'Duplicate',1)")
        self.db.commit()
        self.assertEqual(ProviderIdentityDB.find_selected_volume('comicvine', '2127'), first)
        self.assertEqual(len(self.rows('volumes')), 2)

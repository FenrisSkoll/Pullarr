"""Split selected publications adopt canonical destinations, never discovery folders."""
from pathlib import Path
from shutil import copyfile
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from fixtures.comicvine_fetch import envelope, issue_response
from fixtures.comicvine_search import volume_response
from fixtures.library_import import ImportHarness
from Tbackend.features import local_organization as old_fixture

from backend.features.import_draft import _drafts, apply_draft, stage_import
from backend.features.import_issue_catalog import _cache, issue_catalog
from backend.features.library_import import scan_library_import
from backend.features.local_organization import (_sessions, apply_preview,
                                                 import_preview)
from backend.implementations.volumes import Volume


class Import152Tests(ImportHarness,TestCase):
    file_database=old_fixture.RegistrationBoundaryTests.file_database
    def setUp(self):
        super().setUp()
        self.addCleanup(_sessions.clear)
        self.addCleanup(_cache.clear)
        self.addCleanup(_drafts.clear)
        self.database=self.file_database()

    def split(self,rename=False,cleanup=False,extra=False,root_source=False,collision=False,fetch_failure=False,unrelated=False):
        self.settings.create_empty_volume_folders=False
        self.db.execute("INSERT OR REPLACE INTO config VALUES('delete_empty_folders',?)",(str(int(cleanup)),))
        self.db.commit()
        if unrelated:
            other=self.root/'Unrelated';other.mkdir()
            self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(99,99999,'Unrelated',1,?)",(str(other),))
            self.db.commit()
        folder=self.root if root_source else self.root/'American Gods (2018)'
        if not root_source:folder.mkdir()
        if extra:(folder/'.keep').write_text('unrelated content')
        names=['Shadows','My Ainsel','The Moment of the Storm']
        responses=[];matches=[];sources=[]
        for n,name in enumerate(names,1):
            pid=str(110000+n)
            responses.extend([envelope(volume_response(id=pid,name='American Gods: '+name,start_year='2018',count_of_issues='1',aliases='',deck='')),
                envelope([issue_response(id=str(210000+n),volume={'id':pid},issue_number=str(n),name=name,cover_date='2018-01-01')],number_of_total_results=1)])
            source=folder/f'American Gods (2018) - {n:03} - {name}.cbz'
            with ZipFile(source,'w') as archive:archive.writestr('page.jpg',b'synthetic unchanged pages')
            sources.append(source)
            matches.append(dict(filepath=str(source),provider='comicvine',provider_id=pid))
        if fetch_failure:responses[2:4]=[RuntimeError('synthetic metadata unavailable')]
        self.response.json.side_effect=responses
        value=import_preview(self.database,matches,rename)
        if fetch_failure:
            self.assertEqual(sum(p['status']=='ready' for p in value['plans']),2,value)
            applied=apply_preview(self.database,value['id'])
            self.assertEqual(sum(j['state']=='completed' for j in applied['jobs']),2,applied)
            self.assertEqual(len(applied['review']),1)
            self.assertTrue(sources[1].is_file())
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0],2)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM volumes WHERE folder='' OR folder IS NULL").fetchone()[0],0)
            self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])
            return
        self.assertEqual([p['status'] for p in value['plans']],['ready']*3,value)
        volumes=self.db.execute('SELECT id,folder,custom_folder FROM volumes WHERE id<>99 ORDER BY id').fetchall()
        self.assertEqual(len(volumes),3)
        self.assertEqual(len({v[1] for v in volumes}),3)
        self.assertTrue(all(v[1] and Path(v[1]).is_dir() and not v[2] for v in volumes))
        before=[p.read_bytes() for p in sources]
        if collision:Path(value['plans'][1]['target']).write_bytes(b'unrelated destination')
        applied=apply_preview(self.database,value['id'])
        if collision:
            self.assertEqual(sum(j['state']=='completed' for j in applied['jobs']),2,applied)
            self.assertEqual(sources[1].read_bytes(),before[1])
            self.assertEqual(Path(value['plans'][1]['target']).read_bytes(),b'unrelated destination')
            self.assertTrue(folder.is_dir())
            return
        self.assertEqual([j['state'] for j in applied['jobs']],['completed']*3,applied)
        for original,plan,payload in zip(sorted(sources),value['plans'],sorted(zip(sources,before))):
            target=Path(plan['target'])
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(),payload[1])
            self.assertFalse(original.exists())
            if not rename:self.assertEqual(target.name,original.name)
            self.assertEqual(Volume(plan['volume_id']).get_public_data()['folder'],str(target.parent))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0],3)
        apply_preview(self.database,value['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0],3+int(unrelated))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0],3)
        self.assertEqual(folder.exists(),not cleanup or extra or root_source)
        self.scan.assert_not_called();self.process.assert_not_called()

    def test_split_import_preserves_filenames(self):self.split()
    def test_split_import_and_rename(self):self.split(rename=True)
    def test_cleanup_enabled(self):self.split(cleanup=True)
    def test_cleanup_preserves_hidden_content(self):self.split(cleanup=True,extra=True)
    def test_cleanup_never_deletes_root(self):self.split(cleanup=True,root_source=True)
    def test_target_collision_preserves_source_and_other_groups_complete(self):self.split(cleanup=True,collision=True)
    def test_one_metadata_failure_does_not_discard_other_publications(self):self.split(cleanup=True,fetch_failure=True)
    def test_other_managed_folder_does_not_stale_import_fingerprint(self):self.split(unrelated=True)

    def test_reviewed_volume_number_moves_file_and_links_exact_issue(self):
        folder = self.root/'Synthetics'; folder.mkdir()
        source = folder/'Synthetics v01.cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'synthetic page bytes')
        original = source.read_bytes()
        responses = [envelope(volume_response(id='2222', name='Synthetics',
            start_year='2026', count_of_issues='1', aliases='', deck='')),
            envelope([issue_response(id='3333', volume={'id':'2222'},
                issue_number='1', name='Book 1', cover_date='2026-01-01')],
                number_of_total_results=1)]
        self.response.json.side_effect = responses * 2
        catalogue = issue_catalog(self.database, 'comicvine', '2222', [str(source)])
        self.assertEqual(catalogue['issues'][0]['id'], '3333')
        self.assertEqual(catalogue['loaded_issue_count'], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)
        self.assertFalse((self.root/'Synthetics (2026)').exists())
        preview = import_preview(self.database, [dict(filepath=str(source),
            provider='comicvine', provider_id='2222', issue_ids=['3333'],
            issue_override=True)], False)
        self.assertEqual(preview['plans'][0]['status'], 'ready', preview)
        outcome = apply_preview(self.database, preview['id'])
        self.assertEqual(outcome['jobs'][0]['state'], 'completed', outcome)
        target = Path(outcome['jobs'][0]['target'])
        self.assertEqual(target.name, source.name)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(source.exists())
        self.assertEqual(self.db.execute('''SELECT f.filepath,i.comicvine_id FROM files f
            JOIN issues_files b ON b.file_id=f.id JOIN issues i ON i.id=b.issue_id''').fetchall(),
            [(str(target), 3333)])

    def test_file_discovery_pages_without_provider(self):
        for number in range(430):
            folder = self.root/f'Folder {number:03}'; folder.mkdir()
            (folder/f'Book {number:03}.cbz').write_bytes(b'fixture')
        first = scan_library_import(self.database, page_size=100, folder_limit=500)
        pages = [first]
        while pages[-1]['next_offset'] is not None:
            pages.append(scan_library_import(self.database, offset=pages[-1]['next_offset'],
                                             page_size=100, folder_limit=500))
        self.assertEqual(first['counts']['discovered'], 430)
        self.assertEqual(sum(len(page['items']) for page in pages), 430)
        self.assertTrue(first['enumeration_complete'])
        self.assertIsNone(pages[-1]['next_offset'])
        self.response.json.assert_not_called()

    def test_batman_basename_glob_includes_nested_files_only(self):
        chosen = self.root/'Batman - Deluxe Edition (2019)'
        nested = chosen/'Book 1'; nested.mkdir(parents=True)
        (nested/'Batman Book 1.cbz').write_bytes(b'synthetic')
        other = self.root/'Synthetics'; other.mkdir()
        (other/'Synthetics v01.cbr').write_bytes(b'synthetic')
        result = scan_library_import(self.database, folder_filter='*Batman*')
        self.assertEqual([Path(row['filepath']).name for row in result['items']],
                         ['Batman Book 1.cbz'])
        self.assertTrue(result['enumeration_complete'])

    def test_read_only_stage_then_journalled_apply(self):
        self._staged_synthetics(False)

    def test_read_only_stage_and_rename(self):
        self._staged_synthetics(True)

    def _staged_synthetics(self, rename):
        self.settings.volume_folder_naming = '{series_name} ({year})'
        self.db.execute("UPDATE config SET value=? WHERE key='volume_folder_naming'",
                        ('{series_name} ({year})',))
        self.db.commit()
        folder = self.root/'Synthetics'; folder.mkdir()
        source = folder/'Synthetics v01.cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'synthetic page bytes')
        original = source.read_bytes()
        responses = [envelope(volume_response(id='2222', name='Synthetics',
            start_year='2026', count_of_issues='1', aliases='', deck='')),
            envelope([issue_response(id='3333', volume={'id':'2222'},
                issue_number='1', name='Book 1', cover_date='2026-01-01')],
                number_of_total_results=1)]
        self.response.json.side_effect = responses * 2
        match = dict(filepath=str(source), provider='comicvine', provider_id='2222',
                     issue_ids=['3333'], issue_override=True)
        stage = stage_import(self.database, [match], rename)
        self.assertEqual(stage['plans'][0]['status'], 'ready', stage)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)
        self.assertFalse(Path(stage['plans'][0]['target']).exists(), stage)
        result = apply_draft(self.database, stage['id'])
        self.assertTrue(result['jobs'], (stage, result))
        self.assertEqual(result['jobs'][0]['state'], 'completed', result)
        self.assertEqual(Path(result['jobs'][0]['target']).read_bytes(), original)
        if not rename:
            self.assertEqual(Path(result['jobs'][0]['target']).name, source.name)
        self.assertFalse(source.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 1)

    def test_four_book_http_stage_apply_uses_real_executor_and_db(self):
        from flask import Flask

        from frontend.api import api

        self.db.execute("UPDATE config SET value=? WHERE key='volume_folder_naming'",
                        (self.settings.volume_folder_naming,))
        self.db.commit()

        source_folder = self.root/'Incoming Batman'; source_folder.mkdir()
        names = [f'Detective Comics Rebirth Deluxe Book {n}.cbz' for n in range(1, 5)]
        sources = []
        for name in names:
            path = source_folder/name
            with ZipFile(path, 'w') as archive:
                archive.writestr('page.jpg', name.encode())
            sources.append(path)
        issue_rows = [issue_response(id=str(3100+n), volume={'id':'2200'},
                      issue_number=str(n), name=f'Book {n}', cover_date='2017-01-01')
                      for n in range(1, 5)]
        responses = [envelope(volume_response(id='2200', name='Detective Comics Rebirth Deluxe',
            start_year='2017', count_of_issues='4', aliases='', deck='')),
            envelope(issue_rows, number_of_total_results=4)]
        self.response.json.side_effect = responses * 2
        app = Flask(__name__); app.register_blueprint(api, url_prefix='/api')
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.internals.db.DBConnection.default_file', self.database):
            settings.return_value.sv.api_key = 'fixture-key'
            client = app.test_client()
            params = {'api_key':'fixture-key'}
            catalog = client.post('/api/libraryimport/catalog', query_string=params,
                json=dict(provider='comicvine', provider_id='2200', files=[str(p) for p in sources]))
            self.assertEqual(catalog.status_code, 200, catalog.json)
            suggestions = catalog.json['result']['proposals']
            self.assertEqual([suggestions[str(p)]['confidence'] for p in sources], ['strong'] * 4)
            matches = [dict(filepath=str(path), provider='comicvine', provider_id='2200',
                            issue_ids=[str(3100+n)], catalog_revision=catalog.json['result']['revision'])
                       for n, path in enumerate(sources, 1)]
            staged = client.post('/api/libraryimport/stage', query_string=params, json=matches)
            self.assertEqual(staged.status_code, 200, staged.json)
            self.assertEqual(staged.json['result']['counts']['ready'], 4, staged.json)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)
            applied = client.post('/api/local-organization/' + staged.json['result']['id'] + '/apply',
                                  query_string=params)
            self.assertEqual(applied.status_code, 200, applied.json)
            self.assertEqual([j['state'] for j in applied.json['result']['jobs']], ['completed'] * 4,
                             (staged.json, applied.json))
        rows = self.db.execute('''SELECT f.filepath,i.comicvine_id FROM files f
            JOIN issues_files b ON b.file_id=f.id JOIN issues i ON i.id=b.issue_id
            ORDER BY i.comicvine_id''').fetchall()
        self.assertEqual([row[1] for row in rows], [3101, 3102, 3103, 3104])
        self.assertTrue(all(Path(row[0]).is_file() for row in rows))
        self.assertTrue(all(not path.exists() for path in sources))

    def test_synthetic_cbr_reaches_canonical_folder(self):
        self.settings.volume_folder_naming = '{series_name} ({year})'
        self.db.execute("UPDATE config SET value=? WHERE key='volume_folder_naming'",
                        ('{series_name} ({year})',)); self.db.commit()
        folder = self.root/'Synthetics'; folder.mkdir()
        source = folder/'Synthetics v01.cbr'
        copyfile(Path(__file__).parent/'fixtures'/'archives'/'synthetic-25.cbr', source)
        original = source.read_bytes()
        responses = [envelope(volume_response(id='2222', name='Synthetics',
            start_year='2026', count_of_issues='1', aliases='', deck='')),
            envelope([issue_response(id='3333', volume={'id':'2222'}, issue_number='1',
                name='Book 1', cover_date='2026-01-01')], number_of_total_results=1)]
        self.response.json.side_effect = responses * 2
        draft = stage_import(self.database, [dict(filepath=str(source), provider='comicvine',
            provider_id='2222', issue_ids=['3333'], issue_override=True)], False)
        self.assertEqual(draft['plans'][0]['status'], 'ready', draft)
        result = apply_draft(self.database, draft['id'])
        self.assertEqual(result['jobs'][0]['state'], 'completed', result)
        target = Path(result['jobs'][0]['target'])
        self.assertEqual(target.parent.name, 'Synthetics (2026)')
        self.assertEqual(target.name, source.name)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(source.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 1)

    def test_stale_draft_never_registers_or_moves(self):
        source = Path(self.comic_file(name='Example Hero v2 #1 (2021).cbz'))
        match = dict(filepath=str(source), provider='comicvine', provider_id='2127',
                     issue_ids=['301'])
        stage = stage_import(self.database, [match], False)
        self.assertEqual(stage['plans'][0]['status'], 'ready', stage)
        source.write_bytes(source.read_bytes() + b'changed after preview')
        with self.assertRaisesRegex(ValueError, 'stale_import_source'):
            apply_draft(self.database, stage['id'])
        self.assertTrue(source.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_review_only_draft_can_be_retried_without_stuck_claim(self):
        source = Path(self.comic_file(name='Example Hero v2 #1 (2021).cbz'))
        stage = stage_import(self.database, [dict(filepath=str(source),
            provider='comicvine', provider_id='2127', issue_ids=[])], False)
        self.assertEqual(stage['counts']['ready'], 0)
        first = apply_draft(self.database, stage['id'])
        second = apply_draft(self.database, stage['id'])
        self.assertEqual(first['jobs'], [])
        self.assertEqual(second['jobs'], [])
        self.assertTrue(source.exists())

    def test_occupied_draft_destination_never_registers(self):
        source = Path(self.comic_file(name='Example Hero v2 #1 (2021).cbz'))
        match = dict(filepath=str(source), provider='comicvine', provider_id='2127',
                     issue_ids=['301'])
        stage = stage_import(self.database, [match], False)
        target = Path(stage['plans'][0]['target'])
        if target == source:
            self.skipTest('Fixture naming retains source; collision needs a move')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'unrelated')
        with self.assertRaisesRegex(ValueError, 'occupied_import_target'):
            apply_draft(self.database, stage['id'])
        self.assertTrue(source.exists())
        self.assertEqual(target.read_bytes(), b'unrelated')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_embedded_exact_issue_identity_outweighs_filename_number(self):
        folder = self.root/'Synthetics'; folder.mkdir()
        source = folder/'Synthetics #1.cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'page')
            archive.writestr('ComicInfo.xml', b'<ComicInfo><Series>Synthetics</Series>'
                b'<Number>1</Number><Web>https://comicvine.gamespot.com/issue/4000-3333/</Web></ComicInfo>')
        responses = [envelope(volume_response(id='2222', name='Synthetics',
            start_year='2026', count_of_issues='2', aliases='', deck='')),
            envelope([issue_response(id='3332', volume={'id':'2222'}, issue_number='1',
                      name='Book 1', cover_date='2026-01-01'),
                      issue_response(id='3333', volume={'id':'2222'}, issue_number='2',
                      name='Book 2', cover_date='2026-02-01')], number_of_total_results=2)]
        self.response.json.side_effect = responses
        result = issue_catalog(self.database, 'comicvine', '2222', [str(source)])
        proposal = result['proposals'][str(source)]
        self.assertEqual(proposal['issue_ids'], ['3333'])
        self.assertEqual(proposal['confidence'], 'review_needed')
        self.assertIn('conflicts', proposal['reason'])
        compatible = folder/'Synthetics #2.cbz'
        with ZipFile(compatible, 'w') as archive:
            archive.writestr('page.jpg', b'page 2')
            archive.writestr('ComicInfo.xml', b'<ComicInfo><Series>Synthetics</Series>'
                b'<Number>2</Number><Web>https://comicvine.gamespot.com/issue/4000-3333/</Web></ComicInfo>')
        exact = issue_catalog(self.database, 'comicvine', '2222', [str(compatible)])
        self.assertEqual(exact['proposals'][str(compatible)]['confidence'], 'exact')

    def test_provider_failure_keeps_file_visible_for_later_review(self):
        source = Path(self.comic_file(name='Synthetics v01.cbz'))
        self.response.json.side_effect = RuntimeError('synthetic provider outage')
        discovered = scan_library_import(self.database)
        self.assertIn(str(source), [row['filepath'] for row in discovered['items']])
        stage = stage_import(self.database, [dict(filepath=str(source),
            provider='comicvine', provider_id='2222', issue_ids=['3333'])], False)
        self.assertEqual(stage['plans'][0]['identification_reasons'], ['catalog_unavailable'])
        self.assertTrue(source.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_scan_shows_bounded_embedded_evidence_without_registration(self):
        folder = self.root/'Synthetics'; folder.mkdir()
        source = folder/'Synthetics Book 1.cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'synthetic page')
            archive.writestr('ComicInfo.xml', b'<ComicInfo><Series>Synthetics</Series>'
                b'<Number>Book 1</Number><Title>First Book</Title>'
                b'<Web>https://comicvine.gamespot.com/volume/4050-2222/</Web></ComicInfo>')
        result = scan_library_import(self.database)
        row = next(item for item in result['items'] if item['filepath'] == str(source))
        self.assertEqual(row['embedded']['number'], 'Book 1')
        self.assertEqual(row['embedded']['title'], 'First Book')
        self.assertEqual(row['metadata_source'], {'provider':'comicvine', 'id':'2222'})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_staged_issue_cannot_contradict_embedded_exact_issue(self):
        folder = self.root/'Synthetics'; folder.mkdir()
        source = folder/'Synthetics #2.cbz'
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'synthetic page')
            archive.writestr('ComicInfo.xml', b'<ComicInfo><Series>Synthetics</Series>'
                b'<Number>2</Number><Web>https://comicvine.gamespot.com/issue/4000-3333/</Web></ComicInfo>')
        self.response.json.side_effect = [envelope(volume_response(id='2222', name='Synthetics',
            start_year='2026', count_of_issues='2', aliases='', deck='')),
            envelope([issue_response(id='3332', volume={'id':'2222'}, issue_number='1',
                      name='Book 1', cover_date='2026-01-01'),
                      issue_response(id='3333', volume={'id':'2222'}, issue_number='2',
                      name='Book 2', cover_date='2026-02-01')], number_of_total_results=2)]
        stage = stage_import(self.database, [dict(filepath=str(source),
            provider='comicvine', provider_id='2222', issue_ids=['3332'],
            issue_override=True)], False)
        self.assertEqual(stage['plans'][0]['status'], 'blocked')
        self.assertIn('embedded_identity_conflict', stage['plans'][0]['identification_reasons'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_two_files_same_issue_require_overlap_review(self):
        sources = [Path(self.comic_file(name=f'Example Hero copy {n}.cbz')) for n in (1, 2)]
        matches = [dict(filepath=str(source), provider='comicvine',
                        provider_id='2127', issue_ids=['301']) for source in sources]
        stage = stage_import(self.database, matches, False)
        self.assertEqual(stage['counts']['review_required'], 2, stage)
        self.assertTrue(all('duplicate_issue_selection' in p['identification_reasons']
                            for p in stage['plans']))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_permission_failure_never_reports_imported_file(self):
        from backend.base.organization_job import (ExecutionCode,
                                                   OrganizationError)

        self.settings.volume_folder_naming = '{series_name} ({year})'
        self.db.execute("UPDATE config SET value=? WHERE key='volume_folder_naming'",
                        ('{series_name} ({year})',)); self.db.commit()
        source = Path(self.comic_file(name='Example Hero v2 #1 (2021).cbz'))
        stage = stage_import(self.database, [dict(filepath=str(source),
            provider='comicvine', provider_id='2127', issue_ids=['301'])], False)
        self.assertEqual(stage['counts']['ready'], 1, stage)
        with patch('backend.features.managed_permissions.apply_managed_permissions',
                   side_effect=OrganizationError(ExecutionCode.PERMISSION)):
            result = apply_draft(self.database, stage['id'])
        self.assertEqual(result['jobs'], [])
        self.assertIn('publication_registration_unavailable',
                      result['review'][0]['identification_reasons'])
        self.assertTrue(source.exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 0)

    def test_file_permission_denial_after_transfer_keeps_issue_unowned(self):
        from backend.base.organization_job import (ExecutionCode,
                                                   OrganizationError)

        self.settings.volume_folder_naming = '{series_name} ({year})'
        self.db.execute("UPDATE config SET value=? WHERE key='volume_folder_naming'",
                        ('{series_name} ({year})',)); self.db.commit()
        source = Path(self.comic_file(name='Example Hero v2 #1 (2021).cbz'))
        with ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'synthetic page')
        payload = source.read_bytes()
        responses = [envelope(volume_response()),
            envelope([issue_response(), issue_response(id=302, issue_number='2')],
                     number_of_total_results=2)]
        self.response.json.side_effect = responses * 2
        stage = stage_import(self.database, [dict(filepath=str(source),
            provider='comicvine', provider_id='2127', issue_ids=['301'])], False)
        def deny_file_permissions(path, *, directory, repair=False):
            if not directory:
                raise OrganizationError(ExecutionCode.PERMISSION)
        with patch('backend.features.managed_permissions.apply_managed_permissions',
                   side_effect=deny_file_permissions):
            result = apply_draft(self.database, stage['id'])
        self.assertTrue(result['jobs'], (stage, result))
        job = result['jobs'][0]
        self.assertEqual(job['state'], 'recovery_required', result)
        self.assertEqual(Path(job['target']).read_bytes(), payload)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 0)

    def test_shared_hardlink_requires_seed_policy_and_keeps_both_entries(self):
        import os

        source = Path(self.comic_file(name='Example Hero v2 #1 (2021).cbz'))
        sibling = self.root/'seed-payload.cbz'
        try:
            os.link(source, sibling)
        except OSError:
            self.skipTest('Hardlinks unavailable on this test filesystem')
        stage = stage_import(self.database, [dict(filepath=str(source),
            provider='comicvine', provider_id='2127', issue_ids=['301'])], False)
        self.assertEqual(stage['plans'][0]['identification_reasons'],
                         ['shared_inode_requires_seed_policy'])
        self.assertEqual(source.read_bytes(), sibling.read_bytes())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

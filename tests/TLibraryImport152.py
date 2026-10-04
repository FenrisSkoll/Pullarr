"""Split selected publications adopt canonical destinations, never discovery folders."""
from pathlib import Path
from unittest import TestCase
from zipfile import ZipFile

from fixtures.comicvine_fetch import envelope, issue_response
from fixtures.comicvine_search import volume_response
from fixtures.library_import import ImportHarness
from Tbackend.features import local_organization as old_fixture

from backend.features.local_organization import (_sessions, apply_preview,
                                                 import_preview)
from backend.implementations.volumes import Volume


class Import152Tests(ImportHarness,TestCase):
    file_database=old_fixture.RegistrationBoundaryTests.file_database
    def setUp(self):
        super().setUp()
        self.addCleanup(_sessions.clear)
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

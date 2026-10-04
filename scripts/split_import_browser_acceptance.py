"""Real import API/Chromium; only provider search/fetch use synthetic metadata."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from zipfile import ZipFile

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO),str(REPO/'scripts'),str(REPO/'tests')]

from api_key_browser_acceptance import QuietHandler
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from backend.features.local_organization import _sessions
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)
from backend.internals.db import commit, get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def run(rename):
    names=['Shadows','My Ainsel','The Moment of the Storm']
    titles=['American Gods: '+name for name in names]
    async def proposals(groups,**kwargs):
        return {key:dict(id=110001,title=titles[0],issue_count=1,link='https://comicvine.gamespot.com/volume/4050-110001/') for key in groups}
    async def fetched(provider, identity, *args, **kwargs):
        pid=identity.provider_id if hasattr(identity,'provider_id') else str(identity)
        n=int(pid)-110000
        return VolumeFetchResult(VolumeMetadata('comicvine',pid,titles[n-1],2018,1,None,None,'<p>Readable description.</p>',
            f'https://comicvine.gamespot.com/volume/4050-{pid}/',[],'Fixture',1,False,
            [IssueMetadata('comicvine',str(210000+n),pid,str(n),float(n),names[n-1],'2018-01-01',None)]),())
    errors,dialogs=[],[]
    with TemporaryDirectory(prefix='pullarr-split-152-') as temporary:
        root=Path(temporary);library=root/'library';source=library/'American Gods (2018)';source.mkdir(parents=True)
        originals={}
        for n,name in enumerate(names,1):
            path=source/f'American Gods (2018) - {n:03} - {name}.cbz'
            with ZipFile(path,'w') as z:z.writestr('page.jpg',f'synthetic {n}'.encode())
            originals[path.name]=path.read_bytes()
        set_db_location(str(root/'db'));server=Server()
        with server.app.app_context():
            setup_db();Settings().update({'api_key':'fixture-fixture','delete_empty_folders':True,'create_empty_volume_folders':False})
            db=get_db();db.execute('UPDATE indexer_clients SET enabled=0');db.execute('INSERT INTO root_folders VALUES(1,?)',(str(library),));commit()
            assert Settings().sv.api_key=='fixture-fixture', 'Synthetic API settings did not persist'
        with patch('backend.features.library_import.ComicVine') as comicvine, patch('backend.implementations.volumes.get_volume_provider',return_value=object()), patch('backend.implementations.volumes.fetch_volume_result',side_effect=fetched):
            comicvine.return_value.filenames_to_cvs=proposals
            http=make_server('127.0.0.1',0,server.app,threaded=True,request_handler=QuietHandler)
            Thread(target=http.serve_forever,daemon=True).start();origin=f'http://127.0.0.1:{http.server_port}'
            try:
                with sync_playwright() as driver:
                    browser=driver.chromium.launch();page=browser.new_page()
                    page.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'fixture-fixture',last_login:Date.now()/1000}));")
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('dialog',lambda d:(dialogs.append(d.type),d.dismiss()))
                    page.on('response',lambda r:errors.append(f'HTTP {r.status}') if r.status>=500 else None)
                    page.route('**/api/volumes/search?*',lambda route:route.fulfill(json={'error':None,'result':[
                        dict(comicvine_id=110000+n,title=t,year=2018,issue_count=1,site_url=f'https://comicvine.gamespot.com/volume/4050-{110000+n}/') for n,t in enumerate(titles,1)]}))
                    page.goto(origin+'/library-import');page.locator('#run-import-button').click()
                    rows=page.locator('.proposal-list tr[data-rowid]');expect(rows).to_have_count(3)
                    # Correct individual rows through the actual search/select UI.
                    for index in (1,2):
                        rows.nth(index).locator('button').click()
                        page.locator('#search-input').fill(names[index]);page.locator('#search-input').press('Enter')
                        result=page.locator('.search-results tr').filter(has_text=titles[index])
                        result.locator('button').click()
                    with page.expect_request(lambda r:'/libraryimport/preview?' in r.url) as request:
                        page.locator('#import-rename-button' if rename else '#import-button').click()
                    payload=request.value.post_data_json
                    assert [r['provider_id'] for r in payload]==['110001','110002','110003'],payload
                    panel=page.locator('#library-import-results')
                    expect(panel).to_contain_text('Imported 3 files into 3 publications.',timeout=60000)
                    for title in titles:expect(panel).to_contain_text(title)
                    expect(rows).to_have_count(0)
                    with server.app.app_context():
                        db=get_db();volumes=db.execute('SELECT id,folder FROM volumes ORDER BY id').fetchall()
                        assert len(volumes)==3 and len({r[1] for r in volumes})==3
                        assert db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0]==3
                        files=db.execute('SELECT filepath FROM files').fetchall()
                        assert len(files)==3 and all(Path(r[0]).is_file() for r in files)
                        assert {Path(r[0]).read_bytes() for r in files}==set(originals.values())
                        if not rename:assert {Path(r[0]).name for r in files}==set(originals)
                        else:assert {Path(r[0]).name for r in files}!=set(originals)
                    assert not source.exists()
                    for vid,folder in volumes:
                        page.goto(origin+f'/volumes/{vid}');expect(page.locator('#loading-screen')).to_be_hidden()
                        expect(page.locator('main')).to_contain_text(folder)
                        expect(page.locator('main')).to_contain_text('Readable description.')
                    response=page.request.get(origin+'/api/volumes?api_key=fixture-fixture&metadata=true')
                    assert response.status==200
                    assert {r['metadata_source']['id'] for r in response.json()['result']}=={'110001','110002','110003'}
                    page.goto(origin+'/')
                    for title in titles:expect(page.locator('main')).to_contain_text(title)
                    assert not errors and not dialogs,(errors,dialogs)
                    browser.close()
            finally:http.shutdown();_sessions.clear()
    print('Split import Chromium PASS: '+('Import and Rename' if rename else 'Import')+'; exact edited identities, three usable volumes/folders, safe moves, cleanup, no dialogs/errors')


if __name__=='__main__':
    if len(sys.argv)>1:
        run(sys.argv[1]=='rename')
    else:
        import subprocess
        for mode in ('import','rename'):
            subprocess.run([sys.executable,__file__,mode],check=True)

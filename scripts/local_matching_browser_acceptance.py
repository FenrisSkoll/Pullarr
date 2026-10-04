"""Disposable v1.5.2 HTTP/Chromium acceptance with synthetic provider metadata."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO/'scripts'), str(REPO/'tests')]

from api_key_browser_acceptance import QuietHandler
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from backend.features.local_organization import _sessions
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.volumes import Library
from backend.internals.db import commit, get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    description = '<p>Trade paperback collecting <a href="javascript:bad()">Black Road</a>.</p><p>Second <strong>paragraph</strong><br>New line.</p><script>bad()</script>'
    cases = [
        ('Black Road: The Holy North', 2016, ['Volume One'], 'Black Road', ['The Holy North'], True),
        ('Army of Darkness Omnibus', 2010, ['Volume 1','Volume 2','Volume 3'], 'Army of Darkness Omnibus', ['Army of Darkness Omnibus Vol. 01','Volume 2','Volume 3'], True),
        ('Blade', 1998, ['Blood Allies: Part 1','Blood Allies: Part 2','Blood Allies: Part 3'], 'Blade', ['Blood Allies - Part 1','Blood Allies - Part 2','Blood Allies - Part 3'], False),
        ('Abe Sapien: Dark and Terrible', 2017, ['Volume 1','Volume 2'], 'Abe Sapien - Dark and Terrible', ['Volume 1','Volume 2'], True)]
    errors, dialogs = [], []
    with TemporaryDirectory(prefix='pullarr-matching-152-') as temporary:
        root=Path(temporary); library=root/'library'; library.mkdir()
        set_db_location(str(root/'db')); server=Server(); managed=[]
        with server.app.app_context():
            setup_db(); Settings().update({'api_key':'fixture-fixture'})
            db=get_db(); db.execute('UPDATE indexer_clients SET enabled=0')
            db.execute('INSERT INTO root_folders VALUES(1,?)',(str(library),));commit()
            for index,(title,year,titles,series,local_titles,collected) in enumerate(cases,1):
                pid=str(300000+index); folder=library/(title.replace(':',' -')+f' ({year})');folder.mkdir()
                metadata=VolumeMetadata('comicvine',pid,title,year,1,None,None,description,
                    f'https://comicvine.gamespot.com/volume/4050-{pid}/',[],'Fixture',len(titles),False,
                    [IssueMetadata('comicvine',str(index*100+n),pid,str(n),float(n),t,f'{year}-01-01',description) for n,t in enumerate(titles,1)])
                with patch('backend.implementations.volumes.get_volume_provider',return_value=object()), patch('backend.implementations.volumes.fetch_volume_result',AsyncMock(return_value=VolumeFetchResult(metadata,()))):
                    vid=Library.add_metadata(ProviderVolumeIdentity('comicvine',pid),1,True,volume_folder=str(folder),organizer_registration=True);commit()
                paths=[]
                for n,local_title in enumerate(local_titles,1):
                    token=f'v{n:03}' if collected else f'{n:03} - {local_title}'
                    path=folder/(title.replace(':',' -')+f' ({year}) - {token}.cbz')
                    with ZipFile(path,'w') as archive:
                        archive.writestr('page.jpg',b'unchanged synthetic payload')
                        number=f'<Volume>{n}</Volume>' if collected else f'<Number>{n}</Number>'
                        archive.writestr('ComicInfo.xml',f'<ComicInfo><Series>{series}</Series><Title>{local_title}</Title>{number}</ComicInfo>')
                    paths.append(path)
                managed.append((vid,paths))
        http=make_server('127.0.0.1',0,server.app,threaded=True,request_handler=QuietHandler)
        Thread(target=http.serve_forever,daemon=True).start();origin=f'http://127.0.0.1:{http.server_port}'
        try:
            with sync_playwright() as driver:
                browser=driver.chromium.launch();page=browser.new_page()
                page.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'fixture-fixture',last_login:Date.now()/1000}));")
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('dialog',lambda d:(dialogs.append(d.type),d.dismiss()))
                page.on('response',lambda r:errors.append(f'HTTP {r.status}') if r.status>=500 else None)
                for vid,paths in managed:
                    before=[p.read_bytes() for p in paths]
                    page.goto(origin+f'/volumes/{vid}');expect(page.locator('#loading-screen')).to_be_hidden()
                    rendered=page.locator('#volume-description')
                    # Both desktop/mobile use the same safe plain-text DTO.
                    assert '<p>' not in page.locator('main').inner_text()
                    assert 'bad()' not in page.locator('main').inner_text()
                    expect(page.locator('main')).to_contain_text('Trade paperback collecting Black Road.')
                    page.locator('#refresh-button').click();preview=page.locator('#volume-local-scan-results')
                    expect(preview).to_contain_text('#1 '+cases[vid-1][2][0])
                    expect(preview.get_by_role('button',name='Review issue match')).to_have_count(0)
                    preview.get_by_role('button',name='Apply ready associations').click()
                    expect(preview).to_contain_text(f'{len(paths)} files imported or associated.')
                    with page.expect_response(lambda r:'/organization-scan?' in r.url) as refreshed:
                        page.locator('#refresh-button').click()
                    refreshed_data=refreshed.value.json()['result']
                    assert all(p['status']=='no_changes' for p in refreshed_data['plans']),refreshed_data
                    expect(preview).to_contain_text('Already associated')
                    expect(preview.get_by_role('button',name='Apply ready associations')).to_be_disabled()
                    assert [p.read_bytes() for p in paths]==before
                # A malformed archive remains one row; another volume still works.
                managed[0][1][0].write_bytes(b'malformed fixture')
                page.goto(origin+f'/volumes/{managed[0][0]}');expect(page.locator('#loading-screen')).to_be_hidden()
                page.locator('#refresh-button').click();expect(page.locator('#volume-local-scan-results')).to_contain_text('Needs review')
                page.goto(origin+f'/volumes/{managed[-1][0]}');expect(page.locator('#loading-screen')).to_be_hidden()
                page.locator('#refresh-button').click();expect(page.locator('#volume-local-scan-results')).to_contain_text('Already associated')
                with server.app.app_context(): assert get_db().execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0]==0
                assert not errors and not dialogs,(errors,dialogs)
                browser.close()
            print('v1.5.2 Chromium PASS: safe provider descriptions; Black Road, Army of Darkness, Blade, Abe Sapien; DB-only Apply/fresh no-op; isolated malformed file; zero jobs/dialogs/HTTP500/JS errors')
        finally:http.shutdown();_sessions.clear()


if __name__=='__main__':main()

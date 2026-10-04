"""Real HTTP/Chromium hotfix regression; only metadata acquisition is a fixture."""
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
    title = 'Batman: Rebirth Deluxe Edition'
    metadata = VolumeMetadata('comicvine','103802',title,2017,1,None,None,'Fixture',
        'https://comicvine.gamespot.com/volume/4050-103802/',[], 'DC',3,False,
        [IssueMetadata('comicvine',str(200+n),'103802',str(n),float(n),f'Book {n}','2017-01-01',None) for n in (1,2,3)])
    errors, dialogs = [], []
    with TemporaryDirectory(prefix='pullarr-local-review-') as temporary:
        root=Path(temporary);folder=root/'library'/'Batman - Rebirth Deluxe Edition (2017)';folder.mkdir(parents=True)
        paths=[folder/f'Batman - Rebirth Deluxe Edition (2017) - {n:03} - Book {n}.cbz' for n in (1,2,3)]
        def write(n, number):
            with ZipFile(paths[n-1],'w') as archive:
                archive.writestr('page.jpg', b'unchanged synthetic page')
                archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>{title}</Series><Number>{number}</Number><Year>2017</Year></ComicInfo>')
        for n in (1,2,3):write(n,n)
        set_db_location(str(root/'db'));server=Server()
        with server.app.app_context():
            setup_db();Settings().update({'api_key':'synthetic-local-review'})
            db=get_db();db.execute('UPDATE indexer_clients SET enabled=0')
            db.execute('INSERT INTO root_folders VALUES(1,?)',(str(folder.parent),));commit()
            with patch('backend.implementations.volumes.get_volume_provider',return_value=object()), \
                    patch('backend.implementations.volumes.fetch_volume_result',AsyncMock(return_value=VolumeFetchResult(metadata,()))):
                volume=Library.add_metadata(ProviderVolumeIdentity('comicvine','103802'),1,True,
                                            volume_folder=str(folder),organizer_registration=True)
                commit()
        http=make_server('127.0.0.1',0,server.app,threaded=True,request_handler=QuietHandler)
        Thread(target=http.serve_forever,daemon=True).start();origin=f'http://127.0.0.1:{http.server_port}'
        try:
            with sync_playwright() as driver:
                browser=driver.chromium.launch();page=browser.new_page()
                page.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'synthetic-local-review',last_login:Date.now()/1000}));")
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('dialog',lambda dialog:(dialogs.append(dialog.type),dialog.dismiss()))
                page.on('response',lambda response:errors.append(f'HTTP {response.status}') if response.status>=500 else None)
                page.goto(origin+f'/volumes/{volume}')
                expect(page.locator('#loading-screen')).to_be_hidden()
                page.locator('#refresh-button').click()
                preview=page.locator('#volume-local-scan-results')
                expect(preview).to_contain_text('#3 Book 3')
                expect(preview.get_by_role('button',name='Review issue match')).to_have_count(0)
                with server.app.app_context():assert get_db().execute('SELECT COUNT(*) FROM files').fetchone()[0]==0
                # A genuine issue-level contradiction affects only Book 1.
                write(1,2);original=[p.read_bytes() for p in paths]
                page.locator('#refresh-button').click()
                review=preview.get_by_role('button',name='Review issue match',exact=True)
                expect(review).to_have_count(1)
                for width in (1280,390):
                    page.set_viewport_size({'width':width,'height':900})
                    review.click()
                    panel=preview.locator('.local-issue-review:visible')
                    expect(panel).to_contain_text('ComicInfo issue number differs from the filename issue.')
                    expect(panel).to_contain_text(title)
                    expect(panel).to_contain_text(paths[0].name)
                    expect(panel).to_contain_text('Filename issue: 1')
                    expect(panel).to_contain_text('ComicInfo Number: 2')
                    expect(panel.get_by_label('#1 Book 1',exact=True)).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    expect(preview).to_contain_text('#3 Book 3')
                    panel.get_by_role('button',name='Cancel',exact=True).click()
                    expect(review).to_be_focused()
                    with server.app.app_context():assert get_db().execute('SELECT COUNT(*) FROM files').fetchone()[0]==0
                page.set_viewport_size({'width':1280,'height':900})
                review.click();panel=preview.locator('.local-issue-review:visible')
                panel.get_by_label('#1 Book 1',exact=True).check()
                panel.get_by_role('button',name='Save association').click()
                expect(preview).to_contain_text('Associated: #1 Book 1')
                expect(preview).to_contain_text('#2 Book 2')
                expect(preview).to_contain_text('#3 Book 3')
                preview.get_by_role('button',name='Apply ready associations').click()
                expect(preview).to_contain_text('2 files imported or associated.')
                with server.app.app_context():
                    db=get_db();links=db.execute('SELECT issue_id,forced FROM issues_files ORDER BY issue_id').fetchall()
                    assert [tuple(row) for row in links]==[(1,1),(2,0),(3,0)], links
                    assert tuple(db.execute('SELECT folder,comicvine_id FROM volumes').fetchone())==(str(folder),103802)
                assert [p.read_bytes() for p in paths]==original
                # Refreshing an open review is observational and permits a new preview.
                page.locator('#refresh-button').click();review.click()
                expect(preview.locator('.local-issue-review:visible')).to_contain_text('Existing association: #1 (manual)')
                page.reload();expect(page.locator('#loading-screen')).to_be_hidden()
                page.locator('#refresh-button').click();review.click()
                expect(preview.locator('.local-issue-review:visible')).to_be_visible()
                preview.locator('.local-issue-review:visible').get_by_role('button',name='Cancel').click()
                with page.expect_response(lambda r:'/organization-scan?' in r.url) as response:
                    page.locator('#refresh-button').click()
                identifier=response.value.json()['result']['id']
                endpoint=origin+f'/api/volumes/{volume}/local-scan/{identifier}/review/0?api_key=synthetic-local-review'
                invalid=page.request.post(endpoint,data={'issue_ids':[999]})
                assert invalid.status==409 and invalid.json()['result']['code']=='invalid_issue_selection'
                invalid=page.request.post(endpoint,data={'issue_ids':None})
                assert invalid.status==400
                _sessions[identifier].expires=0
                review.click()
                expect(preview.locator('.local-issue-review:visible')).to_contain_text('This preview is stale. Refresh Local Scan and try again.')
                assert not errors and not dialogs,(errors,dialogs)
                browser.close()
            print('Local review Chromium PASS: clean punctuation/numeric books, exact conflict evidence, fixed volume, open/cancel/reopen/save, same preview/partial Apply, reload, stale error, unchanged files; zero dialogs/HTTP500/JS exceptions')
        finally:http.shutdown();_sessions.clear()


if __name__=='__main__':main()

"""Disposable real API/Chromium regression for explicit import and local scans."""
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
from Tbackend.features.direct_downloads import FixtureHTTP, config
from werkzeug.serving import make_server

from backend.features.direct_downloads import ManualDDL
from backend.features.wanted_search import UnifiedReleaseSearch
from backend.implementations.direct_download_source import GetComicsSource
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)
from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    title = 'Batman: Rebirth Deluxe Edition'
    metadata = VolumeMetadata('comicvine','103802',title,2017,1,None,None,'Fixture','https://comicvine.gamespot.com/volume/4050-103802/',[], 'DC',3,False,
        [IssueMetadata('comicvine',str(200+n),'103802',str(n),float(n),f'Book {n}','2017-01-01',None) for n in (1,2,3)])
    async def proposals(groups, **kwargs):
        return {key:dict(id=103802,title=title,issue_count=3,link='https://comicvine.gamespot.com/volume/4050-103802/') for key in groups}
    errors, dialogs = [], []
    with TemporaryDirectory(prefix='pullarr-reliability-browser-') as temporary:
        root = Path(temporary)
        folder = root/'library'/'Batman - Rebirth Deluxe Edition (2017)'
        folder.mkdir(parents=True)
        paths = []
        for number in (1,2,3):
            path = folder/f'Batman - Rebirth Deluxe Edition (2017) - {number:03} - Book {number}.cbz'
            with ZipFile(path,'w') as archive:
                archive.writestr('page.jpg',b'synthetic unchanged page')
            paths.append(path)
        original = [p.read_bytes() for p in paths]
        set_db_location(str(root/'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key':'synthetic-reliability-browser'})
            db = get_db()
            db.execute('UPDATE indexer_clients SET enabled=0')
            db.execute('INSERT INTO root_folders VALUES(1,?)',(str(folder.parent),))
            db.connection.commit()
        with patch('backend.features.library_import.ComicVine') as comicvine, \
                patch('backend.implementations.volumes.get_volume_provider',return_value=object()), \
                patch('backend.implementations.volumes.fetch_volume_result',AsyncMock(return_value=VolumeFetchResult(metadata,()))):
            comicvine.return_value.filenames_to_cvs = proposals
            http = make_server('127.0.0.1',0,server.app,threaded=True,request_handler=QuietHandler)
            Thread(target=http.serve_forever,daemon=True).start()
            origin = f'http://127.0.0.1:{http.server_port}'
            try:
                with sync_playwright() as driver:
                    browser = driver.chromium.launch()
                    page = browser.new_page()
                    page.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'synthetic-reliability-browser',last_login:Date.now()/1000}));")
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('dialog',lambda dialog:(dialogs.append(dialog.type),dialog.dismiss()))
                    page.on('response',lambda response:errors.append(f'HTTP {response.status}') if response.status>=500 else None)
                    page.goto(origin+'/library-import')
                    page.locator('#run-import-button').click()
                    expect(page.locator('.proposal-list tr[data-rowid]')).to_have_count(3)
                    page.locator('#import-button').click()
                    expect(page.locator('#library-import-results')).to_contain_text('3 files imported or associated.')
                    with server.app.app_context():
                        row = get_db().execute('SELECT id,folder,custom_folder FROM volumes').fetchone()
                        assert row[1:] == (str(folder),True), row
                        volume_id = row[0]
                        assert get_db().execute('SELECT COUNT(*) FROM issues_files').fetchone()[0] == 3
                    page.goto(origin+f'/volumes/{volume_id}')
                    expect(page.locator('#loading-screen')).to_be_hidden()
                    expect(page.locator('main')).to_contain_text(str(folder))
                    response = page.request.get(origin+f'/api/volumes/{volume_id}?api_key=synthetic-reliability-browser&metadata=true&issue_facts=1')
                    assert response.status == 200
                    # Remove only synthetic associations to exercise additive scan.
                    with server.app.app_context():
                        db=get_db(); db.execute('DELETE FROM issues_files'); db.execute('DELETE FROM files'); db.connection.commit()
                    page.locator('#refresh-button').click()
                    expect(page.get_by_role('button',name='Apply ready associations')).to_be_visible()
                    with server.app.app_context():
                        assert get_db().execute('SELECT COUNT(*) FROM issues_files').fetchone()[0] == 0
                    page.get_by_role('button',name='Apply ready associations').click()
                    expect(page.get_by_text('3 files imported or associated.',exact=True)).to_be_visible()
                    with server.app.app_context():
                        assert get_db().execute('SELECT COUNT(*) FROM issues_files').fetchone()[0] == 3
                    assert [p.read_bytes() for p in paths] == original
                    # The same canonical Book 1 metadata drives real search/API
                    # evaluation. Only the provider transport and final queue
                    # boundary are synthetic; the browser chooses the release.
                    with server.app.app_context():
                        db = get_db(); db.execute('DELETE FROM issues_files'); db.connection.commit()
                    release_title = 'Batman – Rebirth Deluxe Edition Book 1 (2017)'
                    remote = FixtureHTTP(titles=(release_title,
                        'Batman Detective Comics Rebirth Deluxe Edition Book 4 (2017)'),
                        offerings=(release_title,))
                    dispatched = []
                    def dispatch(selection, offering, group, issue, forced, blocked):
                        dispatched.append((offering.target.issue_ids, forced))
                        decision = selection.authorization['automation_decision_id']
                        db = get_db()
                        db.execute('INSERT INTO wanted_acquisitions VALUES(?,?,?)',
                                   (decision, 'direct_download', 'synthetic-browser-queue'))
                        db.connection.commit()
                        return [dict(id=1)]
                    def factory(**kwargs):
                        return ManualDDL(**kwargs, source_factory=lambda c:GetComicsSource(c, remote),
                                         dispatch=dispatch)
                    searches = UnifiedReleaseSearch(nzb_loader=lambda:(), ddl_loader=lambda:{1:config()},
                                                    ddl_factory=factory)
                    try:
                        with patch('backend.features.wanted_automation.UNIFIED_SEARCH', searches):
                            page.goto(origin+f'/volumes/{volume_id}')
                            with page.expect_response(lambda r:'/issues/1/release-search?' in r.url) as searched:
                                page.locator('tr[data-id="1"] .action-column > :nth-child(2)').click()
                            result = searched.value.json()['result']
                            assert result['results'][0]['download_eligible'], result
                            assert result['results'][1]['explanation']['state'] == 'rejected', result
                            row = page.locator('#search-result-table tbody tr').filter(has_text=release_title)
                            page.get_by_label('Sort results').select_option('source_desc')
                            page.get_by_label('Source filter').select_option('Fixture GetComics')
                            row.get_by_role('button',name='Download',exact=True).click()
                            expect(row).to_contain_text('Dispatched to the download queue.')
                            assert dispatched == [((1,), False)], dispatched
                    finally:
                        searches.close_all()
                    assert not errors,errors
                    assert not dialogs,dialogs
                    browser.close()
                print('Reliability Chromium PASS: exact three-book import, atomic adopted folder, volume API/page, nonmutating scan, explicit Apply, Batman Book 1 search and manual GetComics selection, unrelated title rejection, source sort/filter, unchanged files, no native dialogs or HTTP 500')
            finally:
                http.shutdown()


if __name__ == '__main__':
    main()

"""Real Pullarr HTTP/Chromium, disposable data and loopback client fixtures."""
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests'), str(REPO / 'scripts')]

from api_key_browser_acceptance import QuietHandler
from fixtures.expanded_clients import configure, services
from fixtures.quality import comic
from playwright.sync_api import sync_playwright
from TQuality import policy
from werkzeug.serving import make_server

from backend.features.intake_runtime import IntakeRuntime
from backend.features.sab_downloads import poll_downloads
from backend.features.torrent_lifecycle import observe_torrents
from backend.features.wanted_runtime import WantedRuntime
from backend.implementations.file_quality import analyze
from backend.implementations.managed_clients import client_for
from backend.internals.db import (DBConnection, get_db,
                                  set_db_location, setup_db)
from backend.internals.download_jobs import DownloadStore
from backend.internals.quality import QualityStore
from backend.internals.server import Server
from backend.internals.settings import Settings


def main(false_hd=False):
    errors, external = [], []
    atlas = REPO / '.devdata' / 'phase9a-atlas'
    atlas.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='pullarr-9a-browser-') as directory, services() as remote:
        root = Path(directory)
        folder, incoming = root / 'library' / 'Batman', root / 'incoming'
        folder.mkdir(parents=True); incoming.mkdir()
        set_db_location(str(root / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key':'synthetic-expanded-browser'})
            db = get_db()
            db.execute('UPDATE indexer_clients SET enabled=0')
            db.execute('INSERT INTO root_folders VALUES(1,?)',(str(folder.parent),))
            db.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder,monitored) VALUES(1,101,'Batman',2020,1,1,?,1)",(str(folder),))
            db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
            db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(1,1,201,'1',1,1)")
            old = folder / 'Batman 001.cbz'; comic(old,600)
            comic(incoming / remote['files'][0],900 if false_hd else 1200)
            original = old.read_bytes()
            db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)',(str(old),old.stat().st_size))
            db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
            quality = QualityStore(db)
            quality.save('Verified upgrade fixture',policy(),identifier=1,revision=1)
            quality.assessment(1,analyze(str(old)))
            client = configure(db,remote,incoming)
            db.connection.commit()
        http = make_server('127.0.0.1',0,server.app,threaded=True,request_handler=QuietHandler)
        Thread(target=http.serve_forever,daemon=True).start()
        origin = 'http://127.0.0.1:' + str(http.server_port)
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch()
                context = browser.new_context()
                context.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'synthetic-expanded-browser',last_login:Date.now()/1000}));")
                page = context.new_page()
                page.on('pageerror',lambda _:errors.append('page error'))
                page.on('console',lambda m:errors.append('console error') if m.type=='error' else None)
                page.on('request',lambda r:external.append('external request') if not r.url.startswith(origin) else None)
                for width in (1440,390):
                    page.set_viewport_size({'width':width,'height':844})
                    page.goto(origin + '/settings/downloadclients')
                    page.locator('#managed-list button').first.click()
                    page.locator('#managed-test').focus(); page.keyboard.press('Enter')
                    page.get_by_text('Connected: qBittorrent v5.0.0 · torrent',exact=True).wait_for()
                    page.locator('#managed-name').fill('<script>window.expandedHostile=true</script> fixture')
                    page.locator('#managed-form button[type=submit]').click()
                    page.get_by_text('Saved. No download was started.',exact=True).wait_for()
                    assert page.locator('#managed-password').input_value()==''
                    assert page.evaluate('window.expandedHostile') is None
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(atlas / f'clients-{width}.png'),full_page=True)
                page.locator('#managed-new').click()
                page.locator('#managed-name').fill('NZBGet fixture')
                page.locator('#managed-url').fill(remote['url'] + '/nzbget')
                page.locator('#managed-username').fill('fixture-user')
                page.locator('#managed-password').fill('fixture-password')
                page.locator('#managed-form button[type=submit]').click()
                page.get_by_text('Saved. No download was started.',exact=True).wait_for()
                page.locator('#managed-test').click()
                page.get_by_text('Connected: NZBGet 25.3 · usenet',exact=True).wait_for()
                page.goto(origin + '/wanted')
                with page.expect_response(lambda r:r.request.method=='POST' and '/api/wanted?' in r.url):
                    page.get_by_role('button',name='Search and grab now',exact=True).click()
                WantedRuntime(DBConnection.default_file,server.app).tick()
                assert remote['submitted']==1
                remote['completed']=True
                downloads=DownloadStore(DBConnection.default_file)
                try:
                    poll_downloads(downloads,[client],client_factory=client_for)
                    observe_torrents(downloads,[client],client_for)
                finally:
                    downloads.close()
                runtime=IntakeRuntime(DBConnection.default_file)
                for stamp in (100.,111.,122.,133.,144.):
                    runtime.clock=lambda stamp=stamp:stamp
                    runtime.tick()
                assert (incoming / remote['files'][0]).exists()
                with server.app.app_context():
                    assert QualityStore(get_db()).issue_states([1])[0]['cutoff_satisfied'] is not false_hd
                    if false_hd:
                        assert old.read_bytes() == original
                        assert get_db().execute("SELECT COUNT(*) FROM acquisition_provenance WHERE error='dimension_floor_failed'").fetchone()[0] == 1
                for width in (1440,390):
                    page.set_viewport_size({'width':width,'height':844})
                    page.goto(origin + '/activity/queue')
                    page.locator('#managed-download-rows table').wait_for()
                    if not false_hd:
                        imported = page.locator('#managed-download-rows td').filter(has_text=re.compile(r'^Imported'))
                        imported.wait_for()
                        assert imported.evaluate('node => node.firstChild.textContent') == 'Imported'
                    page.get_by_role('button',name='Remove torrent + data',exact=True).click()
                    page.locator('#managed-download-status').filter(has_text='Removal blocked:').wait_for()
                    assert 'Indexer/tracker minimum seed requirements' in page.locator('#managed-download-status').inner_text()
                    assert not remote['removed']
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(atlas / f'queue-{"false-hd-" if false_hd else ""}{width}.png'),full_page=True)
                if false_hd:
                    page.goto(origin + '/wanted')
                    page.locator('#wanted-status').filter(has_text='Automation worker:').wait_for()
                    # Review-required intake retains the existing reservation;
                    # the browser must not offer another automatic acquisition.
                    assert page.get_by_role('button',name='Search and grab now',exact=True).count() == 0
                    WantedRuntime(DBConnection.default_file,server.app).tick()
                    assert remote['submitted'] == 1
                    assert old.read_bytes() == original
                else:
                    remote['ratio'] = 1.
                    page.once('dialog', lambda dialog:dialog.accept())
                    page.get_by_role('button',name='Remove torrent + data',exact=True).click()
                    page.get_by_text('Reviewed removal completed. Library copy retained.',exact=True).wait_for()
                    assert remote['removed'] and remote['delete_data']
                    assert old.exists()
                browser.close()
            assert not errors,errors
            assert not external,external
            print(f'Expanded clients Chromium PASS (false-HD={false_hd}): desktop/narrow keyboard Settings/Test, masked secrets, hostile text, Wanted torrent acquisition, seeding/import separation and reviewed cleanup; zero unexpected browser errors/external requests')
        finally:
            http.shutdown()


if __name__=='__main__':
    main('--false-hd' in sys.argv)

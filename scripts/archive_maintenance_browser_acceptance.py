"""Real HTTP/TaskHandler/Chromium against disposable archive fixtures."""
import logging
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO/'tests'), str(REPO/'scripts')]

from api_key_browser_acceptance import QuietHandler
from playwright.sync_api import expect, sync_playwright
from TArchiveMaintenance import comic
from werkzeug.serving import make_server

from backend.base.logging import LOGGER
from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    errors, external, logs = [], [], []
    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                logs.append(record.getMessage())
    capture = Capture(); LOGGER.addHandler(capture)
    atlas = REPO/'.devdata/phase9b-atlas'; atlas.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='pullarr-archives-browser-') as temporary:
        root = Path(temporary); folder = root/'library'/'Fixture'; folder.mkdir(parents=True)
        set_db_location(str(root/'db')); server = Server()
        with server.app.app_context():
            setup_db(); Settings().update({'api_key':'synthetic-archive-browser'})
            db = get_db(); db.execute('UPDATE indexer_clients SET enabled=0')
            db.execute('INSERT INTO root_folders VALUES(1,?)', (str(folder.parent),))
            db.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder) VALUES(1,101,?,2020,1,1,?)",
                       ('<script>window.archiveHostile=true</script> café',str(folder)))
            db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
            paths = []
            for index, name in enumerate(('ordinary.cbr','shared.cbr','healthy.cbz','damaged.cbz'),1):
                path = folder/name; paths.append(path)
                if index==4:
                    path.write_bytes(b'broken archive synthetic fixture')
                else:
                    comic(path, index<3)
                db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(?,1,?,?,?)',
                           (index,200+index,str(index),index))
                db.execute('INSERT INTO files(id,filepath,size) VALUES(?,?,?)',(index,str(path),path.stat().st_size))
                db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,?)',(index,index))
            seed = root/'seed.cbr'; os.link(paths[1],seed); seed_bytes = seed.read_bytes()
            original = [p.read_bytes() for p in paths]
            db.connection.commit()
        http = make_server('127.0.0.1',0,server.app,threaded=True,request_handler=QuietHandler)
        Thread(target=http.serve_forever,daemon=True).start()
        origin = 'http://127.0.0.1:'+str(http.server_port)
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch()
                context = browser.new_context()
                context.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'synthetic-archive-browser',last_login:Date.now()/1000}));")
                page = context.new_page()
                page.on('pageerror',lambda _:errors.append('page error'))
                page.on('console',lambda m:errors.append('console error') if m.type=='error' else None)
                page.on('request',lambda r:external.append('external') if not r.url.startswith(origin) else None)
                page.goto(origin+'/maintenance#archive-maintenance')
                page.locator('#archive-files input').first.wait_for()
                page.locator('#archive-select').click(); page.locator('#archive-scan').click()
                page.locator('#archive-results').get_by_text('convertible',exact=True).first.wait_for()
                assert [p.read_bytes() for p in paths]==original
                assert page.evaluate('window.archiveHostile') is None
                for width in (1440,390):
                    page.set_viewport_size({'width':width,'height':844})
                    page.locator('#archive-preview').focus(); page.keyboard.press('Enter')
                    page.locator('#archive-dialog').wait_for(state='visible')
                    assert 'Shared bytes' in page.locator('#archive-review').inner_text()
                    assert not page.locator('#archive-review input[value="3"]').is_checked()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(atlas/f'preview-{width}.png'),full_page=True)
                    page.locator('#archive-close').focus()
                    expect(page.locator('#archive-close')).to_be_in_viewport()
                    page.keyboard.press('Escape')
                    expect(page.locator('#archive-preview')).to_be_focused()
                page.locator('#archive-preview').click()
                page.locator('#archive-dialog').wait_for(state='visible')
                page.locator('#archive-apply').click()
                page.locator('#archive-results').get_by_text('completed',exact=True).first.wait_for()
                assert not paths[0].exists() and not paths[1].exists()
                assert seed.read_bytes()==seed_bytes
                assert not os.path.samefile(seed,paths[1].with_suffix('.cbz'))
                assert paths[2].read_bytes()==original[2] and paths[3].read_bytes()==original[3]
                page.locator('#archive-files input[value="3"]').check()
                page.locator('#archive-preview').click(); page.locator('#archive-dialog').wait_for(state='visible')
                page.locator('#archive-review input[value="3"]').check(); page.locator('#archive-apply').click()
                page.locator('#archive-results').get_by_text('completed',exact=True).wait_for()
                # Confirmation binds exact bytes/sharing; changed input cannot
                # be replaced with the earlier review's authority.
                page.locator('#archive-files input[value="3"]').check()
                page.locator('#archive-preview').click();page.locator('#archive-dialog').wait_for(state='visible')
                page.locator('#archive-review input[value="3"]').check()
                with paths[2].open('ab') as output:output.write(b'stale-fixture')
                changed=paths[2].read_bytes()
                page.locator('#archive-apply').click()
                page.locator('#archive-results').get_by_text('stale preview',exact=True).wait_for()
                assert paths[2].read_bytes()==changed
                with server.app.app_context():
                    db=get_db(); assert db.execute('SELECT COUNT(*) FROM files').fetchone()[0]==4
                    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                    assert not db.execute('PRAGMA foreign_key_check').fetchall()
                page.screenshot(path=str(atlas/'completed-390.png'),full_page=True)
                browser.close()
            assert not errors,errors
            assert not external,external
            assert not logs,logs
            print('Archive Chromium PASS: scan/dry-run ordinary/shared CBR, optional healthy CBZ repack, damaged preservation, keyboard/Escape/focus, desktop/narrow hostile text; zero unexpected browser/server/external errors')
        finally:
            http.shutdown(); LOGGER.removeHandler(capture)


if __name__=='__main__':
    main()

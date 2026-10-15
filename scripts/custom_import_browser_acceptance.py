"""Disposable Chromium acceptance for local publications and persistent ignores."""

import sys
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

from api_key_browser_acceptance import QuietHandler

from backend.internals.db import commit, get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    with TemporaryDirectory(prefix='pullarr-custom-browser-') as directory:
        root = Path(directory)
        library = root / 'library'; source = library / 'Civil War Ultimate Edition'
        source.mkdir(parents=True)
        for n in range(1, 14):
            with ZipFile(source / f'Civil War Ultimate Edition #{n:02}.cbz', 'w') as archive:
                archive.writestr('page.jpg', f'disposable {n}'.encode())
        set_db_location(str(root / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key':'disposable-custom-browser'})
            get_db().execute('INSERT INTO root_folders VALUES(1,?)', (str(library),))
            commit()
        http = make_server('127.0.0.1', 0, server.app, threaded=True,
                           request_handler=QuietHandler)
        Thread(target=http.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{http.server_port}'
        shots = ROOT / '.devdata' / 'custom-import-browser'
        shots.mkdir(parents=True, exist_ok=True)
        errors = []
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch()
                page = browser.new_page(viewport={'width':1440,'height':900})
                page.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'disposable-custom-browser',last_login:Date.now()/1000,theme:'dark'}))")
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin + '/library-import')
                page.locator('#auto-match-input').uncheck()
                page.locator('#scan-file-limit').select_option('20')
                page.locator('#run-import-button').click()
                rows = page.locator('.proposal-list tr[data-rowid]')
                expect(rows).to_have_count(13)
                assert not page.locator('#create-custom-selected').is_enabled()
                for n in range(3, 14):
                    rows.filter(has_text=f'Civil War Ultimate Edition #{n:02}').locator('.bulk-match-select').check()
                expect(page.locator('#create-custom-selected')).to_contain_text('(11)')
                page.locator('#create-custom-selected').click()
                expect(page.locator('#custom-publication-dialog')).to_be_visible()
                expect(page.locator('#custom-issue-count')).to_have_value('13')
                image = BytesIO(); Image.new('RGB',(120,180),'teal').save(image,'PNG')
                page.locator('#custom-cover').set_input_files({'name':'synthetic.png',
                    'mimeType':'image/png','buffer':image.getvalue()})
                expect(page.locator('#custom-cover-preview')).to_be_visible()
                for width in (1440, 1024, 768, 390):
                    page.set_viewport_size({'width':width,'height':900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                    page.screenshot(path=str(shots / f'create-{width}.png'), full_page=False)
                page.set_viewport_size({'width':1440,'height':900})
                page.locator('#custom-title').fill('Civil War Ultimate Edition')
                cover_attempts = [0]
                def cover_upload(route):
                    if route.request.method == 'POST' and cover_attempts[0] == 0:
                        cover_attempts[0] += 1
                        route.fulfill(status=400, json={'error':'ManualCover',
                            'result':{'code':'invalid_image'}})
                    else:
                        route.continue_()
                page.route('**/api/volumes/*/manual-cover?*', cover_upload)
                page.locator('#custom-create').click()
                expect(page.locator('#custom-publication-dialog')).to_be_visible()
                expect(page.locator('#custom-error')).to_contain_text('Publication created')
                page.locator('#custom-create').click()
                expect(page.locator('#custom-publication-dialog')).not_to_be_visible()
                chosen = rows.filter(has_text='Civil War Ultimate Edition #03')
                expect(chosen.locator('.publication-title')).to_have_text('Civil War Ultimate Edition')
                expect(chosen.locator('.issue-column > span')).to_contain_text('#3')
                page.locator('#selectall-input').uncheck()
                chosen.locator('.import-file-select').check()
                page.locator('#import-button').click()
                expect(chosen.locator('.import-status-text')).to_contain_text('Imported')
                assert (source / 'Civil War Ultimate Edition #03.cbz').exists()
                for width in (1440, 1024, 768, 390):
                    page.set_viewport_size({'width':width,'height':900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                    page.screenshot(path=str(shots / f'import-{width}.png'), full_page=False)
                page.goto(origin + '/')
                expect(page.locator('#list-library .list-entry')).to_have_count(1)
                page.locator('#list-library .list-entry').click()
                expect(page.locator('#local-publication-note')).to_contain_text('Custom / Local publication')
                for width in (1440, 1024, 768, 390):
                    page.set_viewport_size({'width':width,'height':900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                    page.screenshot(path=str(shots / f'volume-{width}.png'), full_page=False)
                expect(page.locator('#volume-cover-source')).to_have_text('Manual cover')
                page.locator('#remove-volume-cover').click()
                expect(page.locator('#volume-cover-source')).to_have_text('No cover available')
                page.locator('#volume-cover-file').set_input_files({'name':'synthetic.png',
                    'mimeType':'image/png','buffer':image.getvalue()})
                expect(page.locator('#volume-cover-source')).to_have_text('Manual cover')
                page.locator('#edit-button').click()
                expect(page.locator('#local-publication-edit')).to_be_visible()
                page.locator('#local-edit-cancel').click()
                page.goto(origin + '/library-import')
                page.locator('#auto-match-input').uncheck()
                page.locator('#run-import-button').click()
                expect(rows).to_have_count(12)
                for n in range(1, 13):
                    if n == 3: continue
                    rows.filter(has_text=f'Civil War Ultimate Edition #{n:02}').locator('.bulk-match-select').check()
                page.locator('#ignore-selected').click()
                expect(page.locator('#ignore-confirm')).to_be_visible()
                page.locator('#ignore-accept').click()
                expect(rows).to_have_count(1)
                page.locator('#list-window .cancel-button').click()
                page.locator('#show-ignored-files').click()
                expect(page.locator('.ignored-file-row')).to_have_count(11)
                page.locator('.ignored-file-row').first.locator('button').click()
                page.locator('#run-import-button').click()
                expect(rows).to_have_count(2)
                remaining = rows.filter(has_text='Civil War Ultimate Edition #13')
                remaining.locator('.publication-choice').click()
                expect(page.locator('#cv-window')).to_be_visible()
                page.locator('#search-my-publications').click()
                expect(page.locator('#my-publications-results button')).to_have_count(1)
                page.locator('#my-publications-results button').click()
                expect(remaining.locator('.issue-column > span')).to_contain_text('#13')
                other = rows.filter(has_text='Civil War Ultimate Edition #01')
                other.locator('.publication-choice').click()
                page.locator('#create-custom-from-search').click()
                expect(page.locator('#custom-publication-dialog')).to_be_visible()
                page.locator('#custom-cancel').click()
                expect(page.locator('#custom-publication-dialog')).not_to_be_visible()
                other.locator('.publication-choice').click()
                page.locator('#search-my-publications').click()
                expect(page.locator('#my-publications-results button')).to_have_count(1)
                assert all((source / f'Civil War Ultimate Edition #{n:02}.cbz').exists()
                           for n in range(1, 14))
                light = browser.new_page(viewport={'width':1440,'height':900})
                light.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'disposable-custom-browser',last_login:Date.now()/1000,theme:'light'}))")
                light.on('pageerror', lambda error: errors.append(str(error)))
                light.goto(origin + '/library-import')
                light.locator('#auto-match-input').uncheck()
                light.locator('#run-import-button').click()
                expect(light.locator('.proposal-list tr[data-rowid]')).to_have_count(2)
                for width in (1440, 1024, 768, 390):
                    light.set_viewport_size({'width':width,'height':900})
                    assert light.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                    light.screenshot(path=str(shots / f'light-import-{width}.png'), full_page=False)
                light.close()
                browser.close()
            assert not errors, errors
            print('custom import Chromium: 13 issues, 11 action-selected, keep import, cover, ignore/restore, dark/light at 4 widths PASS')
        finally:
            http.shutdown()


if __name__ == '__main__':
    main()

"""Disposable Chromium exercise of manual cover controls and Library artwork."""

import sys
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import requests
from api_key_browser_acceptance import QuietHandler
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from fixtures.discovery import seed

from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    with TemporaryDirectory(prefix='pullarr-manual-cover-browser-') as directory:
        base = Path(directory)
        set_db_location(str(base / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key': 'disposable-manual-cover'})
            seed(get_db(), base, quality=False)
        http = make_server('127.0.0.1', 0, server.app, threaded=True,
                           request_handler=QuietHandler)
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{http.server_port}'
        output = BytesIO()
        Image.new('RGB', (300, 450), '#346778').save(output, format='PNG')
        screenshots = ROOT / '.devdata' / 'manual-cover-acceptance'
        screenshots.mkdir(parents=True, exist_ok=True)
        errors = []
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch()
                page = browser.new_page()
                page.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'disposable-manual-cover',last_login:Date.now()/1000,theme:'dark'}))")
                page.on('pageerror', lambda error: errors.append(str(error)))
                for width in (1440, 1024, 768, 390):
                    page.set_viewport_size({'width': width, 'height': 900})
                    page.goto(origin + '/volumes/1')
                    expect(page.locator('#volume-cover-source')).to_have_text('No cover available')
                    assert page.locator('.volume-info > img').is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                    assert page.locator('#upload-volume-cover').is_visible()
                    assert page.locator('#remove-volume-cover').is_hidden()
                    page.screenshot(path=str(screenshots / f'volume-{width}.png'), full_page=False)
                page.locator('#volume-cover-file').set_input_files({
                    'name': 'synthetic.png', 'mimeType': 'image/png', 'buffer': output.getvalue()})
                expect(page.locator('#volume-cover-source')).to_have_text('Manual cover')
                expect(page.locator('#volume-cover-feedback')).to_have_text('Cover updated.')
                expect(page.locator('#remove-volume-cover')).to_be_visible()
                expect(page.locator('#volume-cover-note')).to_have_text(
                    'Removing this cover will show the no-cover placeholder.')
                page.screenshot(path=str(screenshots / 'volume-390-manual.png'), full_page=False)
                active = requests.get(origin + '/api/volumes/1/cover',
                    params={'api_key': 'disposable-manual-cover'}, timeout=10)
                assert active.status_code == 200 and active.headers['Content-Type'].startswith('image/jpeg')
                with Image.open(BytesIO(active.content)) as cover:
                    assert cover.size == (300, 450)
                page.goto(origin + '/')
                expect(page.locator('#list-library .vol-1 img')).to_be_visible()
                assert 'api/volumes/1/cover' in page.locator('#list-library .vol-1 img').get_attribute('src')
                page.wait_for_function("document.querySelector('#list-library .vol-1 img').naturalWidth === 300")
                page.goto(origin + '/volumes/1')
                expect(page.locator('#volume-cover-source')).to_have_text('Manual cover')
                page.locator('#remove-volume-cover').focus()
                page.keyboard.press('Enter')
                expect(page.locator('#volume-cover-source')).to_have_text('No cover available')
                expect(page.locator('#volume-cover-feedback')).to_have_text('Manual cover removed.')
                assert page.locator('#upload-volume-cover').evaluate('(el)=>el===document.activeElement')
                page.goto(origin + '/')
                expect(page.locator('#list-library .vol-1 img')).to_be_visible()
                page.wait_for_function("document.querySelector('#list-library .vol-1 img').complete")
                assert 0 < page.locator('#list-library .vol-1 img').evaluate('(el)=>el.naturalWidth') != 300
                browser.close()
            assert not errors, errors
            print('manual cover Chromium: 4 widths, upload, Library artwork, removal, focus PASS')
        finally:
            http.shutdown()


if __name__ == '__main__':
    main()

"""Real HTTP/Chromium Security controls; disposable DB and synthetic keys only."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


class QuietHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        pass  # Existing API authentication uses query strings; never record keys.


def main():
    with TemporaryDirectory(prefix='pullarr-key-') as directory:
        set_db_location(str(Path(directory) / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key': 'synthetic-security-controls'})
            get_db().connection.commit()
        http = make_server('127.0.0.1', 0, server.app, threaded=True, request_handler=QuietHandler)
        Thread(target=http.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{http.server_port}'
        errors = []
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch()
                context = browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
                context.add_init_script("if (!localStorage.getItem('kapowarr')) localStorage.setItem('kapowarr',JSON.stringify({api_key:'synthetic-security-controls',last_login:Date.now()/1000}));")
                page = context.new_page()
                page.on('pageerror', lambda _: errors.append('page error'))
                page.on('console', lambda m: errors.append('console error') if m.type == 'error' else None)
                calls = []
                page.on('request', lambda r: calls.append(r.method) if r.url.split('?')[0].endswith('/api/settings/api_key') else None)
                for width in (1440, 390):
                    page.set_viewport_size({'width': width, 'height': 844})
                    page.goto(origin + '/settings/general')
                    page.wait_for_function("document.querySelector('#api-input').value.length > 0")
                    page.wait_for_timeout(700)
                    field = page.locator('#api-input')
                    assert field.get_attribute('type') == 'password'
                    before = field.input_value()
                    count = len(calls)
                    page.locator('#copy-api').focus()
                    page.keyboard.press('Enter')
                    page.get_by_text('API key copied.', exact=True).wait_for()
                    assert page.evaluate('navigator.clipboard.readText()') == before
                    assert field.get_attribute('type') == 'password'
                    page.locator('#reveal-api').focus()
                    page.keyboard.press('Enter')
                    assert field.get_attribute('type') == 'text'
                    page.keyboard.press('Enter')
                    assert field.get_attribute('type') == 'password'
                    assert len(calls) == count
                    page.once('dialog', lambda d: d.accept())
                    page.locator('#generate-api').click()
                    page.wait_for_function("document.querySelector('#api-key-status').textContent.startsWith('API key regenerated.')")
                    assert len(calls) == count + 1
                    assert field.input_value() != before
                    assert field.get_attribute('type') == 'password'
                    page.locator('#copy-api').click()
                    page.get_by_text('API key copied.', exact=True).wait_for()
                    assert page.evaluate('navigator.clipboard.readText()') == field.input_value()
                    page.locator('#reveal-api').click()
                    page.reload()
                    page.wait_for_function("document.querySelector('#api-input').value.length > 0")
                    assert field.get_attribute('type') == 'password'
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                browser.close()
            assert not errors, errors
            print('Security Chromium: desktop/narrow keyboard Copy/Show/Hide/Regenerate/reload PASS; no unexpected browser errors; keys omitted')
        finally:
            http.shutdown()


if __name__ == '__main__':
    main()

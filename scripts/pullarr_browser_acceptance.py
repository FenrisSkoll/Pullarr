"""Offline real-server route atlas for the Pullarr shell (synthetic data only)."""
import json
import re
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests')]

from fixtures.discovery import seed, source_fixture
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from backend.features.wanted_search import UNIFIED_SEARCH
from backend.implementations.download_client_manager import DownloadClients
from backend.implementations.root_folders import RootFolders
from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings

ROUTES = ('/', '/add', '/library-import', '/maintenance', '/collections',
          '/calendar', '/discover', '/reading-orders', '/settings/quality',
          '/volumes/1', '/volumes/1/provider-switch', '/activity/queue',
          '/activity/history', '/activity/intake', '/wanted', '/activity/blocklist',
          '/system/status', '/system/tasks', '/system/backups',
          '/settings/mediamanagement', '/settings/indexers', '/settings/download',
          '/settings/downloadclients', '/settings/metadata', '/settings/general')


def capture(origin, output):
    errors, consoles, external, results = [], [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        def route(request):
            if request.request.url.startswith(origin):
                request.continue_()
            else:
                external.append(request.request.url)
                request.abort()
        # Intercept only off-origin traffic. Intercepting every local asset disables
        # Chromium's cache and turns the route atlas into an artificial socket flood.
        context.route(re.compile(r'^(?!' + re.escape(origin) + r'(?:/|$)).+'), route)
        context.add_init_script("if (!localStorage.getItem('kapowarr')) localStorage.setItem('kapowarr',JSON.stringify({api_key:'disposable-pullarr-ui',last_login:Date.now()/1000,theme:'light'}));")
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: consoles.append(dict(text=m.text, location=m.location)) if m.type == 'error' else None)
        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for path in ROUTES:
                start = time.perf_counter()
                response = page.goto(origin + path)
                page.wait_for_timeout(700)
                diagnostics = page.evaluate("""() => ({
                    nodes: document.querySelectorAll('*').length,
                    overflow: document.documentElement.scrollWidth > innerWidth + 1,
                    heading: document.querySelector('main h1')?.textContent,
                    title: document.title,
                    dom_ready_ms: Math.round(performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd),
                    resource_requests: performance.getEntriesByType('resource').length,
                    unnamed: [...document.querySelectorAll('main button,main input:not([type="hidden"]),main select')].filter(e => e.getClientRects().length && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby') && !e.title && !e.labels?.length && !e.textContent.trim()).map(e=>e.id || e.tagName),
                    duplicates: [...document.querySelectorAll('[id]')].map(e=>e.id).filter((id,i,all)=>id && all.indexOf(id)!==i),
                    outside: [...document.querySelectorAll('main button, main input, main select')].filter(e => {
                        const r=e.getBoundingClientRect(); return r.width && r.right > innerWidth + 1;
                    }).map(e => e.id || e.textContent.slice(0,50))
                })""")
                results.append(dict(path=path, width=width, status=response.status,
                                    elapsed_ms=round((time.perf_counter()-start)*1000), **diagnostics))
                if width in (1440, 390):
                    page.screenshot(path=str(output / f'{width}-{path.strip("/").replace("/", "-") or "library"}.png'), full_page=True)
                if width == 390:
                    page.locator('#toggle-nav').click()
                    assert page.locator('#toggle-nav').get_attribute('aria-expanded') == 'true'
                    page.keyboard.press('Escape')
                    assert page.locator('#toggle-nav').evaluate('(e)=>e===document.activeElement')
        # Dark surfaces and reduced motion use the same local routes.
        page.evaluate("localStorage.setItem('kapowarr',JSON.stringify({...JSON.parse(localStorage.getItem('kapowarr')),theme:'dark'}))")
        page.emulate_media(reduced_motion='reduce')
        for path in ROUTES:
            page.goto(origin + path)
            page.wait_for_timeout(500)
            assert page.locator('html').evaluate("e => e.classList.contains('dark-mode')")
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), path
            page.screenshot(path=str(output / f'dark-390-{path.strip("/").replace("/", "-") or "library"}.png'), full_page=True)
        # Keyboard-only legacy and native dialog families; cancel is non-mutating.
        page.goto(origin + '/volumes/1')
        page.locator('#edit-button').wait_for()
        page.locator('#edit-button').focus()
        page.keyboard.press('Enter')
        page.wait_for_timeout(100)
        assert page.evaluate("document.querySelector('.window section[show-window]').contains(document.activeElement)")
        page.keyboard.press('Escape')
        page.wait_for_timeout(100)
        assert page.locator('#edit-button').evaluate('(e)=>e===document.activeElement')
        page.goto(origin + '/settings/quality')
        page.locator('#q-create').focus()
        page.keyboard.press('Enter')
        assert page.locator('#q-dialog').evaluate('(e)=>e.contains(document.activeElement)')
        page.keyboard.press('Escape')
        assert page.locator('#q-create').evaluate('(e)=>e===document.activeElement')
        browser.close()
    report = dict(routes=results, page_errors=errors, console_errors=consoles, external=external)
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    assert not errors and not consoles and not external, report
    assert all(r['status'] == 200 and not r['overflow'] and r['heading']
               and not r['unnamed'] and not r['duplicates'] for r in results), results


def main():
    output = REPO / '.devdata' / 'phase8o-atlas'
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='pullarr-ui-', dir=output) as directory, source_fixture() as (source, _):
        base = Path(directory)
        (base / 'downloads').mkdir()
        set_db_location(str(base / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key': 'disposable-pullarr-ui', 'download_folder': str(base / 'downloads'),
                               'db_backup_folder': str(base / 'db')})
            DownloadClients.trigger_client_registration()
            seed(get_db(), base)
            get_db().executemany(
                'INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder,monitored) '
                'VALUES(?,?,?,2020,1,1,?,1)',
                [(i, 10000+i, f'Demo publication {i:04}', str(base / 'library' / str(i)))
                 for i in range(2, 1001)]
            )
            get_db().executemany('INSERT INTO volumes_covers(volume_id,cover) VALUES(?,NULL)',
                                 [(i,) for i in range(2, 1001)])
            RootFolders()._RootFolders__get_folder_mapping.cache_clear()
            get_db().connection.commit()
        server.app.extensions['discover'].transport = source
        http = make_server('127.0.0.1', 0, server.app, threaded=True)
        Thread(target=http.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{http.server_port}'
        try:
            capture(origin, output)
            with server.app.app_context():
                Settings().update({'auth_password': 'synthetic-pullarr-login'})
                get_db().connection.commit()
            standalone(origin, output)
        finally:
            http.shutdown()
            UNIFIED_SEARCH.close_all()


def standalone(origin, output):
    """Real password authentication, fresh storage, 404 and redirect coverage."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(origin + '/login')
        page.locator('#password-input').wait_for(state='visible')
        assert page.title() == 'Sign in · Pullarr'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(output / '390-login.png'), full_page=True)
        page.locator('#password-input').fill('synthetic-pullarr-login')
        page.keyboard.press('Enter')
        page.wait_for_url(origin + '/')
        page.locator('main h1').wait_for()
        page.locator('#library-next').click()
        page.wait_for_function("document.querySelector('#library-page').textContent.startsWith('Page 2')")
        assert page.locator('#list-library .list-entry').count() == 50
        page.locator('#library-previous').focus()
        page.keyboard.press('Enter')
        page.wait_for_function("document.querySelector('#library-page').textContent.startsWith('Page 1')")
        page.goto(origin + '/settings')
        assert page.url.endswith('/settings/mediamanagement')
        manifest = page.request.get(origin + '/manifest.json').json()
        assert manifest['name'] == 'Pullarr'
        for width in (1440, 390, 720):
            page.set_viewport_size({'width': width, 'height': 1000})
            response = page.goto(origin + '/no-such-pullarr-page')
            assert response.status == 404
            assert page.locator('main h1').inner_text() == 'Page not found'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'{width}-not-found.png'), full_page=True)
        # 720 CSS pixels is the reflow equivalent of 1440px at 200% zoom.
        for path in ROUTES:
            page.goto(origin + path)
            page.wait_for_timeout(150)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), path
        assert not errors, errors
        browser.close()
    print('Standalone login/404/manifest/settings redirect and 200% equivalent reflow: PASS')


if __name__ == '__main__':
    if len(sys.argv) == 2:
        target = REPO / '.devdata' / 'phase8o-container-atlas'
        target.mkdir(parents=True, exist_ok=True)
        capture(sys.argv[1], target)
    else:
        main()

"""Real Chromium/HTTP/TaskHandler acceptance, disposable library and sources."""

import json
import logging
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests')]

from fixtures.reading_orders import CBL, FixtureConnection, FixtureLists
from fixtures.release_calendar import (CalendarFixture,
                                       CalendarGCD, CalendarMetron)
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.logging import LOGGER
from backend.base.reading_orders import export_cbl, parse_cbl
from backend.features.reading_orders import ReadingOrders
from backend.implementations.reading_order_sources import CBLFetcher
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    errors, consoles, records, metrics = [], [], [], {}
    expected_console = []
    expiry_test = {'active': False}
    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                records.append(record.getMessage())
    capture = Capture(); LOGGER.addHandler(capture)
    with TemporaryDirectory(prefix='kapowarr-8l-browser-') as directory, patch.dict(
        'backend.implementations.metadata.registry.PROVIDERS', comicvine=CalendarFixture, metron=CalendarMetron, gcd=CalendarGCD):
        base = Path(directory); library = base / 'library'; library.mkdir()
        set_db_location(str(base / 'db'))
        server = Server(); server.app.logger.addHandler(capture)
        server.app.extensions['reading_orders'] = ReadingOrders(fetcher=CBLFetcher(resolver=lambda h: ['93.184.216.34'], connection=FixtureConnection), provider=FixtureLists())
        with server.app.app_context():
            setup_db(); Settings().update({'api_key': 'disposable-reading-orders-key'})
            db = get_db(); db.execute('INSERT INTO root_folders VALUES(1,?)', (str(library),))
            for identity, cv, title in ((1, 100, 'One'), (2, 200, 'Two'), (3, 300, 'One')):
                folder = library / str(identity); folder.mkdir()
                db.execute('INSERT INTO volumes(id,comicvine_id,title,year,root_folder,folder,monitored) VALUES(?,?,?,2026,1,?,1)', (identity, cv, title, str(folder)))
                db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(?,NULL)', (identity,))
            for identity, volume, cv, number in ((1,1,101,'1'), (2,1,102,'2'), (3,1,103,'3'), (4,2,201,'Annual'), (5,3,301,'3')):
                db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(?,?,?,?,0)', (identity,volume,cv,number))
            comic = library / '1/owned.cbz'; comic.write_bytes(b'fixture exact issue bytes')
            db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(comic), comic.stat().st_size))
            db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
            db.connection.commit()
            preview = claim_preview(db, 1, PublicationRef('comicvine', '103'), ClaimKind.COMPLETE, manual=True)
            claim = confirm_claim(db, 1, PublicationRef('comicvine', '103'), ClaimKind.COMPLETE, preview['preview_token'], manual=True)
            coverage = coverage_preview(db, 1, 1, [claim])
            apply_coverage(db, 1, 1, [claim], coverage['preview_token'])
            db.connection.commit()
        http = make_server('127.0.0.1', 0, server.app, threaded=True)
        Thread(target=http.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{http.server_port}'
        def api(method, path, body=None):
            response = requests.request(method, origin+'/api'+path, params={'api_key':'disposable-reading-orders-key'}, json=body, timeout=30)
            assert response.status_code in (200,201), (path, response.status_code, response.text)
            return response.json()['result']
        try:
            asset = requests.get('https://cdn.socket.io/4.7.5/socket.io.min.js', timeout=20); asset.raise_for_status()
            with sync_playwright() as p:
                browser = p.chromium.launch(); context = browser.new_context()
                def route(r):
                    if r.request.url.startswith(origin): r.continue_()
                    elif 'socket.io.min.js' in r.request.url: r.fulfill(content_type='text/javascript', body=asset.content, headers={'Access-Control-Allow-Origin':'*'})
                    else: r.fulfill(status=200, body='')
                context.route('**/*', route)
                context.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'disposable-reading-orders-key',last_login:Date.now()/1000,theme:'dark'}));")
                page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
                def console_message(message):
                    if message.type == 'error':
                        (expected_console if expiry_test['active'] and '410' in message.text else consoles).append(message.text)
                page.on('console', console_message)
                started = time.perf_counter(); page.goto(origin+'/reading-orders'); page.get_by_text('No Reading Orders yet.', exact=False).wait_for()
                metrics['initial_seconds'] = round(time.perf_counter()-started, 3)
                dialog = page.locator('#ro-dialog')
                page.get_by_role('button', name='Create Reading Order', exact=True).focus(); page.keyboard.press('Enter')
                assert page.evaluate("document.getElementById('ro-dialog').contains(document.activeElement)")
                page.keyboard.press('Escape'); assert not dialog.is_visible()
                page.get_by_role('button', name='Create Reading Order', exact=True).click(); page.get_by_label('Title', exact=True).fill('Manual Cross-Series')
                dialog.get_by_role('button', name='Create', exact=True).evaluate('(e)=>{e.click();e.click();}'); page.get_by_role('heading', name='Manual Cross-Series').wait_for()
                assert len(api('GET','/reading-orders')['items'])==1
                for title in ('One #1 (2026)', 'Two #Annual (2026)', 'One #1 (2026)'):
                    page.get_by_role('button', name='Add Local Issue', exact=True).click(); page.get_by_label('Search local series / issue').fill(title.split(' #')[0]); dialog.get_by_role('button', name='Search Local Issues').click(); dialog.get_by_role('button', name=title, exact=True).click(); dialog.wait_for(state='hidden')
                rows = page.locator('#ro-content tbody tr'); assert rows.count()==3
                rows.nth(1).get_by_role('button', name='Move Up').click(); page.wait_for_function("document.querySelector('#ro-content tbody tr').textContent.includes('Two #Annual')")
                page.reload(); page.wait_for_function("document.querySelector('#ro-content tbody tr')?.textContent.includes('Two #Annual')")
                manual = api('GET','/reading-orders')['items'][0]; ordered = api('GET',f"/reading-orders/{manual['id']}/entries")
                assert [r['canonical_id'] for r in ordered['items']]==[4,1,1]
                page.get_by_role('button', name='Import CBL', exact=True).click(); page.get_by_label('CBL file').set_input_files({'name':'fixture.cbl','mimeType':'application/xml','buffer':CBL}); dialog.get_by_role('button', name='Parse and Review').click()
                page.get_by_role('heading', name='Import Review:', exact=False).wait_for(); assert 'ambiguous' in page.locator('#ro-content').inner_text()
                page.get_by_role('button', name='Resolve Entry 5', exact=True).click(); page.get_by_label('Search local series / issue').fill('One 3'); dialog.get_by_role('button', name='Search Local Issues').click(); dialog.get_by_role('button', name='One #3 (2026)').first.click(); dialog.wait_for(state='hidden')
                page.get_by_role('button', name='Accept Reading Order', exact=True).click(); dialog.get_by_role('button', name='Accept Reading Order', exact=True).click(); dialog.wait_for(state='hidden')
                imported = api('GET','/reading-orders')['items'][0]; oid=imported['id']
                data = api('GET',f'/reading-orders/{oid}/entries'); assert len(data['items'])==7
                assert data['items'][0]['status']=='owned' and data['items'][1]['status']=='missing' and data['items'][2]['content_elsewhere']
                assert data['items'][3]['status']=='external'; assert page.evaluate('window.readingOrderHostile') is None
                with page.expect_download() as download:
                    page.get_by_role('button', name='Export CBL', exact=True).click()
                exported = Path(download.value.path()).read_bytes()
                assert len(parse_cbl(exported)['entries'])==7
                page.get_by_role('button', name='Import CBL', exact=True).click(); page.get_by_label('CBL file').set_input_files({'name':'roundtrip.cbl','mimeType':'application/xml','buffer':exported}); dialog.get_by_role('button', name='Parse and Review').click(); page.get_by_role('button', name='Accept Reading Order', exact=True).click(); dialog.get_by_role('button', name='Accept Reading Order', exact=True).click(); dialog.wait_for(state='hidden')
                assert api('GET','/reading-orders')['items'][0]['entry_count']==7
                for number in (1,2,3,4,6): page.get_by_label(f'Select entry {number}', exact=True).check()
                page.get_by_role('button', name='Send Missing to Wanted', exact=True).click(); dialog.get_by_text('content represented', exact=False).wait_for()
                ready = dialog.get_by_role('checkbox'); assert ready.count()==1; ready.check()
                dialog.get_by_role('button', name='Enable Selected Issues for Wanted', exact=True).click(); dialog.wait_for(state='hidden')
                with server.app.app_context():
                    assert get_db().execute('SELECT monitored FROM issues WHERE id=2').fetchone()[0]==1
                    assert get_db().execute('SELECT count(*) FROM wanted_searches').fetchone()[0]==0
                page.get_by_role('button', name='Add Publication to Library', exact=True).click(); dialog.get_by_role('button', name='Add Publication', exact=True).click(); dialog.wait_for(state='hidden', timeout=60000)
                assert 'External #1' not in page.locator('#ro-content').inner_text()
                page.get_by_role('button', name='Subscribe to CBL URL', exact=True).click(); page.get_by_label('Public HTTPS CBL URL').fill('https://example.com/fixture.cbl'); dialog.get_by_role('button', name='Attach Subscription').click(); dialog.wait_for(state='hidden')
                page.get_by_role('button', name='Refresh Source', exact=True).click(); page.wait_for_function("document.getElementById('ro-message').textContent.startsWith('Operation complete')")
                page.get_by_role('button', name='Review Source Changes', exact=True).click(); page.get_by_role('button', name='Accept Update', exact=True).click(); dialog.get_by_role('button', name='Accept Update', exact=True).click(); dialog.wait_for(state='hidden')
                source_order = api('GET','/reading-orders')['items'][0]; source_id = api('GET',f"/reading-orders/{source_order['id']}")['source']['id']
                before = api('GET',f"/reading-orders/{source_order['id']}/entries")['items']
                model = parse_cbl(CBL); model['entries'] = [model['entries'][3], model['entries'][0], model['entries'][1]]; FixtureConnection.data=export_cbl(model)
                page.get_by_role('button', name='Refresh Source', exact=True).click(); page.wait_for_function("document.getElementById('ro-message').textContent.startsWith('Operation complete')")
                assert before==api('GET',f"/reading-orders/{source_order['id']}/entries")['items']
                page.get_by_role('button', name='Review Source Changes', exact=True).click(); page.get_by_role('heading', name='Source Change Review').wait_for(); assert 'removed' in page.locator('#ro-content').inner_text(); assert 'moved' in page.locator('#ro-content').inner_text()
                page.get_by_role('button', name='Accept Update', exact=True).click(); dialog.get_by_role('button', name='Accept Update', exact=True).click(); dialog.wait_for(state='hidden')
                assert len(api('GET',f"/reading-orders/{source_order['id']}/entries")['items'])==3
                page.get_by_role('button', name='Refresh Source', exact=True).click(); page.wait_for_function("document.getElementById('ro-message').textContent.startsWith('Operation complete')")
                assert not api('GET',f'/reading-orders/sources/{source_id}/pending')['pending']
                page.get_by_role('button', name='Find Provider Reading List', exact=True).click(); page.get_by_label('Metron list name').fill('fixture'); dialog.get_by_role('button', name='Search Metron Lists').click(); dialog.get_by_role('button', name='Review Fixture List', exact=False).click(timeout=60000); page.get_by_role('button', name='Accept Reading Order', exact=True).click(); dialog.get_by_role('button', name='Accept Reading Order', exact=True).click(); dialog.wait_for(state='hidden'); page.get_by_role('heading', name='Provider Ordered Fixture').wait_for()
                page.get_by_role('button', name='Import CBL', exact=True).click(); page.get_by_label('CBL file').set_input_files({'name':'expiry.cbl','mimeType':'application/xml','buffer':CBL}); dialog.get_by_role('button', name='Parse and Review').click(); page.get_by_role('heading', name='Import Review:', exact=False).wait_for()
                server.app.extensions['reading_orders'] = ReadingOrders(fetcher=CBLFetcher(resolver=lambda h: ['93.184.216.34'], connection=FixtureConnection), provider=FixtureLists())
                expiry_test['active']=True
                page.reload(); page.get_by_text('This review expired or the application restarted.',exact=False).wait_for()
                expiry_test['active']=False
                page.get_by_role('button',name='All Reading Orders',exact=True).click(); page.get_by_role('button',name='Open Provider Ordered Fixture',exact=True).click(); page.get_by_role('heading',name='Provider Ordered Fixture').wait_for()
                page.set_viewport_size({'width':540,'height':820}); assert page.locator('.ro-table').evaluate('(e)=>e.clientWidth<=window.innerWidth')
                page.get_by_role('button', name='Detach from Source', exact=True).focus(); page.keyboard.press('Enter'); assert dialog.is_visible(); page.keyboard.press('Escape'); assert not dialog.is_visible()
                page.set_viewport_size({'width':1280,'height':900})
                for route_path in ('/', '/volumes/1', '/add', '/activity/queue', '/settings/metadata', '/library-import', '/volumes/1/provider-switch', '/wanted', '/collections', '/calendar', '/maintenance'):
                    response=page.goto(origin+route_path); assert response.status==200, route_path
                page.goto(origin+'/reading-orders'); page.get_by_role('heading', name='Provider Ordered Fixture').wait_for()
                assert not errors and not consoles, (errors,consoles)
                browser.close()
            assert not records, records
            print(json.dumps(dict(workflows=['manual/repeat/reorder/reload/double-submit','CBL import/resolve/export/reimport','Wanted/C2 separation','external Add','subscription diff/accept/unchanged','provider ordered list','transient restart/durable rediscovery','hostile text','keyboard/dialog','narrow viewport','existing UI smoke'], errors=errors, console_errors=consoles, server_errors=records, expected_expiry_responses=expected_console, metrics=metrics)))
        finally:
            http.shutdown(); LOGGER.removeHandler(capture)


if __name__ == '__main__':
    main()

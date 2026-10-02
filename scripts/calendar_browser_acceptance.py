"""Real HTTP/Chromium/TaskHandler acceptance with deterministic metadata sources."""

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

from fixtures.release_calendar import (CalendarFixture,
                                       CalendarGCD, CalendarMetron)
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.logging import LOGGER
from backend.internals.collections import CollectionStore
from backend.internals.content_claims import claim_preview, confirm_claim
from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    errors, consoles, records, metrics = [], [], [], {}
    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                records.append(record.getMessage())
    capture = Capture()
    LOGGER.addHandler(capture)
    with TemporaryDirectory(prefix='kapowarr-8k-browser-') as directory, patch.dict(
        'backend.implementations.metadata.registry.PROVIDERS', comicvine=CalendarFixture, metron=CalendarMetron, gcd=CalendarGCD):
        base = Path(directory)
        library = base / 'library'
        (library / 'Local').mkdir(parents=True)
        set_db_location(str(base / 'db'))
        server = Server()
        server.app.logger.addHandler(capture)
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key': 'disposable-calendar-browser-key'})
            db = get_db()
            db.execute('INSERT INTO root_folders VALUES(1,?)', (str(library),))
            db.execute("INSERT INTO volumes(id,comicvine_id,title,year,root_folder,folder,monitored,monitor_new_issues) VALUES(1,100,'Local',2027,1,?,1,1)", (str(library / 'Local'),))
            db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
            db.connection.commit()
            store = CollectionStore(db)
            tree = store.create('Calendar <script>window.calendarHostile=true</script>', monitoring='monitored')
            node = tree['nodes'][0]['id']
            publication_ids = {}
            for identity in ('900', '910', '920', '930', '940'):
                store.propose(node, [dict(provider='comicvine', provider_id=identity, title='External ' + identity, year=2027)], 'fixture search')
                suggestion = next(s for s in store.suggestions(node)['items'] if s['provider_id'] == identity)
                if identity != '940':
                    publication_ids[identity] = store.decide(suggestion['id'], 0, store.tree(tree['id'])['revision'], 'accepted')['publication_id']
            # Exact accepted additional reference, no fuzzy cross-provider matching.
            db.execute("INSERT INTO collection_publication_refs(publication_id,provider,provider_id,source) VALUES(?,'metron','950','exact_local_identity')", (publication_ids['900'],))
            db.connection.commit()
        http = make_server('127.0.0.1', 0, server.app, threaded=True)
        thread = Thread(target=http.serve_forever, daemon=True); thread.start()
        origin = f'http://127.0.0.1:{http.server_port}'
        def api(method, path, body=None):
            response = requests.request(method, origin + '/api' + path, params={'api_key': 'disposable-calendar-browser-key'}, json=body, timeout=30)
            assert response.status_code in (200, 201), (path, response.status_code, response.text)
            return response.json()['result']
        def wait_task():
            for _ in range(120):
                if not api('GET', '/system/tasks'):
                    return
                time.sleep(.1)
            raise AssertionError('TaskHandler did not settle')
        try:
            api('POST', '/system/tasks', {'cmd': 'refresh_and_scan', 'volume_id': 1})
            wait_task()
            with server.app.app_context():
                db = get_db()
                assert db.execute('SELECT count(*) FROM issues WHERE volume_id=1').fetchone()[0] == 1
                comic = library / 'Local' / 'fixture.cbz'
                comic.write_bytes(b'unchanged existing constituent content')
                db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(comic), comic.stat().st_size))
                local_issue = db.execute('SELECT id FROM issues WHERE volume_id=1').fetchone()[0]
                db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,?)', (local_issue,))
                db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(2,900,'Historical omnibus',1,?)", (str(library / 'Historical'),))
                db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number) VALUES(2,2,901,'1')")
                db.connection.commit()
                preview = claim_preview(db, 2, PublicationRef('comicvine', '101'), ClaimKind.COMPLETE, manual=True)
                confirm_claim(db, 2, PublicationRef('comicvine', '101'), ClaimKind.COMPLETE, preview['preview_token'], manual=True)
                db.execute("INSERT INTO bibliographic_graph_snapshots VALUES('fixture','comicvine','fixture','fixture','fixture',0,0,0,0,0,0,'fixture')")
                db.execute("INSERT INTO bibliographic_issue_refs VALUES('comicvine','901','900','External','1','',0,'fixture')")
                db.execute('DELETE FROM volumes WHERE id=2')
                db.connection.commit()
                before_monitor = [tuple(r) for r in db.execute('SELECT id,monitored FROM volumes')]
            CalendarMetron.dates = dict(CalendarFixture.dates, **{'950': '2027-03-26'})
            # TBA fixture must not have a cover date to fall back to.
            asset = requests.get('https://cdn.socket.io/4.7.5/socket.io.min.js', timeout=20)
            asset.raise_for_status()
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context()
                def route(r):
                    if r.request.url.startswith(origin):
                        r.continue_()
                    elif 'socket.io.min.js' in r.request.url:
                        r.fulfill(content_type='text/javascript', body=asset.content, headers={'Access-Control-Allow-Origin': '*'})
                    else:
                        r.fulfill(status=200, body='')
                context.route('**/*', route)
                context.add_init_script("localStorage.setItem('kapowarr', JSON.stringify({api_key:'disposable-calendar-browser-key',last_login:Date.now()/1000,theme:'dark'}));")
                page = context.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('console', lambda m: consoles.append(m.text) if m.type == 'error' else None)
                started = time.perf_counter()
                page.goto(origin + '/calendar')
                page.get_by_label('Horizon').select_option('custom')
                page.get_by_label('From', exact=True).fill('2027-01-01')
                page.get_by_label('To', exact=True).fill('2027-12-31')
                page.get_by_role('button', name='Show releases').click()
                page.locator('article').first.wait_for()
                assert '2027-03-17' in page.locator('#calendar-results').inner_text()
                metrics['initial_local_seconds'] = round(time.perf_counter() - started, 3)
                page.get_by_role('button', name='Refresh Calendar', exact=True).click()
                page.wait_for_function("document.getElementById('calendar-message').textContent.includes('refresh: complete')")
                external = page.locator(f'article[data-event-id="publication:{publication_ids["900"]}"]')
                external.get_by_text('Not in Library', exact=False).wait_for()
                assert 'month precision' in page.locator('#calendar-results').inner_text()
                assert 'year precision' in page.locator('#calendar-results').inner_text()
                assert '940' not in page.locator('#calendar-results').inner_text()
                external.get_by_role('button', name='Release details').focus()
                page.keyboard.press('Enter')
                dialog = page.locator('#calendar-dialog')
                dialog.wait_for(state='visible')
                assert page.evaluate("document.getElementById('calendar-dialog').contains(document.activeElement)")
                assert '2027-03-24' in dialog.inner_text() and '2027-03-26' in dialog.inner_text()
                assert 'complete known content' in dialog.inner_text()
                assert external.get_by_text('Not in Library', exact=False).count() == 1
                page.keyboard.press('Escape')
                dialog.wait_for(state='hidden')
                assert external.get_by_role('button', name='Release details').evaluate('(e) => e === document.activeElement')
                # Date movement updates one stable subject; other provider evidence survives.
                CalendarFixture.dates['900'] = '2027-03-25'
                page.get_by_role('button', name='Refresh Calendar', exact=True).click()
                page.wait_for_function("!document.getElementById('calendar-refresh').disabled")
                assert external.count() == 1
                external.get_by_role('button', name='Release details').click()
                dialog.wait_for(state='visible')
                assert '2027-03-24' in dialog.inner_text()
                dialog.get_by_role('button', name='Add to Library', exact=True).click()
                dialog.get_by_role('button', name='Confirm Add to Library').click()
                dialog.wait_for(state='hidden', timeout=30000)
                external.get_by_text('In Library', exact=False).wait_for()
                assert external.count() == 1
                # Controlled provider failure retains local/other-provider observations.
                CalendarMetron.failed = True
                page.get_by_role('button', name='Refresh Calendar', exact=True).click()
                page.wait_for_function("document.getElementById('calendar-message').textContent.includes('refresh: partial')")
                CalendarMetron.failed = False
                assert external.count() == 1
                # Monitoring edits belong to Collections and must not mutate library/Wanted.
                with server.app.app_context():
                    db = get_db()
                    unchanged = {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in
                        ('wanted_decisions', 'wanted_schedule', 'wanted_searches')}
                    monitoring = [tuple(r) for r in db.execute('SELECT id,monitored FROM volumes')]
                current_tree = api('GET', '/collections/' + str(tree['id']))
                root_node = current_tree['nodes'][0]
                for state in ('unmonitored', 'monitored'):
                    current_tree = api('POST', f'/collections/{tree["id"]}/nodes', dict(revision=current_tree['revision'],
                        node_id=node, title=root_node['title'], description=root_node['description'], kind=root_node['kind'],
                        monitoring=state, parent_id=None, position=0))
                    page.get_by_role('button', name='Show releases').click()
                    page.wait_for_function('(count) => document.querySelectorAll("#calendar-results article").length === count', arg=1 if state == 'unmonitored' else 4)
                with server.app.app_context():
                    db = get_db()
                    assert unchanged == {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in unchanged}
                    assert monitoring == [tuple(r) for r in db.execute('SELECT id,monitored FROM volumes')]
                page.get_by_label('Horizon').select_option('unknown')
                page.get_by_role('button', name='Show releases').click()
                page.get_by_text('TBA / Date Unknown — no usable release date', exact=True).first.wait_for()
                assert not page.evaluate('Boolean(window.calendarHostile)')
                page.set_viewport_size({'width': 650, 'height': 850})
                page.locator('article').first.get_by_role('button', name='Release details').click()
                dialog.wait_for(state='visible')
                assert dialog.is_visible()
                assert dialog.bounding_box()['width'] <= 650
                page.keyboard.press('Escape')
                for path in ('/', '/volumes/1', '/add', '/activity/queue', '/settings/mediamanagement', '/library-import', '/collections', '/maintenance', '/volumes/1/provider-switch', '/wanted', '/calendar'):
                    response = page.goto(origin + path)
                    assert response.status == 200, path
                    page.wait_for_timeout(100)
                metrics['workflow_seconds'] = round(time.perf_counter() - started, 3)
                assert not errors, errors
                assert not consoles, consoles
                browser.close()
            with server.app.app_context():
                db = get_db()
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
                assert before_monitor == [tuple(r) for r in db.execute('SELECT id,monitored FROM volumes WHERE id=1')]
                assert db.execute('SELECT count(*) FROM organization_jobs').fetchone()[0] == 0
            assert not records, records
            print(json.dumps(dict(result='pass', workflows=['canonical_refresh', 'external_sync', 'partial_dates', 'date_movement',
                'source_conflict', 'pending_excluded', 'c2_separation', 'normal_add_volume', 'partial_provider_failure', 'monitoring_separation', 'hostile_text', 'keyboard', 'narrow', 'UI_smoke'],
                browser_errors=errors, console_errors=consoles, server_errors=records, diagnostics=metrics)))
        except BaseException:
            print(json.dumps(dict(browser_errors=errors, console_errors=consoles, server_errors=records)))
            raise
        finally:
            http.shutdown(); thread.join(); LOGGER.removeHandler(capture); server.app.logger.removeHandler(capture)


if __name__ == '__main__':
    main()

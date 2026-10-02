"""Disposable real Chromium/HTTP/TaskHandler Collections acceptance."""

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

from fixtures.collections import FixtureProvider, GCDFixture, MetronFixture
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.logging import LOGGER
from backend.features.collections import Collections
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
    with TemporaryDirectory(prefix='kapowarr-8j-browser-') as directory, patch.dict(
            'backend.implementations.metadata.registry.PROVIDERS',
            comicvine=FixtureProvider, metron=MetronFixture, gcd=GCDFixture):
        base = Path(directory)
        library = base / 'library'
        (library / 'Batman').mkdir(parents=True)
        set_db_location(str(base / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key': 'disposable-collections-browser-key'})
            db = get_db()
            db.execute('INSERT INTO root_folders VALUES(1,?)', (str(library),))
            db.execute("INSERT INTO volumes(id,comicvine_id,title,year,root_folder,folder,monitored) VALUES(1,100,'Batman',2016,1,?,0)", (str(library / 'Batman'),))
            comic = library / 'Batman' / 'fixture.cbz'
            comic.write_bytes(b'unchanged constituent-content fixture')
            db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(1,1,101,'1',0)")
            db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(comic), comic.stat().st_size))
            db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
            db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(2,900,'Historical collected edition',1,?)", (str(library / 'Historical'),))
            db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number) VALUES(2,2,901,'1')")
            db.connection.commit()
            preview = claim_preview(db, 2, PublicationRef('comicvine', '101'), ClaimKind.COMPLETE, manual=True)
            confirm_claim(db, 2, PublicationRef('comicvine', '101'), ClaimKind.COMPLETE, preview['preview_token'], manual=True)
            db.execute("INSERT INTO bibliographic_graph_snapshots VALUES('fixture','comicvine','fixture','fixture','fixture',0,0,0,0,0,0,'fixture')")
            db.execute("INSERT INTO bibliographic_issue_refs VALUES('comicvine','901','900','External 900','1','',0,'fixture')")
            db.execute('DELETE FROM volumes WHERE id=2')
            db.connection.commit()
        http = make_server('127.0.0.1', 0, server.app, threaded=True)
        thread = Thread(target=http.serve_forever, daemon=True); thread.start()
        origin = f'http://127.0.0.1:{http.server_port}'
        def api(method, path, body=None):
            response = requests.request(method, origin + '/api' + path, params={'api_key': 'disposable-collections-browser-key'}, json=body, timeout=30)
            assert response.status_code == 200, (path, response.status_code, response.text)
            return response.json()['result']
        try:
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
                context.add_init_script("localStorage.setItem('kapowarr', JSON.stringify({api_key:'disposable-collections-browser-key',last_login:Date.now()/1000,theme:'dark'}));")
                page = context.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('console', lambda m: consoles.append(m.text) if m.type == 'error' else None)
                started = time.perf_counter()
                page.goto(origin + '/collections')
                page.get_by_role('button', name='Create Collection', exact=True).focus()
                page.keyboard.press('Enter')
                dialog = page.locator('#collections-dialog')
                dialog.get_by_label('Title', exact=True).fill('Batman')
                assert page.evaluate("document.getElementById('collections-dialog').contains(document.activeElement)")
                dialog.get_by_role('button', name='Save Collection structure').click()
                dialog.wait_for(state='hidden')
                page.get_by_role('button', name='Add subgroup', exact=True).click()
                dialog.get_by_label('Title', exact=True).fill('Omnibuses')
                dialog.get_by_role('button', name='Save Collection structure').click()
                dialog.wait_for(state='hidden')
                page.locator('#collections-tree').get_by_role('button', name='Omnibuses (inherit)', exact=True).click()
                page.get_by_role('button', name='Search local library', exact=True).click()
                page.locator('#collections-local-results').get_by_role('button', name='Batman', exact=False).click()
                dialog.get_by_role('button', name='Add membership', exact=True).click()
                dialog.wait_for(state='hidden')
                page.locator('#collections-publications').get_by_text('In Library', exact=True).wait_for()
                page.get_by_role('button', name='Edit membership / kind').click()
                dialog.get_by_label('Action', exact=True).select_option('move')
                dialog.get_by_label('Target subgroup', exact=True).select_option(label='Batman')
                dialog.get_by_role('button', name='Save membership').click()
                dialog.wait_for(state='hidden')
                page.reload()
                page.locator('#collections-publications').get_by_text('In Library', exact=True).wait_for()
                metrics['manual_hierarchy_seconds'] = round(time.perf_counter() - started, 3)
                page.locator('#collections-tree').get_by_role('button', name='Omnibuses (inherit)', exact=True).click()
                page.get_by_label('Publication title or qualified ID').fill('Batman omnibus')
                page.get_by_label('Save results as pending search-based suggestions').check()
                page.get_by_role('button', name='Find Publications', exact=True).click()
                page.locator('#collections-search-results').get_by_text('Task complete', exact=True).wait_for()
                assert 'comicvine' in page.locator('#collections-search-results').inner_text()
                assert 'metron' in page.locator('#collections-search-results').inner_text()
                assert 'gcd' in page.locator('#collections-search-results').inner_text()
                row = page.locator('#collections-suggestions tbody tr').filter(has_text='comicvine:900')
                row.get_by_role('button', name='Review / Accept').click()
                dialog.get_by_role('button', name='Accept membership').click()
                dialog.wait_for(state='hidden')
                page.locator('#collections-publications').get_by_text('Not in Library', exact=True).wait_for()
                assert 'complete known content' in page.locator('#collections-publications').inner_text()
                reject = page.locator('#collections-suggestions tbody tr').filter(has_text='comicvine:910')
                reject.get_by_role('button', name='Reject', exact=True).click()
                dialog.get_by_role('button', name='Reject', exact=True).click(); dialog.wait_for(state='hidden')
                page.get_by_role('button', name='Find Publications', exact=True).click()
                page.locator('#collections-search-results').get_by_text('Task complete', exact=True).wait_for()
                assert page.locator('#collections-suggestions tbody tr').filter(has_text='comicvine:910').count() == 0
                page.get_by_label('Decision', exact=True).select_option('rejected')
                page.locator('#collections-suggestions').get_by_role('button', name='Reconsider').click()
                dialog.get_by_role('button', name='Reconsider').click(); dialog.wait_for(state='hidden')
                page.get_by_label('Decision', exact=True).select_option('pending')
                page.locator('#collections-suggestions tbody tr').filter(has_text='comicvine:910').wait_for()
                page.locator('#collections-publications').get_by_role('button', name='Add to Library', exact=True).click()
                dialog.get_by_role('button', name='Add to Library', exact=True).click()
                dialog.wait_for(state='hidden', timeout=30000)
                page.locator('#collections-publications').get_by_text('In Library', exact=True).wait_for()
                # Normal pipeline created precisely one volume and did not enable monitoring/search.
                with server.app.app_context():
                    db = get_db()
                    assert db.execute('SELECT count(*) FROM volumes').fetchone()[0] == 2
                    assert not db.execute('SELECT 1 FROM volumes WHERE monitored=1').fetchone()
                    assert not db.execute('SELECT 1 FROM issues WHERE monitored=1').fetchone()
                    assert db.execute('SELECT count(*) FROM collection_publications').fetchone()[0] == 2
                page.get_by_role('button', name='Edit / move subgroup', exact=True).click()
                dialog.get_by_label('Collection monitoring').select_option('monitored')
                dialog.get_by_role('button', name='Save Collection structure').click(); dialog.wait_for(state='hidden')
                assert len(api('GET', '/collections/discovery')['items']) == 1
                # Actual switch API and current browser projection, not simulated Collection linkage.
                from TProviderSwitchApply import remote

                from backend.implementations.metadata.switch_target import \
                    admit
                async def acquire(reference):
                    return admit(remote(reference.provider, reference.provider_id, first=701, count=1), reference)
                server.app.extensions['provider_switch_reviews'].acquire = acquire
                with server.app.app_context():
                    db = get_db()
                    linked = db.execute("SELECT volume_id FROM volume_external_ids WHERE provider='comicvine' AND provider_id='900'").fetchone()[0]
                    issue = db.execute('SELECT id FROM issues WHERE volume_id=?', (linked,)).fetchone()[0]
                    memberships = [tuple(r) for r in db.execute('SELECT * FROM collection_memberships')]
                review = api('POST', '/provider-switch/reviews', dict(volume_id=linked, provider='metron', provider_id='700'))
                review = api('PUT', '/provider-switch/reviews/' + review['session_id'], dict(revision=review['revision'], mappings=[dict(local_issue_id=issue, target_provider_id='701')]))
                api('POST', '/provider-switch/reviews/' + review['session_id'] + '/apply', dict(revision=review['revision'],
                    mapping_digest=review['preview']['mapping_digest'], source_authority=review['source_authority'], confirmed=True))
                with server.app.app_context():
                    assert memberships == [tuple(r) for r in get_db().execute('SELECT * FROM collection_memberships')]
                server.app.extensions['collections'] = Collections()
                page.reload()
                page.locator('#collections-publications').get_by_text('In Library', exact=True).first.wait_for()
                assert 'Target metron' in page.locator('#collections-publications').inner_text()
                assert not page.evaluate('Boolean(window.hostile)')
                page.set_viewport_size({'width': 680, 'height': 850})
                page.get_by_role('button', name='Add subgroup').click()
                assert dialog.is_visible()
                page.keyboard.press('Escape'); dialog.wait_for(state='hidden')
                assert page.get_by_role('button', name='Add subgroup').evaluate('(e) => e === document.activeElement')
                metrics['complete_workflow_seconds'] = round(time.perf_counter() - started, 3)
                assert not errors, errors
                assert not consoles, consoles
                browser.close()
            with server.app.app_context():
                db = get_db()
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
                assert db.execute('SELECT count(*) FROM organization_jobs').fetchone()[0] == 0
            assert not records, records
            print(json.dumps(dict(result='pass', task_handler='real', providers='synthetic CV/Metron/GCD',
                workflows=['hierarchy', 'membership_move', 'reload', 'provider_search', 'accept', 'reject_refresh_reconsider',
                           'normal_add_volume', 'c2_separate', 'provider_switch', 'monitoring', 'service_restart', 'hostile_text', 'keyboard', 'narrow_viewport'],
                browser_errors=errors, console_errors=consoles, server_errors=records, diagnostics=metrics)))
        except BaseException:
            print(json.dumps(dict(browser_errors=errors, console_errors=consoles, server_errors=records)))
            raise
        finally:
            http.shutdown(); thread.join(); LOGGER.removeHandler(capture)


if __name__ == '__main__':
    main()

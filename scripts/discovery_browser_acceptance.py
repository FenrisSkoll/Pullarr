"""Real HTTP/Server/TaskHandler and Chromium with synthetic GetComics/DDL."""

import json
import logging
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

import requests

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO),str(REPO/'tests')]

from fixtures.collections import FixtureProvider
from fixtures.discovery import seed, source_fixture
from playwright.sync_api import sync_playwright
from TProviderSwitchApply import remote
from werkzeug.serving import make_server

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.logging import LOGGER
from backend.features.intake_runtime import IntakeRuntime
from backend.features.wanted_automation import WantedAutomation
from backend.features.wanted_search import UNIFIED_SEARCH
from backend.implementations.download_client_manager import DownloadClients
from backend.implementations.metadata.switch_target import admit
from backend.implementations.root_folders import RootFolders
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.db import (DBConnection, get_db,
                                  set_db_location, setup_db)
from backend.internals.server import Server
from backend.internals.settings import Settings


class DiscoveryMetadataFixture(FixtureProvider):
    async def fetch_volume(self, provider_id):
        result=await super().fetch_volume(provider_id)
        result.issue_count=2
        result.issues.append(replace(result.issues[0],provider_id=str(int(provider_id)+2),issue_number='2',calculated_issue_number=2.0))
        return result


def main():
    errors,consoles,logs=[],[],[]
    class Capture(logging.Handler):
        def emit(self,record):
            if record.levelno>=logging.ERROR:logs.append(record.getMessage()+'\n'+traceback.format_exc())
    capture=Capture();LOGGER.addHandler(capture)
    with TemporaryDirectory(prefix='kapowarr-discovery-browser-') as directory,source_fixture() as (source,downloads),patch.dict('backend.implementations.metadata.registry.PROVIDERS',comicvine=DiscoveryMetadataFixture):
        source.posts[3]=(4,'External 900 #1 (2020)')
        base=Path(directory);(base/'downloads').mkdir();set_db_location(str(base/'db'));server=Server()
        with server.app.app_context():
            setup_db();Settings().update({'api_key':'disposable-discovery-key','download_folder':str(base/'downloads')})
            DownloadClients.trigger_client_registration()
            folder,incoming=seed(get_db(),base)
            RootFolders()._RootFolders__get_folder_mapping.cache_clear()
            get_db().connection.commit()
        server.app.extensions['discover'].transport=source
        async def acquire(reference):
            result=remote(reference.provider,reference.provider_id,701 if reference.provider=='metron' else 201,count=3)
            result.metadata.title='Batman'
            result.metadata.volume_number=1
            return admit(result,reference)
        server.app.extensions['provider_switch_reviews'].acquire=acquire
        http=make_server('127.0.0.1',0,server.app,threaded=True)
        Thread(target=http.serve_forever,daemon=True).start()
        origin=f'http://127.0.0.1:{http.server_port}'
        def api(method,path,body=None):
            response=requests.request(method,origin+'/api'+path,params={'api_key':'disposable-discovery-key'},json=body,timeout=40)
            assert response.status_code==200,(path,response.status_code,response.text)
            return response.json()['result']
        try:
            # apply_patch normalizes the fixture's final newline; the existing
            # application's SRI requires the original CDN response bytes.
            asset=(REPO/'tests/fixtures/vendor/socket.io-4.7.5.min.js').read_bytes().replace(b'\r\n',b'\n').rstrip(b'\n')+b'\n'
            with sync_playwright() as p:
                browser=p.chromium.launch();context=browser.new_context()
                context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(origin) else r.fulfill(content_type='text/javascript',body=asset,headers={'Access-Control-Allow-Origin':'*'}) if 'socket.io.min.js' in r.request.url else r.fulfill(status=200,body=''))
                context.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'disposable-discovery-key',last_login:Date.now()/1000,theme:'dark'}));")
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('console',lambda m:consoles.append(m.text) if m.type=='error' else None)
                page.goto(origin+'/discover');page.get_by_role('button',name='Refresh Discover',exact=True).click()
                page.wait_for_function("document.querySelectorAll('#d-content tbody tr').length===5")
                assert downloads==[]
                ids={v['title']:v['id'] for v in api('GET','/discover')['items']}
                page.get_by_role('button',name='Refresh Discover',exact=True).click()
                page.wait_for_function("!document.getElementById('d-refresh').disabled && document.getElementById('d-message').textContent.includes('No acquisition')")
                assert ids=={v['title']:v['id'] for v in api('GET','/discover')['items']}
                for issue in (1,2,3):
                    if issue == 3:
                        automation=WantedAutomation(DBConnection.default_file)
                        try:automation.reconcile()
                        finally:automation.close()
                        before=api('GET','/issues/2/acquisitions')
                        for provider,parent,first in (('metron','700',701),('comicvine','101',201)):
                            review=api('POST','/provider-switch/reviews',dict(volume_id=1,provider=provider,provider_id=parent))
                            review=api('PUT','/provider-switch/reviews/'+review['session_id'],dict(revision=review['revision'],
                                mappings=[dict(local_issue_id=i,target_provider_id=str(first+i-1)) for i in (1,2,3)]))
                            assert review['preview']['apply_available'],review['preview']['blockers']
                            result=api('POST','/provider-switch/reviews/'+review['session_id']+'/apply',dict(revision=review['revision'],
                                mapping_digest=review['preview']['mapping_digest'],confirmed=True,source_authority=review['source_authority']))
                            assert result['mapped_count']==3,result
                            assert api('GET','/issues/2/acquisitions')==before
                            assert ids=={v['title']:v['id'] for v in api('GET','/discover')['items']}
                    original=(folder/f'Batman {issue:03}.cbz').read_bytes() if issue>1 else None
                    row=page.locator('#d-content tbody tr').filter(has_text=f'Batman #{issue} (2020)')
                    row.get_by_role('button',name='View Discovery').click()
                    page.get_by_role('button',name='Resolve Current Offerings / Review Acquisition').click()
                    page.get_by_role('button',name='Confirm Acquire This Offering').wait_for(timeout=30000)
                    assert page.get_by_role('button',name='Confirm Acquire This Offering').is_enabled(),page.locator('#d-body').inner_text()
                    if issue == 1:
                        source.changed_offering=True
                        page.get_by_role('button',name='Confirm Acquire This Offering').click()
                        page.wait_for_function("document.getElementById('d-message').textContent.includes('stale preview')")
                        assert downloads==[]
                        source.changed_offering=False
                        page.keyboard.press('Escape')
                        row.get_by_role('button',name='View Discovery').click()
                        page.get_by_role('button',name='Resolve Current Offerings / Review Acquisition').click()
                        page.get_by_role('button',name='Confirm Acquire This Offering').wait_for(timeout=30000)
                    page.get_by_role('button',name='Confirm Acquire This Offering').click()
                    page.wait_for_function("!document.getElementById('d-dialog').open || /unavailable|error|changed|eligible/.test(document.getElementById('d-message').textContent)",timeout=30000)
                    assert not page.locator('#d-dialog').evaluate('(e)=>e.open'),(page.locator('#d-message').inner_text(),logs)
                    for _ in range(100):
                        with server.app.app_context():
                            row=get_db().execute("SELECT id FROM acquisition_intakes WHERE json_extract(completion,'$.volume_id')=1 AND json_extract(completion,'$.issue_ids[0]')=?",(issue,)).fetchone()
                        if row:break
                        time.sleep(.1)
                    assert row,'DDL completion not observed'
                    observed=time.time();runtime=IntakeRuntime(DBConnection.default_file,clock=lambda:observed)
                    runtime.tick();runtime.clock=lambda:observed+11;runtime.tick();runtime.tick()
                    state=api('GET',f'/issues/{issue}/quality')
                    if issue<3:
                        with server.app.app_context():
                            diagnostics=[dict(r) for r in get_db().execute('SELECT state,error,summary FROM acquisition_artifacts')]
                            intakes=[dict(r) for r in get_db().execute('SELECT state,error FROM acquisition_intakes')]
                        assert state['direct_owned'] and state['cutoff_satisfied'],(state,diagnostics,intakes,logs)
                    else:
                        assert (folder/f'Batman {issue:03}.cbz').read_bytes()==original
                        assert api('GET',f'/issues/{issue}/acquisitions')['items'][0]['state']=='rejected'
                    page.reload();page.wait_for_selector('#d-content tbody tr')
                assert len(downloads)>=3
                # Exact-candidate rejection is visible from a fresh Discover preview.
                row=page.locator('#d-content tbody tr').filter(has_text='Batman #3 (2020)')
                row.get_by_role('button',name='View Discovery').click()
                assert not page.get_by_role('button',name='Resolve Current Offerings / Review Acquisition').is_enabled()
                page.keyboard.press('Escape')
                page.locator('#d-content tbody tr').filter(has_text='External 900 #1').get_by_role('button',name='View Discovery').click()
                page.get_by_role('link',name='Find / Add to Library').click()
                page.wait_for_selector('#search-results .search-entry')
                page.locator('#search-results .search-entry').first.click()
                page.locator('#rootfolder-input').select_option('1')
                with page.expect_response(lambda r:'/api/volumes?' in r.url and r.request.method=='POST') as added_response:
                    page.locator('#add-volume').click()
                assert added_response.value.status in (200,201),added_response.value.text()
                page.wait_for_function("!document.querySelector('.window').hasAttribute('show-window')")
                page.goto(origin+'/discover');page.wait_for_selector('#d-content tbody tr')
                added=next(p for p in api('GET','/discover')['items'] if p['id']==ids['External 900 #1 (2020)'])
                assert added['match']=='matched',added
                assert len(downloads)==6
                with server.app.app_context():
                    db=get_db()
                    preview=claim_preview(db,3,PublicationRef('comicvine','901'),ClaimKind.COMPLETE,manual=True)
                    claim=confirm_claim(db,3,PublicationRef('comicvine','901'),ClaimKind.COMPLETE,preview['preview_token'],manual=True)
                    coverage=coverage_preview(db,3,3,[claim])
                    apply_coverage(db,3,3,[claim],coverage['preview_token'])
                    db.connection.commit()
                page.reload();page.wait_for_selector('#d-content tbody tr')
                c2row=page.locator('#d-content tbody tr').filter(has_text='External 900 #1')
                assert 'Content represented elsewhere (C2), not direct issue ownership' in c2row.inner_text()
                current=api('GET','/discover/posts/'+str(added['id']))
                assert not current['local']['direct_owned'] and current['local']['content_elsewhere']
                source.posts.append((6,'<script>window.discoveryAttack=true</script> #1 (2026)'))
                source.version+=1
                page.get_by_role('button',name='Refresh Discover',exact=True).click()
                page.wait_for_function("document.querySelectorAll('#d-content tbody tr').length===6")
                assert not page.evaluate('Boolean(window.discoveryAttack)')
                assert '<script>window.discoveryAttack=true</script>' in page.locator('#d-content').inner_text()
                source.mode='fallback'
                page.get_by_role('button',name='Refresh Discover',exact=True).click()
                page.wait_for_function("!document.getElementById('d-refresh').disabled")
                assert api('GET','/discover/status')['transport']=='html'
                assert not api('GET','/discover/status')['gap']
                source.mode='feed';source.posts=[(100,'New unrelated #1 (2026)')];source.version+=1
                page.get_by_role('button',name='Refresh Discover',exact=True).click()
                page.wait_for_function("!document.getElementById('d-refresh').disabled")
                assert api('GET','/discover/status')['gap']
                assert api('GET','/discover/status')['receipt']['pages']==4
                assert len(api('GET','/discover')['items'])==7
                source.mode='outage'
                page.get_by_role('button',name='Refresh Discover',exact=True).click()
                page.wait_for_function("document.getElementById('d-message').textContent.includes('network unavailable')")
                assert page.locator('#d-content tbody tr').count()==7
                page.set_viewport_size({'width':600,'height':850})
                page.get_by_role('button',name='Source Settings').focus();page.keyboard.press('Enter')
                assert page.evaluate("document.getElementById('d-dialog').contains(document.activeElement)")
                page.keyboard.press('Escape')
                assert not page.locator('#d-dialog').evaluate('(e)=>e.open')
                for path in ('/','/collections','/calendar','/reading-orders','/maintenance','/wanted','/settings/quality','/library-import','/discover'):
                    response=page.goto(origin+path)
                    assert response.status==200,path
                    page.wait_for_timeout(200)
                browser.close()
            assert not errors and not consoles and not logs,(errors,consoles,logs)
            with server.app.app_context():
                assert get_db().execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert not get_db().execute('PRAGMA foreign_key_check').fetchall()
            print(json.dumps(dict(chromium='PASS',errors=errors,console=consoles,server=logs,downloads=len(downloads))))
        finally:
            http.shutdown();UNIFIED_SEARCH.close_all();LOGGER.removeHandler(capture)


if __name__=='__main__':main()

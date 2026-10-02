"""Real Chromium + HTTP + TaskHandler + fixture search/SAB/intake acceptance."""

import json
import logging
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO),str(REPO/'tests')]

from fixtures.quality import comic, configure, sources
from playwright.sync_api import sync_playwright
from TProviderSwitchApply import remote
from werkzeug.serving import make_server

from backend.base.logging import LOGGER
from backend.features.intake_runtime import IntakeRuntime
from backend.features.sab_downloads import poll_downloads
from backend.implementations.metadata.switch_target import admit
from backend.internals.collections import CollectionStore
from backend.internals.db import (DBConnection, get_db,
                                  set_db_location, setup_db)
from backend.internals.download_jobs import DownloadStore
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    errors,consoles,logs=[],[],[]
    network=[]
    original_send=requests.Session.send
    def admitted_send(session,request,**kwargs):
        host=urlsplit(request.url).hostname
        if host not in ('127.0.0.1','localhost','cdn.socket.io'):
            network.append(host)
            raise AssertionError('Non-fixture HTTP destination')
        return original_send(session,request,**kwargs)
    class Capture(logging.Handler):
        def emit(self,r):
            if r.levelno>=logging.ERROR: logs.append(r.getMessage())
    capture=Capture();LOGGER.addHandler(capture)
    with TemporaryDirectory(prefix='kapowarr-8m-browser-') as directory,sources() as fixture,patch.object(requests.Session,'send',admitted_send):
        base=Path(directory);library=base/'library';folder=library/'Batman';incoming=base/'incoming'
        folder.mkdir(parents=True);incoming.mkdir()
        set_db_location(str(base/'db'));server=Server();server.app.logger.addHandler(capture)
        with server.app.app_context():
            setup_db();Settings().update({'api_key':'disposable-quality-key'})
            db=get_db();db.execute('INSERT INTO root_folders VALUES(1,?)',(str(library),))
            db.execute('UPDATE indexer_clients SET enabled=0')
            db.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder,monitored) VALUES(1,101,'Batman',2020,1,1,?,1)",(str(folder),))
            db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
            for iid,edge in ((1,600),(2,1800),(3,600)):
                db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(?,1,?,?,?,1)',(iid,200+iid,str(iid),iid))
                path=folder/f'Batman {iid:03}.cbz';comic(path,edge)
                db.execute('INSERT INTO files(id,filepath,size) VALUES(?,?,?)',(iid,str(path),path.stat().st_size))
                db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,?)',(iid,iid))
            client=configure(db,fixture,incoming)
            store=CollectionStore(db);first=store.create('First');second=store.create('<script>window.qualityHostile=true</script>')
            for tree in (first,second):store.add_local(tree['nodes'][0]['id'],tree['revision'],1)
            db.connection.commit()
        async def acquire(reference):
            result=remote(reference.provider,reference.provider_id,
                701 if reference.provider=='metron' else 201,count=3)
            result.metadata.title='Batman'
            result.metadata.volume_number=1
            return admit(result,reference)
        server.app.extensions['provider_switch_reviews'].acquire=acquire
        http=make_server('127.0.0.1',0,server.app,threaded=True);Thread(target=http.serve_forever,daemon=True).start()
        origin=f'http://127.0.0.1:{http.server_port}'
        def api(method,path,body=None):
            response=requests.request(method,origin+'/api'+path,params={'api_key':'disposable-quality-key'},json=body,timeout=40)
            assert response.status_code==200,(path,response.status_code,response.text)
            return response.json()['result']
        try:
            asset=requests.get('https://cdn.socket.io/4.7.5/socket.io.min.js',timeout=20);asset.raise_for_status()
            with sync_playwright() as p:
                browser=p.chromium.launch();context=browser.new_context()
                context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(origin) else r.fulfill(content_type='text/javascript',body=asset.content,headers={'Access-Control-Allow-Origin':'*'}) if 'socket.io.min.js' in r.request.url else r.fulfill(status=200,body=''))
                context.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'disposable-quality-key',last_login:Date.now()/1000,theme:'dark'}));")
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('console',lambda m:consoles.append(m.text) if m.type=='error' else None)
                page.goto(origin+'/settings/quality');page.get_by_role('button',name='Create Quality Profile',exact=True).focus();page.keyboard.press('Enter')
                dialog=page.locator('#q-dialog');assert page.evaluate("document.getElementById('q-dialog').contains(document.activeElement)")
                page.keyboard.press('Escape');assert not dialog.is_visible()
                page.get_by_role('button',name='Create Quality Profile',exact=True).click()
                page.get_by_label('Profile name',exact=True).fill('Fixture <script>window.qualityHostile=true</script>')
                page.get_by_label('Enable automatic upgrades below cutoff').check()
                page.get_by_label('Minimum verified p10 short edge',exact=False).fill('1000')
                for _ in range(4):dialog.get_by_role('button',name='Higher hd digital',exact=True).click()
                dialog.get_by_role('button',name='Set Cutoff hd digital',exact=True).click()
                dialog.get_by_role('button',name='Save Quality Profile',exact=True).evaluate('(e)=>{e.click();e.click();}')
                dialog.wait_for(state='hidden');assert len(api('GET','/quality-profiles')['items'])==2
                profile=api('GET','/quality-profiles')['items'][1];assert profile['upgrades'] and profile['cutoff']==6
                page.reload();page.get_by_text(profile['name']+' r1',exact=False).wait_for();assert page.evaluate('window.qualityHostile') is None
                page.goto(origin+'/settings/quality?volume=1');page.get_by_label('Volume profile override').select_option(str(profile['id']))
                page.get_by_role('button',name='Save Volume Assignment').click();page.get_by_text('· volume',exact=False).wait_for()
                page.reload();page.get_by_text('· volume',exact=False).wait_for()
                rows=page.locator('#q-content tbody tr')
                for index in range(3):
                    rows.nth(index).get_by_role('button',name='Analyze Quality').click()
                    page.wait_for_function("document.getElementById('q-message').textContent==='Analysis complete'")
                    page.wait_for_function('(n)=>document.querySelectorAll("#q-content tbody tr")[n]?.textContent.includes("verified p10")',arg=index)
                assert api('GET','/issues/1/quality')['upgrade_eligible']
                assert api('GET','/issues/2/quality')['files'][0]['claims']['origin']=='legacy_unknown'
                # Every inheritance mutation is an actual rendered control.
                page.get_by_label('Volume profile override').select_option('');page.get_by_role('button',name='Save Volume Assignment').click()
                page.get_by_text('· default',exact=False).wait_for()
                for tree in (first,second):
                    page.goto(origin+f'/settings/quality?node={tree["nodes"][0]["id"]}')
                    page.get_by_label('Node quality profile').select_option(str(profile['id']));page.get_by_role('button',name='Save Node Assignment').click()
                    page.wait_for_function('!document.querySelector("#q-content button").disabled')
                page.goto(origin+'/settings/quality?volume=1');page.get_by_text('· collection',exact=False).wait_for()
                page.goto(origin+f'/settings/quality?node={second["nodes"][0]["id"]}');page.get_by_label('Node quality profile').select_option('1');page.get_by_role('button',name='Save Node Assignment').click()
                page.wait_for_function('!document.querySelector("#q-content button").disabled')
                page.goto(origin+'/settings/quality?volume=1');page.get_by_text('Profile conflict.',exact=False).wait_for()
                page.get_by_label('Volume profile override').select_option(str(profile['id']));page.get_by_role('button',name='Save Volume Assignment').click();page.get_by_text('· volume',exact=False).wait_for()
                # The browser owns search and exact grab selection; fixture SAB
                # supplies only remote completion facts and synthetic bytes.
                async_script="""async ({issue}) => { const key='disposable-quality-key'; const r=await fetch('/api/issues/'+issue+'/release-search?api_key='+key,{method:'POST'});return (await r.json()).result; }"""
                before_quality=api('GET','/issues/1/quality')
                before_history=api('GET','/issues/1/acquisitions')
                for provider,parent,first_id in (('metron','700',701),('comicvine','101',201)):
                    switched=page.evaluate("""async ({provider,parent,first})=>{
                        const api=async(method,path,body)=>{const r=await fetch('/api'+path+'?api_key=disposable-quality-key',{
                            method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
                            const data=await r.json();if(!r.ok)throw new Error(JSON.stringify(data));return data.result;};
                        let review=await api('POST','/provider-switch/reviews',{volume_id:1,provider,provider_id:parent});
                        review=await api('PUT','/provider-switch/reviews/'+review.session_id,{revision:review.revision,
                            mappings:[1,2,3].map(id=>({local_issue_id:id,target_provider_id:String(first+id-1)}))});
                        if(!review.preview.apply_available)return review.preview;
                        return api('POST','/provider-switch/reviews/'+review.session_id+'/apply',{revision:review.revision,
                            mapping_digest:review.preview.mapping_digest,confirmed:true,source_authority:review.source_authority});
                    }""",{'provider':provider,'parent':parent,'first':first_id})
                    assert switched.get('mapped_count')==3,switched.get('blockers')
                    after_quality=api('GET','/issues/1/quality')
                    assert after_quality['files']==before_quality['files'] and after_quality['assignment']==before_quality['assignment']
                    assert api('GET','/issues/1/acquisitions')==before_history
                for issue,edge,outcome in ((1,1200,'imported'),(2,900,'rejected')):
                    page.goto(origin+'/volumes/1')
                    with page.expect_response(lambda r:f'/issues/{issue}/release-search?' in r.url) as search_response:
                        page.locator(f'tr[data-id="{issue}"] .action-column > :nth-child(2)').click()
                    result=search_response.value.json()['result'];assert result['results'][0]['quality']['result']=='provisional_upgrade'
                    candidate_row=page.locator('#search-result-table tbody tr').filter(has_text=f'Batman #{issue} (2020) (HD-Digital)')
                    candidate_row.get_by_text('provisional upgrade',exact=False).wait_for()
                    with page.expect_response(lambda r:'/api/release-search/' in r.url and r.request.method=='POST') as grab_response:
                        candidate_row.get_by_role('button',name='Download',exact=True).click()
                    receipt=grab_response.value.json()['result']
                    assert receipt['state']=='tracking',receipt
                    original=(folder/f'Batman {issue:03}.cbz').read_bytes()
                    source=incoming/f'Batman {issue:03} (2020).cbz';comic(source,edge)
                    downloads=DownloadStore(DBConnection.default_file)
                    try:
                        row=downloads.get(receipt['decision_id'])
                        fixture['remote']['queue'].pop(row['nzo_id'],None)
                        fixture['remote']['history'][row['nzo_id']]=dict(nzo_id=row['nzo_id'],status='Completed',storage='/complete/'+source.name,completed=1790000000)
                        poll_downloads(downloads,[client])
                    finally:downloads.close()
                    runtime=IntakeRuntime(DBConnection.default_file,clock=lambda:100.)
                    runtime.tick();runtime.clock=lambda:111.;runtime.tick();runtime.tick()
                    history=api('GET',f'/issues/{issue}/acquisitions')['items'];assert history[0]['state']==outcome,history
                    if outcome=='rejected':assert (folder/f'Batman {issue:03}.cbz').read_bytes()==original
                    page.goto(origin+'/settings/quality?volume=1');page.locator('#q-content tbody tr').nth(issue-1).get_by_role('button',name='Acquisition History').click()
                    dialog.get_by_role('button',name='Explain Acquisition').first.click();dialog.get_by_text('State: '+outcome,exact=True).wait_for();page.keyboard.press('Escape')
                assert api('GET','/issues/1/quality')['cutoff_satisfied']
                second_search=page.evaluate(async_script,{'issue':2});assert second_search['results'][0]['operationally_available'] is False
                equal=page.evaluate(async_script,{'issue':1});assert equal['results'][0]['quality']['result']=='equal' and not equal['results'][0]['download_eligible'],equal
                fixture['state']['label']='Digital';lower=page.evaluate(async_script,{'issue':1});assert lower['results'][0]['quality']['result']=='downgrade' and not lower['results'][0]['download_eligible']
                assert len(fixture['remote']['uploads'])==2
                page.set_viewport_size({'width':600,'height':800});page.goto(origin+'/settings/quality?volume=1');page.get_by_role('button',name='Save Volume Assignment').wait_for();assert page.locator('.q-table').is_visible()
                for route in ('/','/volumes/1','/collections','/calendar','/reading-orders','/maintenance','/wanted','/activity/queue','/settings/metadata','/library-import'):
                    assert page.goto(origin+route).status==200
                    page.wait_for_function("typeof socket !== 'undefined' && socket?.connected")
                    page.wait_for_timeout(200)
                browser.close()
            assert not errors and not consoles and not logs and not network,(errors,consoles,logs,network)
            print(json.dumps(dict(profiles=True,assignments=True,inheritance_conflict=True,legacy_analysis=True,
                real_search_sab_intake=True,successful_upgrade=True,false_hd_preserves_old=True,retry_suppression=True,
                same_group=True,manual_downgrade_blocked=True,provenance=True,keyboard=True,narrow=True,hostile=True,
                provider_switch_aba=True,existing_ui_smoke=True,unexpected_browser_errors=0,unexpected_server_errors=0)))
        finally:
            http.shutdown();http.server_close();LOGGER.removeHandler(capture)


if __name__=='__main__':main()

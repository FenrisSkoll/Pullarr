"""Disposable container UI acceptance; never mounts user application data."""
import json
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
NAME='pullarr-9b-acceptance'


def command(*args):
    return subprocess.check_output(args,cwd=ROOT,text=True,stderr=subprocess.STDOUT).strip()


def main():
    assert not command('docker','ps','-aq','--filter','name=^/'+NAME+'$'),'Fixture container already exists; inspect it first'
    image=sys.argv[1] if len(sys.argv)>1 else 'kapowarr-quality-closeout:local'
    identifier=command('docker','run','-d','--name',NAME,'-p','127.0.0.1::5656',
        '--mount',f'type=bind,source={ROOT},target=/app,readonly','--tmpfs','/app/.devdata:rw',
        '--tmpfs','/tmp:rw','-e','PYTHONDONTWRITEBYTECODE=1','-w','/app','--entrypoint','python',
        image,'scripts/archive_maintenance_fixture_server.py')
    try:
        info=json.loads(command('docker','inspect',NAME))[0]
        port=info['NetworkSettings']['Ports']['5656/tcp'][0]['HostPort']
        origin='http://127.0.0.1:'+port
        for _ in range(120):
            try:
                if requests.get(origin+'/maintenance',timeout=1).status_code==200:break
            except requests.RequestException:pass
            time.sleep(.25)
        else:raise AssertionError('Fixture container did not start')
        errors=[];external=[]
        with sync_playwright() as driver:
            browser=driver.chromium.launch();context=browser.new_context()
            context.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'synthetic-archive-container',last_login:Date.now()/1000}));")
            page=context.new_page();page.on('pageerror',lambda _:errors.append('page error'))
            page.on('console',lambda m:errors.append('console error') if m.type=='error' else None)
            page.on('request',lambda r:external.append('external') if not r.url.startswith(origin) else None)
            for width in (1440,390):
                page.set_viewport_size(dict(width=width,height=900))
                page.goto(origin+'/maintenance#archive-maintenance')
                page.locator('#archive-files input').first.wait_for()
                page.locator('#archive-select').click();page.locator('#archive-preview').click()
                page.locator('#archive-dialog').wait_for(state='visible')
                assert 'Shared bytes' in page.locator('#archive-review').inner_text()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.keyboard.press('Escape')
            page.locator('#archive-preview').click();page.locator('#archive-dialog').wait_for(state='visible')
            page.locator('#archive-apply').click()
            page.locator('#archive-results').get_by_text('completed',exact=True).first.wait_for()
            page.locator('#archive-files').get_by_text('ordinary.cbz',exact=True).wait_for()
            browser.close()
        assert not errors and not external,(errors,external)
        smoke=command('docker','exec',NAME,'python','scripts/archive_maintenance_container_smoke.py')
        logs=command('docker','logs',NAME)
        assert 'Traceback' not in logs and '[ERROR]' not in logs,'Unexpected fixture server error'
        print(json.dumps(dict(container_fixture=identifier,image=image,desktop=True,narrow=True,
            ordinary_and_shared_conversion=True,browser_errors=0,server_errors=0,external_requests=0,archive_smoke=smoke)))
    finally:
        # Exact container created above; no application volumes were mounted.
        command('docker','rm','-f',identifier)


if __name__=='__main__':main()

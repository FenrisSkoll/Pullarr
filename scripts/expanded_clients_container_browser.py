"""Host Chromium against the separately named disposable 9A container."""
import json
import re
import subprocess
import time
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright


def main():
    address = subprocess.check_output(['docker','port','pullarr-9a-ui-fixture','5656'],text=True).strip()
    assert address.startswith('127.0.0.1:') and address.split(':')[1].isdigit()
    origin = 'http://' + address
    errors = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        context = browser.new_context()
        context.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'synthetic-container-fixture',last_login:Date.now()/1000}));")
        page = context.new_page()
        page.on('pageerror',lambda _:errors.append('page error'))
        page.on('console',lambda m:errors.append(dict(text=m.text.replace('synthetic-container-fixture','[fixture]'),path=urlsplit(m.location.get('url','')).path)) if m.type=='error' else None)
        page.on('response',lambda response:errors.append(dict(status=response.status,path=urlsplit(response.url).path)) if response.status>=400 else None)
        page.goto(origin + '/wanted')
        page.locator('#wanted-status').filter(has_text='Automation worker:').wait_for()
        if page.get_by_role('button',name='Search and grab now',exact=True).count():
            page.get_by_role('button',name='Search and grab now',exact=True).click()
        history = []
        for _ in range(180):
            history = requests.get(origin+'/api/issues/1/acquisitions',params={'api_key':'synthetic-container-fixture'},timeout=5).json()['result']['items']
            if history and history[0]['state'] == 'imported': break
            time.sleep(.5)
        assert history and history[0]['state']=='imported'
        for width in (1440,390):
            page.set_viewport_size(dict(width=width,height=844))
            for path in ('/settings/downloadclients','/settings/indexers','/settings/general','/activity/queue','/wanted','/volumes/1'):
                page.goto(origin+path)
                page.locator('main h1').wait_for()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            page.goto(origin+'/activity/queue')
            imported = page.locator('#managed-download-rows td').filter(has_text=re.compile(r'^Imported'))
            imported.wait_for()
            assert imported.evaluate('node => node.firstChild.textContent') == 'Imported'
            page.get_by_role('button',name='Remove torrent + data',exact=True).click()
            page.locator('#managed-download-status').filter(has_text='minimum seed requirements').wait_for()
        browser.close()
    logs = subprocess.check_output(['docker','logs','pullarr-9a-ui-fixture'],text=True,stderr=subprocess.STDOUT)
    assert 'Traceback' not in logs and '[ERROR]' not in logs
    assert not errors,errors
    print(json.dumps(dict(container='pullarr-9a-ui-fixture',chromium='PASS',upgrade='imported while seeding',
        tracker_cleanup='blocked',widths=[1440,390],unexpected_errors=0)))


if __name__=='__main__':
    main()

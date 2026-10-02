"""Disposable public-image startup, persistence, browser and all-layer audit."""
import argparse
import json
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import requests

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    return subprocess.check_output(['docker', *args], cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()


def audit_layers(image):
    checked = 0
    with tempfile.TemporaryDirectory(prefix='pullarr-image-audit-') as temporary:
        archive = Path(temporary)/'image.tar'
        docker('save', '-o', str(archive), image)
        with tarfile.open(archive) as outer:
            manifest = json.load(outer.extractfile('manifest.json'))
            for layer in manifest[0]['Layers']:
                with tarfile.open(fileobj=outer.extractfile(layer), mode='r|*') as content:
                    for member in content:
                        name = member.name.lstrip('./')
                        parts = Path(name).parts
                        assert not {'.git', '.devdata', '.venv'}.intersection(parts), 'Private directory in image layer'
                        if name.startswith('app/') and member.isfile():
                            assert not name.endswith(('.db', '.db-wal', '.db-shm', '.sqlite', '.log', '.png', '.cbr')), 'Runtime/fixture data in image layer'
                            assert 'rollback' not in name and 'browser-state' not in name
                            checked += 1
    return checked


def main(image, browser=False):
    identifier = 'pullarr-public-check-' + uuid4().hex[:12]
    volume = identifier + '-db'
    docker('volume', 'create', volume)
    def start():
        docker('run', '-d', '--name', identifier, '-p', '127.0.0.1::5656',
               '--mount', f'type=volume,source={volume},target=/app/db',
               '--tmpfs', '/app/logs:rw', '--tmpfs', '/app/temp_downloads:rw', image)
        port = json.loads(docker('inspect', identifier))[0]['NetworkSettings']['Ports']['5656/tcp'][0]['HostPort']
        origin = 'http://127.0.0.1:' + port
        for _ in range(100):
            try:
                response = requests.post(origin+'/api/auth', json={}, timeout=2)
                if response.status_code == 200:
                    return origin, response.json()['result']['api_key']
            except requests.RequestException:
                pass
            time.sleep(.2)
        raise AssertionError('Disposable public startup failed')
    try:
        origin, key = start()
        auth = {'api_key': key}
        r = requests.put(origin+'/api/settings', params=auth, json={'db_backup_amount': 4}, timeout=10)
        assert r.status_code == 200
        docker('restart', identifier)
        # Docker may allocate a different ephemeral host port on restart.
        port = json.loads(docker('inspect', identifier))[0]['NetworkSettings']['Ports']['5656/tcp'][0]['HostPort']
        origin = 'http://127.0.0.1:' + port
        for _ in range(100):
            try:
                if requests.get(origin+'/api/settings', params=auth, timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(.2)
        else:
            raise AssertionError('Disposable public restart did not become ready')
        docker('rm', '-f', identifier)
        origin, restarted_key = start()
        assert key == restarted_key
        result = requests.get(origin+'/api/settings', params=auth, timeout=5).json()['result']
        assert result['db_backup_amount'] == 4
        errors = []
        if browser:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                chromium = p.chromium.launch()
                context = chromium.new_context()
                context.add_init_script('localStorage.setItem("kapowarr",JSON.stringify('+json.dumps(dict(api_key=key,last_login=time.time()))+'));')
                page = context.new_page()
                page.on('pageerror', lambda _: errors.append('page'))
                page.on('console', lambda m: errors.append('console: '+re.sub(r'\?[^\s\"\x27]+', '?[redacted]', m.text)) if m.type=='error' else None)
                page.on('request', lambda r: errors.append('external') if not r.url.startswith(origin) else None)
                for width in (1440,390):
                    page.set_viewport_size(dict(width=width,height=900))
                    for route in ('/','/wanted','/discover','/collections','/calendar','/reading-orders',
                                  '/maintenance','/settings/general','/settings/quality','/system/status'):
                        r = page.goto(origin+route)
                        page.wait_for_timeout(500)
                        assert r.status == 200 and 'Pullarr' in page.title(), route
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), {
                            'route': route, 'viewport': width,
                            'overflow': page.evaluate("Array.from(document.querySelectorAll('main *')).filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,12).map(e=>({tag:e.tagName,id:e.id,width:e.getBoundingClientRect().width}))")}
                chromium.close()
        assert not errors, errors
        probe = "import sqlite3;d=sqlite3.connect('file:/app/db/Kapowarr.db?mode=ro',uri=True);assert d.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert not d.execute('PRAGMA foreign_key_check').fetchall();assert int(d.execute(\"SELECT value FROM config WHERE key='database_version'\").fetchone()[0])==72;print('schema72 integrity/FKs PASS')"
        docker('exec',identifier,'python','-c',probe)
        # Immutable synthetic fixture input; no library mount or ownership writes.
        docker('cp',str(ROOT/'tests/fixtures/archives'),identifier+':/tmp/public-fixtures')
        archive = docker('exec',identifier,'python','scripts/archive_maintenance_container_smoke.py','/tmp/public-fixtures')
        logs = docker('logs',identifier)
        assert key not in logs and 'Traceback' not in logs and ' ERROR' not in logs
        layers = audit_layers(image)
        print(json.dumps(dict(image=image, schema=72, restart=True, recreation=True, browser_errors=len(errors),
                              archive_smoke=archive, app_layer_files_checked=layers, clean_layers=True)))
    finally:
        subprocess.run(['docker','rm','-f',identifier],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='pullarr:public-candidate')
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    main(args.image, args.browser)

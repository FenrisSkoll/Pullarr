"""Template-derived, disposable Linux/amd64 UID99 Docker acceptance. Never publish."""
import argparse
import json
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

import requests
from public_docker_gate import audit_layers, docker

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = {'/app/db':'db','/app/logs':'logs','/app/temp_downloads':'temp_downloads','/data':'data'}


def contract(template):
    template = template or ROOT/'templates/pullarr.xml'
    if template:
        data = Path(template).read_bytes()
        assert len(data)<32768 and b'<!DOCTYPE' not in data.upper()
        app = ET.fromstring(data)
        assert app.attrib=={'version':'2'} and app.findtext('Network')=='bridge' and app.findtext('Privileged')=='false'
        configs = app.findall('Config')
        paths = {c.get('Target'):c.get('Default') for c in configs if c.get('Type')=='Path'}
        variables = {c.get('Target'):c.get('Default') for c in configs if c.get('Type')=='Variable'}
        ports = [c for c in configs if c.get('Type')=='Port']
        assert len(ports)==1 and ports[0].get('Target')==ports[0].get('Default')=='5656'
        assert set(paths)==set(DEFAULT_PATHS) and variables=={'PUID':'99','PGID':'100','TZ':'Etc/UTC'}
        return paths, variables
    return DEFAULT_PATHS, {'PUID':'99','PGID':'100','TZ':'Etc/UTC'}


def main(image, template=None, browser=False):
    paths, variables = contract(template)
    name = 'pullarr-unraid-check-'+uuid4().hex[:12]
    volume = name+'-fixture'
    docker('volume','create',volume)
    host = json.loads(docker('volume','inspect',volume))[0]['Mountpoint']
    # Host paths from XML are replaced only with disposable equivalents; targets,
    # variables, network and container port are consumed from the actual template.
    docker('run','--rm','--network','none','--entrypoint','python','--mount',f'type=volume,source={volume},target=/fixture',image,'-c',
        "import os;from pathlib import Path;[(Path('/fixture')/n).mkdir() for n in ('db','logs','temp_downloads','data')];os.chown('/fixture/data',99,100);os.chmod('/fixture/data',0o750)")
    def start():
        args = ['run','-d','--name',name,'--platform','linux/amd64','--network','bridge','-p','127.0.0.1::5656',
                '--tmpfs','/split:rw,uid=99,gid=100,mode=0750']
        for target in paths:
            args += ['--mount',f'type=bind,source={host}/{DEFAULT_PATHS[target]},target={target}']
        for key,value in variables.items(): args += ['-e',key+'='+value]
        docker(*args,image)
        info = json.loads(docker('inspect',name))[0]
        assert not info['HostConfig']['Privileged'] and info['HostConfig']['NetworkMode']=='bridge'
        assert not info['HostConfig']['CapAdd'] and not info['HostConfig']['Devices']
        origin = 'http://127.0.0.1:'+info['NetworkSettings']['Ports']['5656/tcp'][0]['HostPort']
        for _ in range(150):
            try:
                response = requests.post(origin+'/api/auth',json={},timeout=2)
                if response.status_code==200: return origin,response.json()['result']['api_key']
            except requests.RequestException: pass
            time.sleep(.2)
        raise AssertionError('UID99 startup failed')
    try:
        origin,key = start()
        uid = json.loads(docker('exec',name,'python','-c',"import json;from pathlib import Path;p=Path('/proc/1/status').read_text();print(json.dumps([x.split()[1:] for x in p.splitlines() if x.startswith(('Uid:','Gid:'))]))"))
        assert uid == [['99']*4,['100']*4], uid
        docker('exec','--user','99:100',name,'mkdir','-p','/data/media/comics')
        response = requests.post(origin+'/api/rootfolder',params={'api_key':key},json={'folder':'/data/media/comics'},timeout=10)
        assert response.status_code==201, response.status_code
        docker('exec',name,'mkdir','-p','/tmp/acceptance')
        docker('cp',str(ROOT/'tests'),name+':/tmp/acceptance/tests')
        docker('cp',str(ROOT/'scripts/unraid_container_fixture.py'),name+':/tmp/acceptance/fixture.py')
        fixture = json.loads(docker('exec','--user','99:100',name,'python','/tmp/acceptance/fixture.py'))
        errors = []
        if browser:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser_instance = p.chromium.launch()
                page = browser_instance.new_page()
                page.add_init_script('localStorage.setItem("kapowarr",JSON.stringify('+json.dumps(dict(api_key=key,last_login=time.time()))+'));')
                page.on('pageerror',lambda _:errors.append('page'))
                page.on('console',lambda m:errors.append('console') if m.type=='error' else None)
                page.on('request',lambda r:errors.append('external') if not r.url.startswith(origin) else None)
                for width in (1440,390):
                    page.set_viewport_size(dict(width=width,height=900))
                    for route in ('/','/maintenance','/settings/general','/settings/downloadclients'):
                        assert page.goto(origin+route).status==200
                        page.wait_for_timeout(500)
                        assert 'Pullarr' in page.title()
                browser_instance.close()
        assert not errors, errors
        for _ in range(100):
            if json.loads(docker('inspect',name))[0]['State']['Health']['Status']=='healthy': break
            time.sleep(.5)
        else: raise AssertionError('Healthcheck did not become healthy')
        probe = "import sqlite3;from pathlib import Path;d=sqlite3.connect('/app/db/Kapowarr.db');assert d.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert not d.execute('PRAGMA foreign_key_check').fetchall();assert int(d.execute(\"SELECT value FROM config WHERE key='database_version'\").fetchone()[0])==72;assert d.execute('SELECT folder FROM root_folders').fetchone()[0].startswith('/data/');assert all((p.stat().st_uid,p.stat().st_gid)==(99,100) for root in ('/app/db','/app/logs','/app/temp_downloads') for p in Path(root).rglob('*') if p.is_file());print('schema72 ownership integrity PASS')"
        docker('exec','--user','99:100',name,'python','-c',probe)
        docker('stop','--time','45',name)
        assert json.loads(docker('inspect',name))[0]['State']['ExitCode']==0
        docker('start',name)
        time.sleep(3)
        docker('stop','--time','45',name)
        logs = docker('logs',name)
        assert key not in logs and 'Traceback' not in logs and '[ERROR]' not in logs
        docker('rm',name)
        origin,new_key = start()
        assert new_key==key
        docker('exec','--user','99:100',name,'python','-c',probe)
        # Disposable previous-schema image startup; no stable volume reused.
        docker('stop','--time','45',name)
        docker('run','--rm','--network','none','--user','99:100','--entrypoint','python',
            '--mount',f'type=bind,source={host}/db,target=/fixture',image,'-c',
            "import os,sqlite3;from backend.internals.db import SCHEMA_71;old=sqlite3.connect('/fixture/Kapowarr.db');config=old.execute('SELECT * FROM config').fetchall();roots=old.execute('SELECT * FROM root_folders').fetchall();old.close();d=sqlite3.connect('/fixture/prior.db');d.executescript(SCHEMA_71);d.executemany('INSERT OR REPLACE INTO config VALUES(?,?)',config);d.execute(\"UPDATE config SET value=71 WHERE key='database_version'\");d.executemany('INSERT INTO root_folders VALUES(?,?)',roots);d.commit();d.close();os.replace('/fixture/prior.db','/fixture/Kapowarr.db')")
        docker('start',name)
        time.sleep(4)
        docker('exec','--user','99:100',name,'python','-c',probe)
        logs = docker('logs',name)
        assert key not in logs and 'Traceback' not in logs and '[ERROR]' not in logs
        image_info = json.loads(docker('image','inspect',image))[0]
        assert image_info['Architecture']=='amd64' and image_info['Os']=='linux'
        print(json.dumps(dict(image=image_info['Id'],architecture='linux/amd64',bytes=image_info['Size'],
            template_derived=True,uid=99,gid=100,bridge=True,privileged=False,fixtures=fixture,
            restart=True,recreation=True,migration='71-to-72',health='healthy',browser_errors=errors,
            image_layer_files=audit_layers(image))))
    finally:
        subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL,check=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',default='pullarr:unraid-check')
    parser.add_argument('--template')
    parser.add_argument('--browser',action='store_true')
    args=parser.parse_args()
    try:
        main(args.image,args.template,args.browser)
    except subprocess.CalledProcessError as error:
        print(error.output[-6000:] if error.output else 'Docker command failed')
        raise

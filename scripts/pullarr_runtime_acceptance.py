"""Pullarr/compatibility entrypoints: fresh/latest, previous71 and disposable63."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO),str(REPO/'tests')]

from fixtures.discovery import seed
from TQuality import policy

from backend.base.definitions import GCDownloadService
from backend.internals.collections import CollectionStore
from backend.internals.db import DB_SCHEMA, SCHEMA_63, SCHEMA_71
from backend.internals.reading_orders import ReadingOrderStore


def run(version):
    with TemporaryDirectory(prefix='kapowarr-discover-startup-') as directory:
        base=Path(directory)
        for folder in ('db','logs','downloads','library'): (base/folder).mkdir()
        database=base/'db/Kapowarr.db';before={};post_ids=None
        if version!='fresh':
            with closing(sqlite3.connect(database)) as db:
                db.execute('PRAGMA foreign_keys=ON')
                db.executescript(DB_SCHEMA if version=='72' else SCHEMA_71 if version=='71' else SCHEMA_63)
                db.execute("INSERT INTO config VALUES('database_version',?)",(int(version),))
                seed(db.cursor(),base,quality=version in ('71','72'))
                tables=['volume_external_ids','issue_external_ids','issues_files']
                if version in ('71','72'):
                    c=CollectionStore(db.cursor());tree=c.create('Preserved Collection');c.add_local(tree['nodes'][0]['id'],tree['revision'],1)
                    db.execute('INSERT INTO release_events(id,issue_id) VALUES(1,1)')
                    r=ReadingOrderStore(db.cursor());order=r.create('Preserved sequence');r.add_local(order['id'],order['revision'],1)
                    tables+=['collections','collection_nodes','collection_memberships','release_events','reading_orders','reading_order_entries','quality_profiles','file_quality_assessments']
                db.commit();before={t:db.execute('SELECT * FROM '+t).fetchall() for t in tables}
        for attempt in range(2):
            with closing(socket.socket()) as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            log=base/f'run-{attempt}.log'
            env=dict(os.environ,KAPOWARR_RUN_MAIN='1',KAPOWARR_START_TYPE='130',
                PYTHONPATH=os.pathsep.join((str(REPO/'tests/fixtures/discovery_startup'),str(REPO/'tests'),str(REPO))))
            with log.open('w',encoding='utf-8') as output:
                process=subprocess.Popen([sys.executable,'-u',str(REPO/('Pullarr.py' if attempt==0 else 'Kapowarr.py')),'-d',str(base/'db'),'-l',str(base/'logs'),
                    '-t',str(base/'downloads'),'-o','127.0.0.1','-p',str(port)],cwd=REPO,env=env,stdout=output,stderr=subprocess.STDOUT)
                try:
                    origin=f'http://127.0.0.1:{port}'
                    for _ in range(240):
                        if process.poll() is not None:raise AssertionError(log.read_text())
                        try:
                            if requests.get(origin+'/discover',timeout=1).status_code==200:break
                        except requests.RequestException:pass
                        time.sleep(.25)
                    else:raise AssertionError('Startup timeout')
                    with closing(sqlite3.connect(database)) as db:
                        db.execute('PRAGMA foreign_keys=ON')
                        assert db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0]==72
                        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                        assert not db.execute('PRAGMA foreign_key_check').fetchall()
                        if attempt==0:
                            assert before=={t:db.execute('SELECT * FROM '+t).fetchall() for t in before}
                            if version=='fresh':seed(db.cursor(),base)
                            if not db.execute('SELECT 1 FROM indexer_clients').fetchone():
                                db.execute("""INSERT INTO indexer_clients(enabled,download_type,client_type,title,url,gc_service_preference,gc_avoid_large_downloads)
                                    VALUES(1,1,'GetComics','GetComics','https://getcomics.org',?,0)""",(','.join(s.value for s in GCDownloadService),))
                            db.commit()
                        key=db.execute("SELECT value FROM config WHERE key='api_key'").fetchone()[0]
                    def api(method,path,body=None):
                        response=requests.request(method,origin+'/api'+path,params={'api_key':key},json=body,timeout=35)
                        assert response.status_code==200,(path,response.status_code,response.text)
                        return response.json()['result']
                    def wait(task):
                        for _ in range(160):
                            value=api('GET','/discover/tasks/'+task['id'])
                            if value['state'] not in ('queued','running'):
                                assert value['state']=='complete',value
                                return value['result']
                            time.sleep(.25)
                        raise AssertionError('Task timeout')
                    assert requests.get(origin+'/api/discover',timeout=5).status_code==401
                    if attempt==0:
                        assert not api('GET','/discover/status')['automatic']
                        if version=='63':
                            api('POST','/quality-profiles/1',dict(name='Fixture quality',policy=policy(),revision=1))
                            task=api('POST','/quality-analysis',dict(file_ids=[2,3]))
                            for _ in range(80):
                                result=api('GET','/quality-analysis/'+task['id'])
                                if result['state'] not in ('queued','running'):break
                                time.sleep(.25)
                            assert result['state']=='complete',result
                        wait(api('POST','/discover/refresh',{}))
                        posts=api('GET','/discover')['items'];post_ids=[p['id'] for p in posts]
                        for issue in (1,2,3):
                            post=next(p for p in posts if p.get('local',{}).get('issue_id')==issue)
                            original=(base/'library/Batman'/f'Batman {issue:03}.cbz').read_bytes() if issue>1 else None
                            preview=wait(api('POST',f'/discover/posts/{post["id"]}/acquisition-preview',{}))
                            offering=next(o for o in preview['offerings'] if o['allowed'])
                            wait(api('POST',f'/discover/posts/{post["id"]}/acquire',dict(preview_id=preview['preview_id'],offering_id=offering['offering_id'],confirmed=True)))
                            for _ in range(240):
                                history=api('GET',f'/issues/{issue}/acquisitions')['items']
                                if history and history[0]['state'] in ('imported','rejected'):break
                                time.sleep(.25)
                            assert history[0]['state']==('rejected' if issue==3 else 'imported'),(history,api('GET','/activity/queue'))
                            if issue==3:assert (base/'library/Batman'/f'Batman {issue:03}.cbz').read_bytes()==original
                        status=api('GET','/discover/status')
                        api('POST','/discover/settings',dict(revision=status['revision'],enabled=True,automatic=True,interval_minutes=60))
                    else:
                        assert api('GET','/discover/status')['automatic']
                        assert [p['id'] for p in api('GET','/discover')['items']]==post_ids
                        assert wait(api('POST','/discover/refresh',{}))['status']=='unchanged'
                        assert api('GET','/issues/2/quality')['cutoff_satisfied']
                        assert api('GET','/issues/3/acquisitions')['items'][0]['state']=='rejected'
                    api('POST','/system/power/shutdown',{})
                    process.wait(timeout=40)
                finally:
                    if process.poll() is None:
                        try:
                            requests.post(origin+'/api/system/power/shutdown',params={'api_key':key},timeout=10)
                            process.wait(timeout=40)
                        except (requests.RequestException,subprocess.TimeoutExpired):
                            process.terminate();process.wait(timeout=15)
            content=log.read_text(encoding='utf-8')
            assert 'Traceback' not in content and '[ERROR]' not in content,content[-8000:]
        with closing(sqlite3.connect(database)) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
        print(json.dumps(dict(start=version,schema=72,reopen=True,acquisition='missing/upgrade/false-HD',integrity='ok',errors=0)),flush=True)


if __name__=='__main__':
    for mode in ('fresh','72','71','63'):run(mode)

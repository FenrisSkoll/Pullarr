"""Normal Kapowarr.py startup: fresh69, 68/63→69, durable source and reopen."""

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
from unittest.mock import patch

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests')]

from fixtures.reading_orders import CBL

from backend.internals.collections import CollectionStore
from backend.internals.db import SCHEMA_63, SCHEMA_68
from backend.internals.provider_identity import ProviderIdentityDB


def run(version):
    with TemporaryDirectory(prefix='kapowarr-8l-startup-') as directory:
        base = Path(directory)
        for name in ('db','logs','downloads','library/Local'):
            (base/name).mkdir(parents=True)
        database = base/'db/Kapowarr.db'
        original = None
        if version != 'fresh':
            with closing(sqlite3.connect(database)) as db:
                db.executescript({'63': SCHEMA_63, '68': SCHEMA_68}[version])
                db.execute("INSERT INTO config VALUES('database_version',?)", (int(version),))
                db.execute("INSERT INTO config VALUES('file_naming','Reading preserved {issue_number}')")
                db.execute('INSERT INTO root_folders VALUES(1,?)', (str(base/'library'),))
                db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder,monitored) VALUES(1,100,'Local',1,?,1)", (str(base/'library/Local'),))
                db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
                db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(1,1,101,'1',0)")
                db.commit()
                if version == '68':
                    store=CollectionStore(db.cursor()); tree=store.create('Preserved Collection',monitoring='monitored')
                    store.add_local(tree['nodes'][0]['id'],tree['revision'],1)
                    db.execute("INSERT INTO release_events(id,issue_id) VALUES(1,1)")
                    db.commit()
                names = ['volume_external_ids','issue_external_ids','issues_files']
                if version == '68': names += ['collections','collection_nodes','collection_memberships','release_events']
                original = {t:db.execute('SELECT * FROM '+t).fetchall() for t in names}
        order_id = source_id = None
        for attempt in range(2):
            with closing(socket.socket()) as listener:
                listener.bind(('127.0.0.1',0)); port=listener.getsockname()[1]
            log=base/f'startup-{attempt}.txt'
            env=dict(os.environ,KAPOWARR_RUN_MAIN='1',KAPOWARR_START_TYPE='130',
                PYTHONPATH=os.pathsep.join((str(REPO/'tests/fixtures/reading_orders_startup'),str(REPO/'tests'),str(REPO))))
            with log.open('w',encoding='utf-8') as output:
                process=subprocess.Popen([sys.executable,'-u',str(REPO/'Kapowarr.py'),'-d',str(base/'db'),'-l',str(base/'logs'),
                    '-t',str(base/'downloads'),'-o','127.0.0.1','-p',str(port)],cwd=REPO,env=env,stdout=output,stderr=subprocess.STDOUT)
                try:
                    origin=f'http://127.0.0.1:{port}'
                    for _ in range(240):
                        if process.poll() is not None: raise AssertionError('Startup exited: '+log.read_text())
                        try:
                            if requests.get(origin+'/reading-orders',timeout=1).status_code==200: break
                        except requests.RequestException: pass
                        time.sleep(.25)
                    else: raise AssertionError('Startup timed out')
                    with closing(sqlite3.connect(database)) as db:
                        assert db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0]==69
                        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                        assert not db.execute('PRAGMA foreign_key_check').fetchall()
                        if original and attempt==0: assert original=={t:db.execute('SELECT * FROM '+t).fetchall() for t in original}
                        key=db.execute("SELECT value FROM config WHERE key='api_key'").fetchone()[0]
                    def api(method,path,body=None):
                        response=requests.request(method,origin+'/api'+path,params={'api_key':key},json=body,timeout=30)
                        assert response.status_code in (200,201),(path,response.status_code,response.text)
                        return response.json()['result']
                    def wait(task):
                        for _ in range(120):
                            result=api('GET','/reading-orders/tasks/'+task['id'])
                            if result['state'] not in ('queued','running'):
                                assert result['state']=='complete',result
                                return result
                            time.sleep(.25)
                        raise AssertionError('Task did not settle')
                    assert requests.get(origin+'/api/reading-orders',timeout=10).status_code==401
                    if attempt==0:
                        if version=='fresh': api('POST','/rootfolder',{'folder':str(base/'library')})
                        response=requests.post(origin+'/api/reading-orders/import',params={'api_key':key},data=CBL,headers={'Content-Type':'application/xml'},timeout=30)
                        assert response.status_code==200,response.text
                        review=response.json()['result']
                        order=api('POST','/reading-orders/reviews/'+review['id']+'/accept',dict(revision=review['revision'],expected_digest=review['digest'],confirmed=True))
                        order_id=order['id']
                        entries=api('GET',f'/reading-orders/{order_id}/entries')['items']
                        preview=api('POST',f'/reading-orders/{order_id}/wanted-preview',dict(revision=order['revision'],entries=[e['id'] for e in entries]))
                        ready=[e['entry_id'] for e in preview['items'] if e['bucket']=='ready']
                        if ready: api('POST','/reading-orders/wanted/'+preview['id']+'/apply',dict(expected_digest=preview['digest'],selected=ready,confirmed=True))
                        external=next(e for e in entries if any(r['issue_id']=='901' for r in e['refs']))
                        wait(api('POST',f'/reading-orders/{order_id}/add-publication',dict(entry_id=external['id'],provider='comicvine',root_id=1,confirmed=True)))
                        order=api('POST',f'/reading-orders/{order_id}/subscriptions',dict(revision=order['revision'],url='https://example.com/list.cbl'))
                        source_id=order['source']['id']
                        wait(api('POST',f'/reading-orders/sources/{source_id}/refresh',{}))
                        pending=api('GET',f'/reading-orders/sources/{source_id}/pending')
                        assert pending['pending']
                        # Leave this pending across actual process shutdown.
                        search=wait(api('POST','/reading-orders/providers/search',dict(provider='metron',query='fixture')))
                        fetched=wait(api('POST','/reading-orders/providers/'+search['id']+'/fetch',dict(result_id=search['items'][0]['id'])))
                        assert fetched['result']['total']==2
                    else:
                        pending=api('GET',f'/reading-orders/sources/{source_id}/pending')
                        assert pending['pending']
                        api('POST',f'/reading-orders/sources/{source_id}/decision',dict(revision=pending['revision'],order_revision=pending['order_revision'],expected_digest=pending['digest'],decision='accept',confirmed=True))
                        wait(api('POST',f'/reading-orders/sources/{source_id}/refresh',{}))
                        assert not api('GET',f'/reading-orders/sources/{source_id}/pending')['pending']
                    entries=api('GET',f'/reading-orders/{order_id}/entries')['items']
                    assert len(entries)==7
                    assert next(e for e in entries if any(r['issue_id']=='901' for r in e['refs']))['canonical_id'] is not None
                    exported=requests.get(origin+f'/api/reading-orders/{order_id}/export',params={'api_key':key},timeout=10)
                    assert exported.status_code==200 and b'<ReadingList>' in exported.content
                    for path in ('/reading-orders','/collections','/calendar','/maintenance','/wanted','/settings/metadata'):
                        assert requests.get(origin+path,timeout=10).status_code==200
                    requests.post(origin+'/api/system/power/shutdown',params={'api_key':key},timeout=10)
                    process.wait(timeout=30); assert process.returncode==0
                finally:
                    if process.poll() is None: process.terminate(); process.wait(timeout=15)
            logs=log.read_text(encoding='utf-8'); assert 'Traceback' not in logs and '[ERROR]' not in logs,logs
        with closing(sqlite3.connect(database)) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            with patch('backend.internals.provider_identity.get_db',side_effect=db.cursor):
                assert ProviderIdentityDB.audit(('comicvine','metron','gcd'))==[]
        print(json.dumps(dict(start=version,end=69,normal_entrypoint=True,reopen=True,integrity='ok',foreign_keys=[],provider_identity_audit=[],
            preserved=True,task_handler=True,CBL=True,subscription_restart=True,provider_source=True,add_volume=True,wanted=True,errors=0)))


if __name__=='__main__':
    for version in (sys.argv[1:] or ['fresh','68','63']):
        if version not in ('fresh','68','63'): raise SystemExit('Choose fresh, 68 or 63')
        run(version)

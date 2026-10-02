"""Disposable real Kapowarr.py startup, historical migration, analysis and reopen."""

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
sys.path[:0] = [str(REPO), str(REPO/'tests')]

from fixtures.quality import comic, configure, sources
from TQuality import policy

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.features.intake_runtime import IntakeRuntime
from backend.features.sab_downloads import poll_downloads
from backend.internals.collections import CollectionStore
from backend.internals.content_claims import claim_preview, confirm_claim
from backend.internals.db import SCHEMA_63, SCHEMA_69
from backend.internals.download_jobs import DownloadStore
from backend.internals.provider_identity import ProviderIdentityDB
from backend.internals.reading_orders import ReadingOrderStore


def run(version):
    with TemporaryDirectory(prefix='kapowarr-8m-startup-') as directory, sources() as fixture:
        base=Path(directory)
        for name in ('db','logs','downloads','library/Batman'):
            (base/name).mkdir(parents=True)
        database=base/'db/Kapowarr.db'
        before=None
        if version != 'fresh':
            with closing(sqlite3.connect(database)) as db:
                db.executescript({'63':SCHEMA_63,'69':SCHEMA_69}[version])
                db.execute("INSERT INTO config VALUES('database_version',?)",(int(version),))
                seed(db,base)
                names=['volume_external_ids','issue_external_ids','issues_files']
                if version=='69':
                    collections=CollectionStore(db.cursor());tree=collections.create('Preserved')
                    collections.add_local(tree['nodes'][0]['id'],tree['revision'],1)
                    db.execute('INSERT INTO release_events(id,issue_id) VALUES(1,1)')
                    orders=ReadingOrderStore(db.cursor());order=orders.create('Preserved sequence')
                    orders.add_local(order['id'],order['revision'],1)
                    preview=claim_preview(db.cursor(),2,PublicationRef('comicvine','101'),ClaimKind.COMPLETE,manual=True)
                    confirm_claim(db.cursor(),2,PublicationRef('comicvine','101'),ClaimKind.COMPLETE,preview['preview_token'],manual=True)
                    names+=['collections','collection_nodes','collection_memberships','release_events','reading_orders','reading_order_entries','bibliographic_content_claims','file_content_coverage']
                db.commit()
                before={t:db.execute('SELECT * FROM '+t).fetchall() for t in names}
        profile=None
        for attempt in range(2):
            with closing(socket.socket()) as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            log=base/f'run-{attempt}.log'
            env=dict(os.environ,KAPOWARR_RUN_MAIN='1',KAPOWARR_START_TYPE='130')
            with log.open('w',encoding='utf-8') as output:
                process=subprocess.Popen([sys.executable,'-u',str(REPO/'Kapowarr.py'),'-d',str(base/'db'),
                    '-l',str(base/'logs'),'-t',str(base/'downloads'),'-o','127.0.0.1','-p',str(port)],
                    cwd=REPO,env=env,stdout=output,stderr=subprocess.STDOUT)
                try:
                    origin=f'http://127.0.0.1:{port}'
                    for _ in range(240):
                        if process.poll() is not None:raise AssertionError(log.read_text())
                        try:
                            if requests.get(origin+'/settings/quality',timeout=1).status_code==200:break
                        except requests.RequestException:pass
                        time.sleep(.25)
                    else:raise AssertionError('Startup timeout')
                    with closing(sqlite3.connect(database)) as db:
                        assert db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0]==70
                        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                        assert not db.execute('PRAGMA foreign_key_check').fetchall()
                        if before and attempt==0:assert before=={t:db.execute('SELECT * FROM '+t).fetchall() for t in before}
                        if version=='fresh' and attempt==0:seed(db,base)
                        db.execute('UPDATE indexer_clients SET enabled=0')
                        if attempt==0:client=configure(db,fixture,base/'downloads')
                        db.commit()
                        key=db.execute("SELECT value FROM config WHERE key='api_key'").fetchone()[0]
                    def api(method,path,body=None):
                        response=requests.request(method,origin+'/api'+path,params={'api_key':key},json=body,timeout=30)
                        assert response.status_code==200,(path,response.status_code,response.text)
                        return response.json()['result']
                    assert requests.get(origin+'/api/quality-profiles',timeout=10).status_code==401
                    if attempt==0:
                        profile=api('POST','/quality-profiles',dict(name='Runtime fixture',policy=policy()))
                        api('POST','/volumes/1/quality',dict(profile_id=profile['id'],expected_profile_id=None))
                        task=api('POST','/quality-analysis',dict(file_ids=[1,2]))
                        for _ in range(160):
                            task=api('GET','/quality-analysis/'+task['id'])
                            if task['state'] not in ('queued','running'):break
                            time.sleep(.25)
                        assert task['state']=='complete',task
                        for iid,edge,expected in ((1,1200,'imported'),(2,900,'rejected')):
                            search=api('POST',f'/issues/{iid}/release-search',{})
                            selected=next(r for r in search['results'] if r['download_eligible'])
                            receipt=api('POST',f'/release-search/{search["search_id"]}/{selected["selection_id"]}',dict(action='download'))
                            path=base/'downloads'/f'Batman {iid:03} (2020).cbz';comic(path,edge)
                            original=(base/'library/Batman'/f'Batman {iid:03}.cbz').read_bytes()
                            downloads=DownloadStore(str(database))
                            try:
                                row=downloads.get(receipt['decision_id'])
                                fixture['remote']['queue'].pop(row['nzo_id'],None)
                                fixture['remote']['history'][row['nzo_id']]=dict(nzo_id=row['nzo_id'],status='Completed',storage='/complete/'+path.name,completed=1790000000)
                                poll_downloads(downloads,[client])
                            finally:downloads.close()
                            observed=time.time();runtime=IntakeRuntime(str(database),clock=lambda:observed)
                            runtime.tick();runtime.clock=lambda:observed+11;runtime.tick();runtime.tick()
                            history=api('GET',f'/issues/{iid}/acquisitions')
                            assert history['items'][0]['state']==expected,history
                            if expected=='rejected':assert (base/'library/Batman'/f'Batman {iid:03}.cbz').read_bytes()==original
                    else:
                        assert api('GET','/quality-profiles/'+str(profile['id']))['revision']==1
                    state=api('GET','/issues/1/quality')
                    assert state['cutoff_satisfied'] and state['files'][0]['facts']['short_edge']['p10']==1200
                    assert not state['upgrade_eligible']
                    history=api('GET','/issues/1/acquisitions')
                    assert len(history['items'])==2 and history['items'][-1]['reason']=='legacy'
                    assert api('GET','/issues/2/acquisitions')['items'][0]['state']=='rejected'
                    repeated=api('POST','/issues/2/release-search',{})
                    assert not any(r['download_eligible'] for r in repeated['results'])
                    assert len(fixture['remote']['uploads'])==2
                    for path in ('/collections','/calendar','/reading-orders','/maintenance','/wanted','/settings/quality'):
                        assert requests.get(origin+path,timeout=10).status_code==200
                    requests.post(origin+'/api/system/power/shutdown',params={'api_key':key},timeout=10)
                    process.wait(timeout=40);assert process.returncode==0
                finally:
                    if process.poll() is None:process.terminate();process.wait(timeout=15)
            logs=log.read_text(encoding='utf-8')
            assert 'Traceback' not in logs and '[ERROR]' not in logs,logs
        with closing(sqlite3.connect(database)) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            with patch('backend.internals.provider_identity.get_db',side_effect=db.cursor):
                assert not ProviderIdentityDB.audit(('comicvine','metron','gcd'))
        print(json.dumps(dict(start=version,end=70,normal_entrypoint=True,reopen=True,preserved=True,
            task_handler=True,analysis=True,legacy_provenance=True,upgrade_eligibility=True,successful_upgrade=True,false_hd_preserved=True,
            integrity='ok',foreign_keys=[],provider_identity_audit=[],errors=0)))


def seed(db,base):
    folder=base/'library/Batman';path=folder/'Batman 001.cbz';comic(path,600)
    db.execute('INSERT INTO root_folders VALUES(1,?)',(str(base/'library'),))
    db.execute("INSERT INTO volumes(id,comicvine_id,title,year,root_folder,folder,monitored) VALUES(1,100,'Batman',2020,1,?,1)",(str(folder),))
    db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
    db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(1,1,101,'1',1)")
    db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)',(str(path),path.stat().st_size))
    db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)');db.commit()
    path=folder/'Batman 002.cbz';comic(path,1800)
    db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(2,1,102,'2',2,1)")
    db.execute('UPDATE issues SET calculated_issue_number=1 WHERE id=1')
    db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,?)',(str(path),path.stat().st_size))
    db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(2,2)');db.commit()


if __name__=='__main__':
    for version in (sys.argv[1:] or ['fresh','69','63']):
        if version not in ('fresh','69','63'):raise SystemExit('Choose fresh, 69 or 63')
        run(version)

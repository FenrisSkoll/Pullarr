"""Actual Pullarr.py workers against loopback clients, disposable library only."""
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]

import TQualityUpgrade as quality_fixture
from fixtures.expanded_clients import configure, services


def run(kind, false_hd=False):
    fixture=quality_fixture.UpgradeTests(); fixture.setUp()
    runtime_db=None
    try:
        db=fixture.db
        db.execute("UPDATE config SET value=72 WHERE key='database_version'")
        db.execute('UPDATE indexer_clients SET enabled=0')
        if false_hd: quality_fixture.comic(fixture.h.source,900)
        original=fixture.old.read_bytes(); seed=fixture.h.source.read_bytes()
        with services() as remote:
            configure(db,remote,fixture.h.incoming,kind=kind)
            remote['completed']=True
            with closing(socket.socket()) as listener:
                listener.bind(('127.0.0.1',0)); port=listener.getsockname()[1]
            base=Path(fixture.h.dbpath).parent
            runtime_folder=base/'normal-db'; runtime_folder.mkdir()
            runtime_db=sqlite3.connect(runtime_folder/'Kapowarr.db',isolation_level=None)
            db.backup(runtime_db)
            db=runtime_db
            logs=base/'logs'; logs.mkdir(exist_ok=True)
            output_path=base/'normal-runtime.log'
            origin='http://127.0.0.1:'+str(port)
            env=dict(os.environ,KAPOWARR_RUN_MAIN='1',KAPOWARR_START_TYPE='130')
            with output_path.open('w',encoding='utf-8') as output:
                process=subprocess.Popen([sys.executable,'-u',str(ROOT/'Pullarr.py'),'-d',str(runtime_folder),
                    '-l',str(logs),'-t',str(fixture.h.incoming),'-o','127.0.0.1','-p',str(port)],
                    cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
                key=None
                try:
                    for _ in range(240):
                        if process.poll() is not None: raise AssertionError('Normal entrypoint exited')
                        try:
                            if requests.get(origin+'/wanted',timeout=1).status_code==200: break
                        except requests.RequestException: pass
                        time.sleep(.25)
                    else: raise AssertionError('Normal entrypoint startup timeout')
                    key=db.execute("SELECT value FROM config WHERE key='api_key'").fetchone()[0]
                    response=requests.post(origin+'/api/wanted',params={'api_key':key},
                        json=dict(action='search',volume_id=1,issue_id=1),timeout=10)
                    assert response.status_code==200
                    for _ in range(240):
                        row=db.execute('SELECT state,error FROM acquisition_provenance WHERE client_kind=? ORDER BY created_at DESC LIMIT 1',(kind,)).fetchone()
                        if row and row[0] in ('imported','rejected'): break
                        time.sleep(.5)
                    assert row and row[0]==('rejected' if false_hd else 'imported'),(kind,row)
                    assert remote['submitted']==1
                    assert fixture.old.read_bytes()==(original if false_hd else seed)
                    if kind=='qbittorrent': assert fixture.h.source.read_bytes()==seed
                    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                    assert not db.execute('PRAGMA foreign_key_check').fetchall()
                    requests.post(origin+'/api/system/power/shutdown',params={'api_key':key},timeout=10)
                    process.wait(timeout=45)
                finally:
                    if process.poll() is None:
                        process.terminate(); process.wait(timeout=15)
            content=output_path.read_text(encoding='utf-8')
            assert 'Traceback' not in content and '[ERROR]' not in content,'Unexpected normal-entrypoint error'
            print(f'Normal Pullarr workers: {kind}, false-HD={false_hd}, imported/rejected as expected, one submission, integrity/FKs PASS',flush=True)
    finally:
        if runtime_db is not None: runtime_db.close()
        fixture.doCleanups()


if __name__=='__main__':
    run('nzbget')
    run('qbittorrent')
    run('qbittorrent',True)

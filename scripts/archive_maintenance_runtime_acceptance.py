"""Normal entrypoint archive maintenance/reopen with disposable library only."""
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
import TArchiveMaintenance as fixtures


def main():
    fixture=fixtures.ArchiveJournalTests();fixture.setUp()
    db=None
    try:
        base=Path(fixture.h.dbpath).parent
        folder=base/'normal-db';folder.mkdir()
        db=sqlite3.connect(folder/'Kapowarr.db',isolation_level=None)
        fixture.h.db.backup(db)
        db.execute("UPDATE config SET value=72 WHERE key='database_version'")
        db.execute('UPDATE indexer_clients SET enabled=0')
        seed=base/'seed.cbr';os.link(fixture.path,seed);before=seed.read_bytes()
        jobs=[]
        for attempt in range(6):
            interrupted=None
            if attempt>=2:
                from backend.features.organization_archive import (
                    register_archive, review)
                from backend.features.organization_execution import \
                    OrganizationExecutor
                executor=OrganizationExecutor(str(folder/'Kapowarr.db'),(str(fixture.h.root),))
                try:
                    _,confirmation=review(executor,1)
                    interrupted=register_archive(executor,1,confirmation,'normal-restart-'+str(attempt))
                finally:executor.close()
                child="""import os,sys
from backend.features.organization_execution import OrganizationExecutor
def checkpoint(stage, job, ordinal):
    if stage=='after_effect' and ordinal==int(sys.argv[4]):os._exit(77)
executor=OrganizationExecutor(sys.argv[1],(sys.argv[2],),checkpoint=checkpoint)
executor.apply_job(sys.argv[3])
"""
                result=subprocess.run([sys.executable,'-c',child,str(folder/'Kapowarr.db'),str(fixture.h.root),
                    interrupted,str(attempt-2)],cwd=ROOT,capture_output=True,timeout=30)
                assert result.returncode==77,'Interrupted fixture did not reach checkpoint'
            with closing(socket.socket()) as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            logs=base/'logs';logs.mkdir(exist_ok=True)
            output_path=base/f'archive-runtime-{attempt}.log'
            origin='http://127.0.0.1:'+str(port)
            env=dict(os.environ,KAPOWARR_RUN_MAIN='1',KAPOWARR_START_TYPE='130')
            with output_path.open('w',encoding='utf-8') as output:
                process=subprocess.Popen([sys.executable,'-u',str(ROOT/('Kapowarr.py' if attempt==1 else 'Pullarr.py')),
                    '-d',str(folder),'-l',str(logs),'-t',str(fixture.h.incoming),'-o','127.0.0.1','-p',str(port)],
                    cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
                try:
                    for _ in range(240):
                        if process.poll() is not None:raise AssertionError('Normal entrypoint exited')
                        try:
                            if requests.get(origin+'/maintenance',timeout=1).status_code==200:break
                        except requests.RequestException:pass
                        time.sleep(.25)
                    else:raise AssertionError('Startup timeout')
                    key=db.execute("SELECT value FROM config WHERE key='api_key'").fetchone()[0]
                    def api(method,path,body=None):
                        response=requests.request(method,origin+'/api'+path,params={'api_key':key},json=body,timeout=30)
                        assert response.status_code==200,(path,response.status_code)
                        return response.json()['result']
                    def wait(task):
                        for _ in range(240):
                            result=api('GET','/maintenance/archives/tasks/'+task['id'])
                            if result['state'] not in ('queued','running'):
                                assert result['state']=='complete',result
                                return result
                            time.sleep(.25)
                        raise AssertionError('Archive task timeout')
                    assert api('GET','/maintenance/archives')['items'][0]['file_id']==1
                    if attempt==0:
                        scan=wait(api('POST','/maintenance/archives/scan',{'selected':[1]}))
                        assert scan['items'][0]['status']=='convertible',scan
                        assert seed.read_bytes()==before and fixture.path.read_bytes()==before
                        preview=wait(api('POST','/maintenance/archives/batch-preview',{'selected':[1]}))
                        result=wait(api('POST','/maintenance/archives/batch-apply',dict(review_id=preview['id'],selected=[1],confirmed=True)))
                        assert result['items'][0]['status']=='completed',result
                        jobs=[result['items'][0]['job_id']]
                    elif attempt==1:
                        assert [r[0] for r in db.execute('SELECT id FROM organization_jobs')]==jobs
                        healthy=wait(api('POST','/maintenance/archives/scan',{'selected':[1]}))
                        assert healthy['items'][0]['status']=='healthy',healthy
                    else:
                        def action(task):
                            for _ in range(240):
                                result=api('GET','/maintenance/action-tasks/'+task['id'])
                                if result['state'] not in ('queued','running'):
                                    assert result['state']=='complete',result
                                    return result['result']
                                time.sleep(.25)
                            raise AssertionError('Recovery task timeout')
                        path='/maintenance/history/organization/'+interrupted
                        preview=action(api('POST',path+'/recovery-preview',{}))['preview']
                        assert preview['eligible'],preview
                        result=action(api('POST',path+'/recover',dict(digest=preview['digest'],confirmed=True)))
                        assert result['entry']['state']=='complete',result
                        jobs.append(interrupted)
                    history=api('GET','/maintenance/history/organization/'+jobs[0])
                    assert history['entry']['operation']=='archive_normalization',history
                    assert seed.read_bytes()==before
                    assert fixture.path.with_suffix('.cbz').exists() and not fixture.path.exists()
                    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                    assert not db.execute('PRAGMA foreign_key_check').fetchall()
                    api('POST','/system/power/shutdown');process.wait(timeout=45)
                finally:
                    if process.poll() is None:process.terminate();process.wait(timeout=15)
            content=output_path.read_text(encoding='utf-8')
            assert 'Traceback' not in content and '[ERROR]' not in content,'Unexpected runtime error'
        print('Normal Pullarr/compatibility startup/reopen: archive scan/dry-run/shared CBR replacement, history, four process-exit checkpoint recoveries through HTTP after normal startup, no automatic rescan, stable ownership, source bytes and integrity/FKs PASS')
    finally:
        if db is not None:db.close()
        fixture.doCleanups()


if __name__=='__main__':main()

"""Measured synthetic diagnostics, never a performance SLA or user-data scan."""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import tracemalloc
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import TArchiveMaintenance as fixtures

from backend.base.definitions import RAR_EXECUTABLES
from backend.base.files import folder_path
from backend.base.helpers import get_os_type
from backend.features.archive_maintenance import ArchiveMaintenance
from backend.features.maintenance_history import MaintenanceHistory
from backend.implementations.archive_normalization import inspect, normalize


def main():
    results=[]
    def measure(name,operation,db=None):
        counts={'selects':0,'tool_calls':0}
        def trace(sql):
            if sql.lstrip().upper().startswith(('SELECT','WITH')):counts['selects']+=1
        if db is not None:db.set_trace_callback(trace)
        real=subprocess.Popen
        def process(*args,**kwargs):counts['tool_calls']+=1;return real(*args,**kwargs)
        connect=sqlite3.connect
        def connection(*args,**kwargs):
            value=connect(*args,**kwargs);value.set_trace_callback(trace);return value
        tracemalloc.start();start=time.perf_counter()
        with patch('subprocess.Popen',process), patch('sqlite3.connect',connection):value=operation()
        elapsed=time.perf_counter()-start;_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        if db is not None:db.set_trace_callback(None)
        encoded=json.dumps(value,default=str).encode()
        results.append(dict(operation=name,elapsed_seconds=round(elapsed,4),peak_python_bytes=peak,
                            json_bytes=len(encoded),**counts))
        return value
    fixture=fixtures.ArchiveJournalTests();fixture.setUp()
    try:
        service=ArchiveMaintenance(fixture.h.dbpath)
        for index in range(2,1001):
            path=fixture.h.folder/f'synthetic-{index}.cbz';fixtures.comic(path)
            fixture.h.db.execute('INSERT INTO files(id,filepath,size) VALUES(?,?,?)',(index,str(path),path.stat().st_size))
            fixture.h.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,1)',(index,))
        executor=service.executor()
        try:
            for count in (100,1000):
                measure(f'scan_{count}',lambda:[service._preview(executor,i,lambda:False) for i in range(1,count+1)],executor.store.db)
            measure('batch_preview_100',lambda:[service._preview(executor,i,lambda:False) for i in range(1,101)],executor.store.db)
            measure('maintenance_page_50',lambda:service.files(limit=50))
            for fid in range(2,52):
                from backend.features.organization_archive import (
                    register_archive, review)
                _,confirmation=review(executor,fid)
                executor.apply_job(register_archive(executor,fid,confirmation,'diagnostic-'+str(fid)))
            measure('history_page_50',lambda:MaintenanceHistory(fixture.h.dbpath).page(limit=50))
        finally:executor.close()
        with TemporaryDirectory() as temporary:
            root=Path(temporary); payload=fixtures.payloads()[0][1]
            for count in (25,250):
                source=root/f'pages-{count}.cbr'
                shutil.copyfile(ROOT/'tests/fixtures/archives'/f'synthetic-{count}.cbr',source)
                measure(f'cbr_analysis_{count}',lambda:inspect(str(source)))
                target=root/f'pages-{count}.cbz'
                receipt=measure(f'cbr_conversion_{count}',lambda:normalize(str(source),str(target)))
                results[-1].update(temp_bytes=target.stat().st_size,payload_hashes=count*2)
                if count==250:
                    measure('cbz_verify_250',lambda:inspect(str(target)))
                    measure('cbz_repack_250',lambda:normalize(str(target),str(root/'repacked.cbz')))
                    results[-1].update(temp_bytes=(root/'repacked.cbz').stat().st_size,payload_hashes=500)
        path=ROOT/'.devdata/phase9b-performance.json';path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(json.dumps(results,indent=2))
    finally:fixture.doCleanups()


if __name__=='__main__':main()

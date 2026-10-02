"""Deterministic diagnostic measurements; no SLA or live library/provider IO."""

import json
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO),str(REPO/'tests')]

from fixtures.quality import comic
from TQuality import policy

from backend.base.quality import classify
from backend.implementations.file_quality import analyze
from backend.internals.collections import CollectionStore
from backend.internals.db import DB_SCHEMA
from backend.internals.quality import QualityStore, canonical


def main():
    db=sqlite3.connect(':memory:');db.execute('PRAGMA foreign_keys=ON');db.executescript(DB_SCHEMA)
    db.execute("INSERT INTO root_folders VALUES(1,'fixture')")
    db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder,monitored) VALUES(1,100,'Fixture',1,'fixture/volume',1)")
    db.executemany("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(?,1,?,?,1)",((i,1000+i,str(i)) for i in range(1,1001)));db.commit()
    store=QualityStore(db.cursor());collections=CollectionStore(db.cursor())
    for number in range(10):
        tree=collections.create('Context '+str(number));collections.add_local(tree['nodes'][0]['id'],tree['revision'],1)
        store.assign('node',tree['nodes'][0]['id'],1,None)
    store.save('Upgrade diagnostic',policy(),identifier=1,revision=1)
    with TemporaryDirectory(prefix='kapowarr-quality-observation-') as directory:
        path=Path(directory)/'sample.cbz';comic(path,600)
        facts=analyze(str(path))
    # Identical synthetic bytes represented by separate admitted identities.
    db.executemany('INSERT INTO files(id,filepath,size) VALUES(?,?,?)',((i,f'fixture/{i}.cbz',facts['size']) for i in range(1,1001)))
    db.executemany('INSERT INTO issues_files(file_id,issue_id) VALUES(?,?)',((i,i) for i in range(1,1001)))
    db.executemany('INSERT INTO file_quality_assessments(file_id,fingerprint,analyzer,facts,observed_at) VALUES(?,?,?,?,0)',
        ((i,facts['sha256'],'fixture',canonical(facts)) for i in range(1,1001)));db.commit()
    db.executemany('''INSERT INTO acquisition_provenance
        (id,volume_id,issue_id,reason,state,release_title,source,claims,profile_snapshot,decision,created_at,updated_at)
        VALUES(?,1,1,'manual','failed','Fixture selection','fixture','{}','{}','{}',?,?)''',
        ((f'{i:032x}',i,i) for i in range(1,51)));db.commit()
    def measure(name,fn):
        statements=[];db.set_trace_callback(lambda sql:statements.append(sql) if sql.lstrip().upper().startswith(('SELECT','WITH')) else None)
        tracemalloc.start();start=time.perf_counter();result=fn();elapsed=time.perf_counter()-start
        _,peak=tracemalloc.get_traced_memory();tracemalloc.stop();db.set_trace_callback(None)
        print(json.dumps(dict(name=name,selects=len(statements),seconds=round(elapsed,6),peak_bytes=peak,json_bytes=len(json.dumps(result).encode()))))
    measure('1000 issues / 50-row quality page',lambda:store.issue_page(1,0,50))
    measure('effective profile / 10 Collection contexts',lambda:store.effective([1]))
    measure('acquisition history page',lambda:store.history(1))
    measure('upgrade eligibility',lambda:db.execute('SELECT * FROM quality_upgrade_issues LIMIT 50').fetchall())
    second=store.save('Other policy',policy())
    store.assign('node',tree['nodes'][0]['id'],second['id'],1)
    measure('conflicting inherited profiles',lambda:store.effective([1]))
    for count in (100,1000):
        measure(str(count)+' release classifications',lambda:[classify('Fixture #1 (HD-Digital)').preview() for _ in range(count)])
    with TemporaryDirectory(prefix='kapowarr-quality-metrics-') as directory:
        for pages in (25,250):
            path=Path(directory)/f'{pages}.cbz';comic(path,1200,pages,codec='JPEG')
            measure(str(pages)+' JPEG page header/CRC analysis',lambda:analyze(str(path)))
    db.close()


if __name__=='__main__':main()

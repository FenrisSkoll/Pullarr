"""Disposable bounded diagnostics, no provider network or large artifacts."""

import json
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO),str(REPO/'tests')]

from fixtures.reading_orders import CBL, FixtureLists

from backend.base.reading_orders import export_cbl, parse_cbl
from backend.internals.db import DB_SCHEMA
from backend.internals.reading_orders import ReadingOrderStore


def main():
    db=sqlite3.connect(':memory:'); db.executescript(DB_SCHEMA)
    db.execute("INSERT INTO root_folders VALUES(1,'fixture-root')")
    db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder,monitored) VALUES(1,100,'One',1,'fixture-volume',1)")
    db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(1,1,101,'1',1)"); db.commit()
    store=ReadingOrderStore(db.cursor())
    def measure(name, action):
        selects=[]; db.set_trace_callback(lambda sql: selects.append(1) if sql.lstrip().upper().startswith(('SELECT','WITH')) else None)
        tracemalloc.start(); start=time.perf_counter(); result=action(); elapsed=time.perf_counter()-start
        peak=tracemalloc.get_traced_memory()[1]; tracemalloc.stop(); db.set_trace_callback(None)
        print(json.dumps(dict(name=name,selects=len(selects),seconds=round(elapsed,6),peak_bytes=peak,json_bytes=len(json.dumps(result).encode()))))
        return result
    for count in (100,1000,2000):
        model=parse_cbl(CBL); model['entries']=[model['entries'][0]]*count; raw=export_cbl(model)
        resolved=measure(f'{count}-entry parse/match',lambda:store.match(parse_cbl(raw)))
        order=store.accept_model(model,resolved,dict(kind='cbl_import'))
        measure(f'{count}-entry order / 50-row page',lambda:store.entries(order['id'],0,50))
        measure(f'{count}-entry owned/missing filter',lambda:store.entries(order['id'],0,50,'missing'))
        order=store.attach(order['id'],order['revision'],'cbl_url','https://example.com/fixture.cbl')
        model['entries']=model['entries'][1:]+[parse_cbl(CBL)['entries'][3]]
        store.observe(order['source']['id'],0,dict(model=model,digest='a'*64))
        measure(f'{count}-entry source diff / 50 rows',lambda:store.pending(order['source']['id']))
    measure('list page',lambda:store.page())
    measure('provider fixture / canonical match',lambda:store.match(FixtureLists().fetch('42')['model']))
    db.close()


if __name__=='__main__': main()

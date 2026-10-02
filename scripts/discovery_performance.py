"""Disposable, deterministic Discover diagnostics; no remote source requests."""

import json
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests')]

from fixtures.discovery import FixtureSource
from TDiscover import listing, rss

from backend.base.definitions import GCDownloadService
from backend.base.discovery import parse_feed, parse_listing
from backend.features.direct_downloads import resolve_offerings
from backend.features.discovery import Discover
from backend.features.discovery_matching import project
from backend.implementations.direct_download_source import (DDLSourceConfig,
                                                            GetComicsSource)
from backend.implementations.release_candidates import adapt_ddl_result
from backend.internals.db import DB_SCHEMA
from backend.internals.discovery import DiscoveryStore


def main():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(DB_SCHEMA)
    store = DiscoveryStore(db.cursor())
    queries = []
    db.set_trace_callback(lambda sql: queries.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)

    def measure(name, action):
        queries.clear()
        tracemalloc.start()
        started = time.perf_counter()
        result = action()
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(json.dumps(dict(case=name, selects=len(queries), seconds=round(elapsed, 6),
            peak_bytes=peak, json_bytes=len(json.dumps(result).encode()))), flush=True)
        return result

    for size in (100, 500):
        raw = rss([(i, f'Batman #{i} (2020) (HD-Digital)') for i in range(size)])
        measure(f'parse_dedupe_{size}', lambda: store.ingest(parse_feed(raw)))
    for size in (10000, 25000):
        for start in range(store.status()['retained'], size, 500):
            store.ingest(parse_feed(rss([(i, f'Batman #{i} (2020) (HD-Digital)') for i in range(start, min(start+500, size))])))
        with patch('backend.features.discovery.get_db', side_effect=db.cursor):
            owner = Discover(enqueue=lambda task: None)
            measure(f'page_{size}_50', owner.page)
            for key, value in (('category', 'Other Comics'), ('q', '#123'), ('state', 'missing'), ('state', 'upgrade')):
                measure(f'{size}_{key}_{value}', lambda key=key, value=value: owner.page(**{key:value}))
    db.execute("INSERT INTO root_folders VALUES(1,'/fixture')")
    db.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder,monitored) VALUES(1,101,'Batman',2020,1,1,'/fixture/Batman',1)")
    db.executemany('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(?,1,?,?,?,1)', ((i, 10000+i, str(i), i) for i in range(1,1001)))
    db.commit()
    values = parse_feed(rss([(i, f'Batman #{i} (2020) (HD-Digital)') for i in range(1,501)]))
    for size in (50,100,1000):
        measure(f'local_match_{size}', lambda size=size: [project(db.cursor(), values[:min(500,size)]) for _ in range(max(1,size//500))])
    raw = listing([(i, f'Batman #{i} (2020)') for i in range(100)])
    measure('bounded_catchup_4x100', lambda: [parse_listing(raw) for _ in range(4)])
    source=GetComicsSource(DDLSourceConfig(1,'GetComics','https://getcomics.org',tuple(s.value for s in GCDownloadService)),http=FixtureSource())
    record=dict(indexer_id=1,indexer_title='GetComics',link='https://getcomics.org/other-comics/1/',display_title='Batman #1 (2020) (HD-Digital)',size=-1)
    selected=SimpleNamespace(source=source,raw=record,evaluation=SimpleNamespace(candidate=adapt_ddl_result(record)))
    measure('existing_offering_parser',lambda: [dict(id=key,title=value['web_sub_title'],size=value['size']) for key,value in resolve_offerings(selected,[]).items()])
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    db.close()


if __name__ == '__main__':
    main()

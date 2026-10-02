"""Disposable local-only Calendar query and bounded fixture-sync diagnostics."""

import asyncio
import json
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests')]

from fixtures.release_calendar import CalendarFixture

from backend.implementations.metadata.release_dates import acquire_dates
from backend.internals.collections import CollectionStore
from backend.internals.db import DB_SCHEMA
from backend.internals.issue_facts import mapped_facts, write_facts
from backend.internals.release_calendar import CalendarStore


def measure(db, name, fn):
    selects = []
    db.set_trace_callback(lambda sql: selects.append(sql) if sql.lstrip().upper().startswith(('SELECT', 'WITH')) else None)
    tracemalloc.start(); started = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop(); db.set_trace_callback(None)
    print(json.dumps(dict(case=name, selects=len(selects), elapsed_seconds=round(elapsed, 6), peak_bytes=peak,
        json_bytes=len(json.dumps(result).encode()))))
    return result


def main():
    db = sqlite3.connect(':memory:')
    try:
        db.executescript(DB_SCHEMA)
        db.execute("INSERT INTO root_folders VALUES(1,'fixture')")
        db.execute("INSERT INTO volumes(id,title,comicvine_id,root_folder,folder,monitored) VALUES(1,'Fixture',100,1,'fixture',1)")
        for identity in range(1, 1001):
            value = None if identity % 10 == 0 else '2027-03-17'
            db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,date,monitored) VALUES(?,1,?,?,?,1)',
                (identity, 1000 + identity, str(identity), value))
            write_facts(db.cursor(), identity, mapped_facts(str(identity), value, 'fixture'))
        db.commit()
        store = CalendarStore(db.cursor())
        collections = CollectionStore(db.cursor())
        tree = collections.create('Fixture Collection', monitoring='monitored')
        node = tree['nodes'][0]['id']
        collections.add_local(node, 0, 1)
        for name, kwargs in (('1000-retained-50-page', {}), ('TBA-50', {'unknown': True}), ('Collection-filter', {'collection': tree['id']})):
            measure(db, name, lambda kwargs=kwargs: store.page(start='2027-01-01', end='2027-12-31', **kwargs))
        measure(db, 'canonical-detail', lambda: store.detail('issue:1'))
        publications = []
        for identity in range(9000, 9010):
            collections.propose(node, [dict(provider='comicvine', provider_id=str(identity), title='External fixture', year=2027)], 'fixture')
            suggestion = next(s for s in collections.suggestions(node)['items'] if s['provider_id'] == str(identity))
            publications.append(collections.decide(suggestion['id'], 0, collections.tree(tree['id'])['revision'], 'accepted')['publication_id'])
        def sync():
            subjects = store.subjects()
            for subject in subjects:
                acquired = asyncio.run(acquire_dates(subject['provider'], subject['provider_id'], time.time()))
                store.persist(subject, acquired)
            return dict(subjects=len(subjects), provider_requests='one bounded fixture fetch per exact known identity')
        with patch.dict('backend.implementations.metadata.registry.PROVIDERS', comicvine=CalendarFixture):
            measure(db, '11-known-subject-fixture-sync', sync)
        measure(db, 'external-evidence-detail', lambda: store.detail('publication:' + str(publications[0])))
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        db.close()


if __name__ == '__main__':
    main()

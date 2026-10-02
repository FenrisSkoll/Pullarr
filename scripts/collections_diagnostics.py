"""Disposable bounded local query diagnostics, never provider latency SLAs."""

import json
import sqlite3
import sys
import tracemalloc
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.internals.collections import CollectionStore
from backend.internals.db import DB_SCHEMA


def main():
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(DB_SCHEMA)
    store = CollectionStore(db.cursor())
    root = store.create('Collection 0', monitoring='monitored')['nodes'][0]['id']
    def measure(name, call):
        statements = []
        db.set_trace_callback(statements.append)
        tracemalloc.start(); started = perf_counter()
        result = call()
        elapsed = perf_counter() - started
        _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop(); db.set_trace_callback(None)
        print(json.dumps(dict(name=name, selects=sum(s.lstrip().upper().startswith(('SELECT', 'WITH')) for s in statements),
            seconds=round(elapsed, 6), peak_bytes=peak, response_bytes=len(json.dumps(result).encode()))))
    for i in range(1, 100):
        store.create('Collection ' + str(i))
        if i == 9:
            measure('10 Collections list', store.page)
    measure('100 Collections list page50', store.page)
    for i in range(1000):
        # Large diagnostic fixture, not an admission API. Accepted synthetic refs.
        db.execute("INSERT INTO collection_publications(title,kind_source,created_at) VALUES(?,'unknown',0)", ('External ' + str(i),))
        pid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.execute("INSERT INTO collection_publication_refs VALUES(?,'gcd',?,'provider_search')", (pid, str(i + 1)))
        db.execute("INSERT INTO collection_memberships VALUES(?,?,0,'','manual',?,0)", (root, pid, json.dumps({'version': 'collection-membership/v1', 'kind': 'manual'})))
        if i == 99:
            db.commit(); measure('100 publications page50', lambda: store.publications(1))
    db.commit()
    measure('1000 publications page50', lambda: store.publications(1))
    measure('1000 publication completeness', lambda: store.tree(1))
    measure('monitoring handoff page50', store.calendar_page)
    store.propose(root, [dict(provider='gcd', provider_id=str(i), title='Suggestion', year=None) for i in range(1, 101)], 'fixture')
    measure('suggestion page50 exact refs/local resolution', lambda: store.suggestions(root))
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    db.close()


if __name__ == '__main__':
    main()

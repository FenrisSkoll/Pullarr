"""Bounded Library projection using synthetic in-memory data only."""
import json
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from TPullarrUI import LibraryPaging

from backend.implementations.volumes import Library


def main():
    fixture = LibraryPaging()
    fixture.setUp()
    try:
        fixture.db.executemany(
            'INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder,monitored) '
            'VALUES(?,?,?,2020,1,1,?,1)',
            [(i, i, f'Demo {i:04}', f'/synthetic/{i}') for i in range(104, 1001)]
        )
        for name, kwargs in [('1000 volumes / 50 rows', {}), ('title filter / 50 rows', {'query': 'Demo'})]:
            statements = []
            fixture.db.set_trace_callback(statements.append)
            tracemalloc.start()
            start = time.perf_counter()
            result = Library.get_public_volumes(limit=50, **kwargs)
            elapsed = time.perf_counter() - start
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            print(json.dumps(dict(case=name, selects=len(statements), seconds=round(elapsed, 6),
                                  peak_bytes=peak, json_bytes=len(json.dumps(result)), rows=len(result))))
            assert len(result) == 50 and len(statements) == 1
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()

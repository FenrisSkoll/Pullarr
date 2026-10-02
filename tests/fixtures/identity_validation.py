"""Disposable integration fixture/receipt and migration performance utility.

Run from repository root: python tests/fixtures/identity_validation.py --help
Never overwrites an existing database. Does not read configuration secrets.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

# CLI execution needs both repository and tests roots before local imports.
# isort: off
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures.provider_identity import build_legacy, receipt
from flask import Flask

from backend.internals.db import DBConnection
from backend.internals.db_migration import DatabaseMigrationHandler
# isort: on


def digest(rows):
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def generate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'Kapowarr.db'
    # Atomic exclusive creation prevents accidentally clobbering any database.
    with path.open('xb'):
        pass
    with closing(sqlite3.connect(str(path))) as db:
        db.execute('PRAGMA foreign_keys=ON')
        build_legacy(db)
        # The unit golden dataset preserves a queued-download relationship.
        # This runtime fixture is deliberately idle: do not launch downloads.
        db.execute('DELETE FROM download_queue')
        # Prevent synthetic fixtures from running unrelated scheduled work.
        db.executemany('INSERT INTO task_intervals VALUES (?,?,?)', [
            ('update_all', '0 * * * *', 4102444800),
            ('rss_sync', '0,30 * * * *', 4102444800),
            ('backup_db', '0 0 * * 1', 4102444800)])
        db.commit()
        columns, rows = receipt(db)
        # Startup inserts default config and schedules; compare the pre-existing
        # config subset separately without exposing values in output.
        saved = {
            'columns': columns, 'hashes': {
                k: digest(v) for k, v in rows.items()}, 'counts': {
                k: len(v) for k, v in rows.items()}}
        (directory / 'receipt.json').write_text(json.dumps(saved, indent=2), encoding='utf-8')
    print('Created synthetic schema-51 fixture:', path)


def verify(directory):
    saved = json.loads((directory / 'receipt.json').read_text(encoding='utf-8'))
    with closing(sqlite3.connect((directory / 'Kapowarr.db').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        _, rows = receipt(db, saved['columns'])
        # Compare only the known pre-existing fake configuration entries.
        rows['config'] = [r for r in rows['config'] if r[0] == 'api_key']
        for table, data in rows.items():
            assert digest(data) == saved['hashes'][table], table
        assert db.execute(
            "SELECT value FROM config WHERE key='database_version'").fetchone() == (52,)
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT volume_id,provider,provider_id,provenance,last_fetch FROM volume_external_ids ORDER BY volume_id').fetchall(
        ) == db.execute("SELECT id,'comicvine',CAST(comicvine_id AS TEXT),'migration',last_cv_fetch FROM volumes ORDER BY id").fetchall()
        assert db.execute('SELECT issue_id,provider,provider_id,provenance FROM issue_external_ids ORDER BY issue_id').fetchall(
        ) == db.execute("SELECT id,'comicvine',CAST(comicvine_id AS TEXT),'migration' FROM issues ORDER BY id").fetchall()
        print('Lossless receipt verified:', saved['counts'])
        print('Schema 52; integrity ok; foreign_key_check empty; identities 3/9.')


def benchmark():
    for volumes, issues in ((100, 50), (1000, 50)):
        with Flask(__name__).app_context():
            db = DBConnection(db_file=':memory:')
            try:
                build_legacy(db, volumes, issues, edges=False)
                before = receipt(db)
                started = perf_counter()
                with patch('backend.internals.db_migration.get_db', side_effect=db.cursor):
                    DatabaseMigrationHandler.handlers[51]()
                elapsed = perf_counter() - started
                assert receipt(db, before[0])[1] == before[1]
                assert db.execute('PRAGMA foreign_key_check').fetchall() == []
                assert db.execute('PRAGMA integrity_check').fetchall() == [
                                  ('ok',)]
                print(
                    f'{volumes} volumes / {volumes * issues} issues: {elapsed:.4f}s (in-memory migration, excluding framework VACUUM)')
            finally:
                db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('generate', 'verify', 'benchmark'))
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args()
    if args.action == 'benchmark':
        benchmark()
    elif args.directory is None:
        parser.error('--directory is required')
    elif args.action == 'generate':
        generate(args.directory)
    else:
        verify(args.directory)

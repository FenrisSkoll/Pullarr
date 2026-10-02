"""Schema-52 Docker startup fixture and lossless post-startup receipt.

Never overwrites a DB. Runtime outputs belong under ignored .devdata only.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

# isort: off
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures.provider_identity import receipt
from fixtures.provider_schema52 import build_schema52
from flask import Flask
from backend.internals.db import DBConnection
from backend.internals.provider_identity import ProviderIdentityDB
# isort: on


def digest(data):
    return hashlib.sha256(repr(data).encode()).hexdigest()


def generate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'Kapowarr.db'
    with path.open('xb'):
        pass
    with Flask(__name__).app_context(), closing(DBConnection(db_file=str(path))) as db:
        build_schema52(db)
        db.execute('DELETE FROM download_queue')
        db.executemany('INSERT INTO task_intervals VALUES (?,?,?)', [
            ('update_all', '0 * * * *', 4102444800),
            ('rss_sync', '0,30 * * * *', 4102444800),
            ('backup_db', '0 0 * * 1', 4102444800)])
        db.commit()
        columns, rows = receipt(db)
        saved = {'columns': columns, 'hashes': {k: digest(v) for k, v in rows.items()},
                 'counts': {k: len(v) for k, v in rows.items()}}
        (directory / 'receipt.json').write_text(json.dumps(saved, indent=2), encoding='utf-8')
    print('Created synthetic schema-52 fixture:', path)


def verify(directory):
    saved = json.loads(
        (directory / 'receipt.json').read_text(encoding='utf-8'))
    with closing(sqlite3.connect((directory / 'Kapowarr.db').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        _, rows = receipt(db, saved['columns'])
        rows['config'] = [r for r in rows['config'] if r[0] == 'api_key']
        for table, data in rows.items():
            assert digest(data) == saved['hashes'][table], table
        assert db.execute(
            "SELECT value FROM config WHERE key='database_version'").fetchone() == (53,)
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        with patch('backend.internals.provider_identity.get_db', side_effect=db.cursor):
            assert ProviderIdentityDB.audit() == []
        print('Schema 53; complete logical receipt preserved:',
              saved['counts'])
        print('Integrity ok; FK empty; provider diagnostics clean.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('generate', 'verify'))
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    (generate if args.action == 'generate' else verify)(args.directory)

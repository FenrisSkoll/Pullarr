"""Disposable normal-entrypoint 8I acceptance. Never opens the development DB.

Run with the repository virtualenv; optional argument fresh, 65 or 63.
All fixtures contain synthetic identities and no provider credentials.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import requests
from flask import Flask

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.implementations.metadata.registry import PROVIDERS
from backend.internals.db import (SCHEMA_63, SCHEMA_65, SCHEMA_66,
                                  DBConnection, set_db_location)
from backend.internals.db_migration import DatabaseMigrationHandler
from backend.internals.provider_identity import ProviderIdentityDB


def seed(db, library, comic):
    db.execute('INSERT INTO root_folders(id,folder) VALUES(1,?)', (str(library),))
    db.execute("INSERT INTO volumes(id,title,root_folder,folder,comicvine_id,volume_number,year) VALUES(1,'Example',1,?,100,1,2020)", (str(comic.parent),))
    db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(1,1,101,'1',1)")
    db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(comic), comic.stat().st_size))
    db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
    db.commit()


def preserved(db):
    return {table: db.execute('SELECT ' + columns + ' FROM ' + table).fetchall()
            for table, columns in (
                ('volumes', 'id,title,year,comicvine_id,root_folder,folder'),
                ('issues', 'id,volume_id,comicvine_id,issue_number'),
                ('files', 'id,filepath,size'), ('issues_files', '*'),
                ('volume_external_ids', '*'), ('issue_external_ids', '*'))}


def run(version):
    with TemporaryDirectory(prefix='kapowarr-8i-startup-') as directory:
        base = Path(directory)
        for name in ('db', 'logs', 'downloads', 'library/Example'):
            (base / name).mkdir(parents=True)
        comic = base / 'library/Example/wrong.cbz'
        with ZipFile(comic, 'w') as archive:
            archive.writestr('001.jpg', b'disposable normal entrypoint acceptance')
        digest = sha256(comic.read_bytes()).hexdigest()
        database = base / 'db/Kapowarr.db'
        before = None
        if version != 'fresh':
            with closing(sqlite3.connect(database)) as db:
                db.executescript({'63': SCHEMA_63, '65': SCHEMA_65, '66': SCHEMA_66}[version])
                db.execute("INSERT INTO config VALUES('database_version',?)", (int(version),))
                db.execute("INSERT INTO config VALUES('file_naming','Fixture {issue_number}')")
                seed(db, base / 'library', comic)
                before = preserved(db)
        for attempt in range(2):
            with closing(socket.socket()) as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            log = base / f'startup-{attempt}.txt'
            env = dict(os.environ, KAPOWARR_RUN_MAIN='1', KAPOWARR_START_TYPE='130')
            with log.open('w', encoding='utf-8') as output:
                process = subprocess.Popen([sys.executable, '-u', str(REPO / 'Kapowarr.py'),
                    '-d', str(base / 'db'), '-l', str(base / 'logs'), '-t', str(base / 'downloads'),
                    '-o', '127.0.0.1', '-p', str(port)], cwd=REPO, env=env,
                    stdout=output, stderr=subprocess.STDOUT)
                try:
                    origin = f'http://127.0.0.1:{port}'
                    for _ in range(240):
                        if process.poll() is not None:
                            raise AssertionError('normal startup exited: ' + log.read_text())
                        try:
                            if requests.get(origin + '/maintenance', timeout=1).status_code == 200:
                                break
                        except requests.RequestException:
                            pass
                        time.sleep(.25)
                    else:
                        raise AssertionError('normal startup timeout')
                    with closing(sqlite3.connect(database)) as db:
                        assert db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0] == DatabaseMigrationHandler.latest_db_version()
                        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                        assert not db.execute('PRAGMA foreign_key_check').fetchall()
                        if version != 'fresh':
                            assert db.execute("SELECT value FROM config WHERE key='file_naming'").fetchone()[0] == 'Fixture {issue_number}'
                        if before is not None:
                            assert before == preserved(db)
                        elif attempt == 0:
                            seed(db, base / 'library', comic)
                            before = preserved(db)
                        assert db.execute('SELECT COUNT(*) FROM active_files').fetchone()[0] == 1
                        assert db.execute('SELECT COUNT(*) FROM quarantined_files').fetchone()[0] == 0
                        key = db.execute("SELECT value FROM config WHERE key='api_key'").fetchone()[0]
                    def api(method, path, body=None):
                        response = requests.request(method, origin + '/api/maintenance' + path,
                            params={'api_key': key}, json=body, timeout=30)
                        assert response.status_code == 200, (path, response.status_code)
                        return response.json()['result']
                    assert requests.get(origin + '/api/maintenance/history', timeout=5).status_code == 401
                    api('GET', '/history')
                    scan = api('POST', '/scans', dict(scope=dict(kind='library', ids=[]), level='inventory'))
                    for _ in range(120):
                        status = api('GET', '/scans/' + scan['id'])
                        if status['state'] not in ('queued', 'running'):
                            break
                        time.sleep(.25)
                    assert status['state'] == 'complete', status
                    api('GET', '/scans/' + scan['id'] + '/findings')
                    worklist = api('POST', '/worklists', dict(scan_id=scan['id']))
                    if attempt == 0:
                        def wait_task(delivery, kind='action-tasks'):
                            for _ in range(120):
                                outcome = api('GET', '/' + kind + '/' + delivery['id'])
                                if outcome['state'] not in ('queued', 'running'):
                                    break
                                time.sleep(.25)
                            assert outcome['state'] == 'complete', outcome
                            return outcome
                        path = '/worklists/' + worklist['id']
                        finding = next(item['finding']['id'] for item in api('GET', path + '/items')['items']
                                       if item['finding']['code'] == 'filename_deviation')
                        wait_task(api('POST', path + '/revise', dict(revision=0, edits=[dict(
                            finding_id=finding, selected=True, excluded=False, action='rename')])), 'review-tasks')
                        worklist = api('GET', path)
                        child = wait_task(api('POST', '/rename/reviews', dict(worklist_id=worklist['id'],
                            revision=worklist['revision'], digest=worklist['manifest_digest'], selected=[finding])))['result']
                        review = api('GET', '/rename/reviews/' + child['id'])
                        confirmation = {key: review[key] for key in ('revision', 'digest', 'origin', 'selected')}
                        batch = wait_task(api('POST', '/rename/reviews/' + child['id'] + '/apply',
                            dict(confirmation, confirmed=True)))['result']
                        job = api('GET', '/batches/' + batch['id'])['items'][0]['id']
                        inverse = wait_task(api('POST', '/history/organization/' + job + '/inverse-preview', {}))['result']['preview']
                        assert inverse['eligible'], inverse
                        result = wait_task(api('POST', '/history/organization/' + job + '/inverse',
                            dict(digest=inverse['digest'], confirmed=True)))['result']
                        assert result['entry']['state'] == 'complete'
                        assert comic.exists() and sha256(comic.read_bytes()).hexdigest() == digest
                    collection_response = requests.get(origin + '/api/collections', params={'api_key': key}, timeout=10)
                    assert collection_response.status_code == 200
                    for route in ('/', '/collections', '/add', '/library-import', '/volumes/1', '/volumes/1/provider-switch', '/wanted', '/activity/history', '/settings/metadata'):
                        assert requests.get(origin + route, timeout=10).status_code == 200, route
                    requests.post(origin + '/api/system/power/shutdown', params={'api_key': key}, timeout=10)
                    process.wait(timeout=30)
                    assert process.returncode == 0, process.returncode
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=15)
            text = log.read_text(encoding='utf-8')
            assert 'Traceback' not in text and '[ERROR]' not in text, text
            set_db_location(str(base / 'db'))
            with Flask(__name__).app_context():
                assert ProviderIdentityDB.audit(PROVIDERS.keys()) == []
                DBConnection.close_connection_of_thread()
        assert sha256(comic.read_bytes()).hexdigest() == digest
        print(json.dumps(dict(start=version, end=DatabaseMigrationHandler.latest_db_version(), normal_entrypoint=True, reopen=True,
            integrity='ok', foreign_keys=[], identity_and_domain_preserved=True,
            bytes_preserved=True, task_handler_scan=True, maintenance_http=True, rename_inverse=True,
            unexpected_errors=0)))


if __name__ == '__main__':
    for version in (sys.argv[1:] or ['fresh', '65', '63']):
        if version not in ('fresh', '66', '65', '63'):
            raise SystemExit('Choose fresh, 66, 65 or 63')
        run(version)

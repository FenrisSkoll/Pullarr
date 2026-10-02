"""Actual Kapowarr.py entrypoint, disposable fresh/67/63 migration and reopen."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.internals.collections import CollectionStore
from backend.internals.db import SCHEMA_63, SCHEMA_67
from backend.internals.issue_facts import mapped_facts, write_facts
from backend.internals.provider_identity import ProviderIdentityDB


def run(version):
    with TemporaryDirectory(prefix='kapowarr-8k-startup-') as directory:
        base = Path(directory)
        for name in ('db', 'logs', 'downloads', 'library/Local'):
            (base / name).mkdir(parents=True)
        database = base / 'db/Kapowarr.db'
        original = None
        if version != 'fresh':
            with closing(sqlite3.connect(database)) as db:
                db.executescript({'63': SCHEMA_63, '67': SCHEMA_67}[version])
                db.execute("INSERT INTO config VALUES('database_version',?)", (int(version),))
                db.execute("INSERT INTO config VALUES('file_naming','Calendar preserved {issue_number}')")
                db.execute('INSERT INTO root_folders VALUES(1,?)', (str(base / 'library'),))
                db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder,monitored) VALUES(1,100,'Local',1,?,1)", (str(base / 'library/Local'),))
                db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
                db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,date,monitored) VALUES(1,1,101,'1','2027-03-17',1)")
                write_facts(db.cursor(), 1, mapped_facts('1', '2027-03-17', 'fixture'))
                db.commit()
                if version == '67':
                    store = CollectionStore(db.cursor())
                    tree = store.create('Preserved Collection', monitoring='monitored')
                    store.add_local(tree['nodes'][0]['id'], tree['revision'], 1)
                original = {t: db.execute('SELECT * FROM ' + t).fetchall() for t in ('volume_external_ids', 'issue_external_ids', 'issues_files')}
                if version == '67':
                    original.update({t: db.execute('SELECT * FROM ' + t).fetchall() for t in ('collections', 'collection_nodes', 'collection_memberships', 'collection_publications', 'collection_publication_refs')})
        subject_id = None
        for attempt in range(2):
            with closing(socket.socket()) as listener:
                listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
            log = base / f'startup-{attempt}.txt'
            env = dict(os.environ, KAPOWARR_RUN_MAIN='1', KAPOWARR_START_TYPE='130',
                PYTHONPATH=os.pathsep.join((str(REPO / 'tests/fixtures/calendar_startup'), str(REPO / 'tests'), str(REPO))))
            with log.open('w', encoding='utf-8') as output:
                process = subprocess.Popen([sys.executable, '-u', str(REPO / 'Kapowarr.py'), '-d', str(base / 'db'),
                    '-l', str(base / 'logs'), '-t', str(base / 'downloads'), '-o', '127.0.0.1', '-p', str(port)],
                    cwd=REPO, env=env, stdout=output, stderr=subprocess.STDOUT)
                try:
                    origin = f'http://127.0.0.1:{port}'
                    for _ in range(240):
                        if process.poll() is not None:
                            raise AssertionError('Normal startup exited: ' + log.read_text())
                        try:
                            if requests.get(origin + '/calendar', timeout=1).status_code == 200:
                                break
                        except requests.RequestException:
                            pass
                        time.sleep(.25)
                    else:
                        raise AssertionError('Normal startup timed out')
                    with closing(sqlite3.connect(database)) as db:
                        assert db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0] == 68
                        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                        assert not db.execute('PRAGMA foreign_key_check').fetchall()
                        if original and attempt == 0:
                            assert original == {t: db.execute('SELECT * FROM ' + t).fetchall() for t in original}
                        key = db.execute("SELECT value FROM config WHERE key='api_key'").fetchone()[0]
                    def api(method, path, body=None):
                        response = requests.request(method, origin + '/api' + path, params={'api_key': key}, json=body, timeout=30)
                        assert response.status_code in (200, 201), (path, response.status_code, response.text)
                        return response.json()['result']
                    def wait(delivery, family):
                        for _ in range(120):
                            status = api('GET', family + delivery['id'])
                            if status['state'] not in ('queued', 'running'):
                                assert status['state'] == 'complete', status
                                return status
                            time.sleep(.25)
                        raise AssertionError('Task did not finish')
                    assert requests.get(origin + '/api/calendar', timeout=10).status_code == 401
                    if attempt == 0:
                        if version == 'fresh':
                            api('POST', '/rootfolder', {'folder': str(base / 'library')})
                        tree = api('POST', '/collections', dict(title='Runtime monitored external', description='', monitoring='monitored'))
                        node = tree['nodes'][0]['id']
                        search = api('POST', f'/collections/nodes/{node}/search', dict(query='fixture', provider='comicvine', suggestions=True))
                        wait(search, '/collections/tasks/')
                        suggestion = api('GET', f'/collections/nodes/{node}/suggestions')['items'][0]
                        accepted = api('POST', '/collections/suggestions/' + suggestion['id'] + '/decision',
                            dict(revision=0, collection_revision=tree['revision'], decision='accepted'))
                        publication = accepted['publication_id']
                        sync = api('POST', '/calendar/refresh', dict(provider='all'))
                        wait(sync, '/calendar/tasks/')
                        subject_id = 'publication:' + str(publication)
                        event = api('GET', '/calendar/events/' + subject_id)
                        assert event['status'] == 'external' and event['effective']['date'] == '2027-03-24'
                        added = api('POST', f'/collections/publications/{publication}/add', dict(root_id=1, provider='comicvine', confirmed=True))
                        wait(added, '/collections/tasks/')
                    event = api('GET', '/calendar/events/' + subject_id)
                    assert event['status'] == 'in_library' and event['id'] == subject_id
                    assert api('GET', '/calendar?from=2027-01-01&to=2027-12-31')['items']
                    for route in ('/calendar', '/collections', '/maintenance', '/', '/wanted', '/settings/metadata'):
                        assert requests.get(origin + route, timeout=10).status_code == 200
                    requests.post(origin + '/api/system/power/shutdown', params={'api_key': key}, timeout=10)
                    process.wait(timeout=30)
                    assert process.returncode == 0
                finally:
                    if process.poll() is None:
                        process.terminate(); process.wait(timeout=15)
            output_text = log.read_text(encoding='utf-8')
            assert 'Traceback' not in output_text and '[ERROR]' not in output_text, output_text
        with closing(sqlite3.connect(database)) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            assert not db.execute('SELECT 1 FROM issues i LEFT JOIN volumes v ON v.id=i.volume_id WHERE v.id IS NULL').fetchone()
            with patch('backend.internals.provider_identity.get_db', side_effect=db.cursor):
                assert ProviderIdentityDB.audit(('comicvine', 'metron', 'gcd')) == []
        print(json.dumps(dict(start=version, end=68, normal_entrypoint=True, reopen=True, integrity='ok', foreign_keys=[],
            preserved=True, provider_identity_audit=[], task_handler=True, calendar_event=True, add_volume=True, errors=0)))


if __name__ == '__main__':
    for version in (sys.argv[1:] or ['fresh', '67', '63']):
        if version not in ('fresh', '67', '63'):
            raise SystemExit('Choose fresh, 67 or 63')
        run(version)

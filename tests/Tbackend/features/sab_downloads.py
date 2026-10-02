"""Offline selected-release/SAB acceptance and remote-side-effect recovery."""

import json
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from email.parser import BytesParser
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from flask import Flask

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, DownloadJobState as S,
                                       RemoteDownload, SABConfig)
from backend.base.release_evaluation import Compatibility, ScoringPolicy
from backend.base.release_search import ReleaseSearchRequest, SearchLimits
from backend.features.sab_downloads import (SABRuntime, create_grab_intent,
                                            download_gate, poll_downloads,
                                            submit_selected)
from backend.implementations.download_transport import (DownloadHTTP,
                                                        PrivateResponse)
from backend.implementations.newznab import (BoundedHTTP, NewznabSource,
                                             SearchBudget)
from backend.implementations.nzb_resolution import (MAX_NZB_BYTES,
                                                    SelectedSourceSession,
                                                    validate_nzb)
from backend.implementations.release_explanations import explain_release
from backend.implementations.release_scoring import evaluate_release
from backend.implementations.sabnzbd import SABClient
from backend.internals.db import DB_SCHEMA, SCHEMA_58
from backend.internals.db_migration import DatabaseMigrationHandler
from backend.internals.download_jobs import DownloadStore
from backend.internals.download_schema import SCHEMA, STATEMENTS
from backend.internals.intake_schema import SCHEMA as INTAKE_SCHEMA
from backend.internals.sab_clients import (delete_sab_client,
                                           load_sab_clients, save_sab_client)
from backend.internals.wanted_schema import SCHEMA as WANTED_SCHEMA
from tests.Tbackend.features.release_scoring import target
from tests.Tbackend.features.release_search import (CAPS, SECRET,
                                                    FixtureTransport, config,
                                                    fake_http, item, rss)

SAB_KEY = 'fake-sab-only-key'
NZB = b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><file subject="comic.cbz"><groups><group>alt.binaries.comics</group></groups><segments><segment bytes="123" number="1">message@example.invalid</segment></segments></file></nzb>'


class StaticHTTP:
    def __init__(self, body=NZB, status=200, location=''):
        self.response = PrivateResponse(status, body, location)
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@contextmanager
def fake_sab():
    """Real multipart HTTP; remote state persists independently of local SQLite."""
    state = dict(cats=['*', 'comics'], queue={}, history={}, uploads=[], calls=[],
                 auth=True, overrides={}, drop=False, outage=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_api(False)

        def do_POST(self):
            self.handle_api(True)

        def handle_api(self, post):
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            upload = None
            if post:
                body = self.rfile.read(int(self.headers['Content-Length']))
                message = BytesParser().parsebytes(('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode() + body)
                for part in message.get_payload():
                    key = part.get_param('name', header='content-disposition')
                    value = part.get_payload(decode=True)
                    if part.get_filename():
                        upload = value
                    else:
                        query[key] = value.decode()
                state['uploads'].append((upload, body, query))
            mode = query.get('mode')
            state['calls'].append((mode, query, urlsplit(self.path).path))
            if state['outage']:
                self.send_response(503)
                self.end_headers()
                return
            if mode != 'version' and (not state['auth'] or query.get('apikey') != SAB_KEY):
                result = {'error': 'API Key Incorrect'}
            elif mode == 'version':
                result = {'version': '5.1.3'}
            elif mode == 'get_cats':
                result = {'categories': state['cats']}
            elif mode in ('queue', 'history'):
                ids = query.get('nzo_ids', '').split(',')
                result = {mode: {'slots': [v for k, v in state[mode].items() if k in ids]}}
            elif mode == 'addfile':
                identifier = 'SABnzbd_nzo_' + str(len(state['uploads']))
                state['queue'][identifier] = dict(nzo_id=identifier, status='Queued', percentage='0', cat=query['cat'])
                result = dict(status=True, nzo_ids=[identifier])
                if state['drop']:
                    self.close_connection = True
                    return
            else:
                result = {'error': 'Unsupported fixture operation'}
            result = state['overrides'].get(mode, result)
            data = result if isinstance(result, bytes) else json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/sabbase', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class SABHarness:
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kapowarr-sab-')
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'app.db')
        db = sqlite3.connect(self.path)
        db.executescript(DB_SCHEMA)
        db.execute("INSERT INTO config VALUES('database_version',56)")
        db.commit()
        db.close()
        self.store = DownloadStore(self.path)
        self.addCleanup(self.store.close)
        self.target, self.policy = target(), ScoringPolicy()
        self.source = NewznabSource(config(), FixtureTransport())
        self.candidate = self.source.search(ReleaseSearchRequest('Batman')).candidates[0]
        self.evaluation = evaluate_release(self.target, self.candidate, self.policy)
        self.assertEqual(self.evaluation.state, Compatibility.COMPATIBLE)
        self.source_http = StaticHTTP()
        self.session = SelectedSourceSession(self.source, http=self.source_http)
        self.addCleanup(self.session.close)

    def client(self, url):
        return SABClient(SABConfig('sab', 'Fixture SAB', url, SAB_KEY, category='comics'))

    def intent(self, client, **kwargs):
        return create_grab_intent(self.candidate, self.evaluation, self.target, self.policy, client.config, **kwargs)

    def submit(self, client, intent=None, **kwargs):
        return submit_selected(self.store, intent or self.intent(client), self.candidate, self.evaluation,
                               self.target, self.policy, self.session, client, **kwargs)

    def poll(self, client, **kwargs):
        return poll_downloads(self.store, (client.config,), **kwargs)


class SABFlowTests(SABHarness, TestCase):
    def test_real_source_to_multipart_queue_history_completion(self):
        def source_response(path, query):
            if path == '/nzb':
                return 200, NZB, {}
            return 200, CAPS if query.get('t') == ['caps'] else rss(item(url=source_url + '/nzb?apikey=' + SECRET)), {}
        with fake_http(source_response) as (source_url, source_calls), fake_sab() as (url, remote):
            source = NewznabSource(config(url=source_url + '/api'), BoundedHTTP(SearchBudget(SearchLimits())))
            self.candidate = source.search(ReleaseSearchRequest('Batman')).candidates[0]
            self.evaluation = evaluate_release(self.target, self.candidate, self.policy)
            self.assertEqual(explain_release(self.evaluation).score, self.evaluation.score)
            self.session = SelectedSourceSession(source)
            self.addCleanup(self.session.close)
            client = self.client(url)
            result = self.submit(client)
            identifier = result['nzo_id']
            self.assertEqual(remote['uploads'][0][0], NZB)
            self.assertNotIn(SECRET.encode(), remote['uploads'][0][1])
            self.assertEqual(remote['uploads'][0][2]['priority'], '-100')
            self.assertEqual(remote['uploads'][0][2]['pp'], '-1')
            self.assertTrue(all(call[2] == '/sabbase/api' for call in remote['calls']))
            self.poll(client)
            self.assertEqual(self.store.get(result['id'])['state'], S.QUEUED.value)
            remote['queue'][identifier]['status'] = 'Downloading'
            self.poll(client)
            self.assertEqual(self.store.get(result['id'])['state'], S.DOWNLOADING.value)
            remote['queue'].clear()
            for status in ('Verifying', 'Repairing', 'Extracting', 'Moving'):
                remote['history'][identifier] = dict(nzo_id=identifier, status=status)
                self.poll(client)
                self.assertEqual(self.store.get(result['id'])['state'], S.POST_PROCESSING.value)
            remote['history'][identifier] = dict(nzo_id=identifier, status='Completed', storage='/external/only/comic', completed=1790000000)
            with patch('backend.features.post_processing.PostProcessingContext.add_file_to_database', side_effect=AssertionError):
                self.poll(client)
            receipt = self.store.preview(result['id'])
            self.assertEqual(receipt['state'], S.COMPLETED.value)
            self.assertEqual(receipt['observation']['storage'], '/external/only/comic')
            self.assertIsNotNone(receipt['completed_at'])
            self.assertEqual(len(remote['uploads']), 1)
            self.assertEqual(len(source_calls), 2)
            self.assertNotIn(SECRET, json.dumps(receipt))
            self.assertNotIn(SAB_KEY, json.dumps(receipt))

    def test_failure_no_retry(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            intent = self.intent(client)
            result = self.submit(client, intent)
            remote['queue'].clear()
            remote['history'][result['nzo_id']] = dict(nzo_id=result['nzo_id'], status='Failed', fail_message='secret ' + SAB_KEY)
            self.poll(client)
            self.assertEqual(self.submit(client, intent)['state'], S.FAILED.value)
            self.assertEqual(len(remote['uploads']), 1)
            self.assertNotIn(SAB_KEY, json.dumps(self.store.preview(intent.request_id)))

    def test_restart_tracks_nzo_id_no_resolver(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            result = self.submit(client)
            self.session.close()
            reopened = DownloadStore(self.path)
            self.addCleanup(reopened.close)
            remote['queue'].clear()
            remote['history'][result['nzo_id']] = dict(nzo_id=result['nzo_id'], status='Completed')
            poll_downloads(reopened, (client.config,))
            self.assertEqual(reopened.get(result['id'])['state'], S.COMPLETED.value)
            self.assertEqual(len(remote['uploads']), 1)

    def test_duplicate_intent_vs_explicit_repeat(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            intent = self.intent(client)
            self.submit(client, intent)
            self.submit(client, intent)
            self.assertEqual(len(remote['uploads']), 1)
            self.submit(client)  # New explicitly requested intent ID.
            self.assertEqual(len(remote['uploads']), 2)

    def test_missing_then_outage_not_completion_or_failure(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            result = self.submit(client)
            self.poll(client)
            remote['outage'] = True
            self.poll(client)
            self.assertEqual(self.store.get(result['id'])['state'], S.QUEUED.value)
            remote['outage'] = False
            remote['queue'].clear()
            self.poll(client)
            row = self.store.get(result['id'])
            self.assertEqual(row['state'], S.REMOTE_UNKNOWN.value)
            self.assertEqual(row['error'], E.MISSING.value)
            self.assertEqual(len(remote['uploads']), 1)

    def test_completed_history_survives_remote_deletion(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            result = self.submit(client)
            remote['queue'].clear()
            remote['history'][result['nzo_id']] = dict(nzo_id=result['nzo_id'], status='Completed', storage='D:\\Comics')
            self.poll(client)
            remote['history'].clear()
            self.poll(client, identifiers=(result['id'],))
            row = self.store.preview(result['id'])
            self.assertEqual(row['state'], S.COMPLETED.value)
            self.assertEqual(row['observation']['storage'], 'D:\\Comics')
            self.assertEqual(row['error'], E.MISSING.value)

    def test_category_drift_blocks_before_resolution(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            client.check()
            remote['cats'] = ['*']
            with self.assertRaises(DownloadFailure) as caught:
                self.submit(client)
            self.assertEqual(caught.exception.code, E.CATEGORY)
            self.assertFalse(self.source_http.calls)
            self.assertFalse(remote['uploads'])

    def test_full_key_auth_required_before_resolution(self):
        with fake_sab() as (url, remote):
            remote['auth'] = False
            with self.assertRaises(DownloadFailure) as caught:
                self.submit(self.client(url))
            self.assertEqual(caught.exception.code, E.AUTHENTICATION)
            self.assertFalse(self.source_http.calls)
            self.assertFalse(remote['uploads'])

    def test_endpoint_edit_disabled_or_deleted_preserves_job(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            result = self.submit(client)
            for configs in ((), (replace(client.config, enabled=False),), (replace(client.config, url=url + '/other'),)):
                count = len(remote['calls'])
                poll_downloads(self.store, configs)
                self.assertEqual(len(remote['calls']), count)
                self.assertEqual(self.store.get(result['id'])['error'], E.DRIFT.value)

    def test_display_name_change_does_not_lose_job(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            result = self.submit(client)
            remote['queue'][result['nzo_id']]['filename'] = 'renamed'
            self.poll(client)
            self.assertEqual(self.store.get(result['id'])['state'], S.QUEUED.value)


class SubmissionRecoveryTests(SABHarness, TestCase):
    def test_crash_boundaries(self):
        for boundary, uploaded, state in (('intent_persisted', 0, S.PENDING), ('resolved', 0, S.PENDING),
                ('submitting', 0, S.AMBIGUOUS), ('remote_accepted', 1, S.AMBIGUOUS), ('receipt_persisted', 1, S.SUBMITTED)):
            with self.subTest(boundary=boundary), fake_sab() as (url, remote):
                client = self.client(url)
                intent = self.intent(client)
                def crash(stage):
                    if stage == boundary:
                        raise SystemExit('injected process exit')
                with self.assertRaises(SystemExit):
                    self.submit(client, intent, checkpoint=crash)
                reopened = DownloadStore(self.path)
                with download_gate(self.path):
                    reopened.recover()
                self.assertEqual(reopened.get(intent.request_id)['state'], state.value)
                reopened.close()
                self.assertEqual(len(remote['uploads']), uploaded)
                if state != S.PENDING:
                    self.submit(client, intent)
                    self.assertEqual(len(remote['uploads']), uploaded)

    def test_receipt_db_failure_is_ambiguous(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            intent = self.intent(client)
            with patch.object(self.store, 'submitted', side_effect=sqlite3.OperationalError('injected')):
                with self.assertRaises(DownloadFailure) as caught:
                    self.submit(client, intent)
            self.assertEqual(caught.exception.code, E.AMBIGUOUS)
            self.assertEqual(self.store.get(intent.request_id)['state'], S.AMBIGUOUS.value)
            self.submit(client, intent)
            self.assertEqual(len(remote['uploads']), 1)

    def test_lost_response_is_ambiguous(self):
        with fake_sab() as (url, remote):
            remote['drop'] = True
            client = self.client(url)
            intent = self.intent(client)
            with self.assertRaises(DownloadFailure) as caught:
                self.submit(client, intent)
            self.assertEqual(caught.exception.code, E.AMBIGUOUS)
            self.assertEqual(len(remote['queue']), 1)
            self.assertEqual(self.submit(client, intent)['state'], S.AMBIGUOUS.value)
            self.assertEqual(len(remote['uploads']), 1)

    def test_invalid_or_multiple_ids_are_not_success(self):
        for value in (b'{', {}, {'status': True}, {'status': True, 'nzo_ids': []},
                {'status': True, 'nzo_ids': ['one', 'two']}, {'status': True, 'nzo_ids': [SAB_KEY]}):
            with self.subTest(value=value), fake_sab() as (url, remote):
                remote['overrides']['addfile'] = value
                client = self.client(url)
                intent = self.intent(client)
                with self.assertRaises(DownloadFailure) as caught:
                    self.submit(client, intent)
                self.assertEqual(caught.exception.code, E.AMBIGUOUS)
                self.assertEqual(self.store.get(intent.request_id)['state'], S.AMBIGUOUS.value)

    def test_explicit_api_rejection(self):
        with fake_sab() as (url, remote):
            remote['overrides']['addfile'] = dict(status=False, error='rejected ' + SAB_KEY)
            client = self.client(url)
            intent = self.intent(client)
            with self.assertRaises(DownloadFailure) as caught:
                self.submit(client, intent)
            self.assertEqual(caught.exception.code, E.REJECTED)
            self.assertEqual(self.store.get(intent.request_id)['state'], S.FAILED.value)

    def test_duplicate_claim_is_not_second_upload(self):
        with fake_sab() as (url, remote):
            client = self.client(url)
            with download_gate(self.path):
                with self.assertRaises(DownloadFailure):
                    self.submit(client)
            self.assertFalse(remote['uploads'])

    def test_reused_id_different_intent_rejected(self):
        client = self.client('http://localhost:1234')
        intent = self.intent(client)
        self.store.create(intent)
        with self.assertRaises(DownloadFailure):
            self.store.create(replace(intent, category='other'))


class SelectionTests(SABHarness, TestCase):
    def test_noncompatible_no_network(self):
        client = self.client('http://localhost:1234')
        for state in (Compatibility.REJECTED, Compatibility.REVIEW, Compatibility.UNDETERMINED):
            with self.subTest(state=state), self.assertRaises(DownloadFailure):
                create_grab_intent(self.candidate, replace(self.evaluation, state=state, score=None),
                                   self.target, self.policy, client.config)
        self.assertFalse(self.source_http.calls)

    def test_candidate_target_policy_tamper(self):
        client = self.client('http://localhost:1234')
        for candidate, evaluation, wanted, policy in (
                (replace(self.candidate, raw_title='other'), self.evaluation, self.target, self.policy),
                (self.candidate, self.evaluation, target('6'), self.policy),
                (self.candidate, replace(self.evaluation, policy_fingerprint='0' * 64), self.target, self.policy)):
            with self.assertRaises(DownloadFailure):
                create_grab_intent(candidate, evaluation, wanted, policy, client.config)

    def test_intent_mutation_fails_before_network(self):
        client = self.client('http://localhost:1234')
        with self.assertRaises(DownloadFailure):
            self.submit(client, replace(self.intent(client), candidate_id='0' * 64))
        self.assertFalse(self.source_http.calls)

    def test_cross_source_tamper_and_expiry(self):
        foreign = NewznabSource(config(key='other'), FixtureTransport())
        wrong = SelectedSourceSession(foreign, http=self.source_http)
        with self.assertRaises(DownloadFailure):
            wrong.resolve(self.candidate)
        with self.assertRaises(DownloadFailure):
            self.session.resolve(replace(self.candidate, raw_title='changed'))
        self.session.close()
        with self.assertRaises(DownloadFailure) as caught:
            self.session.resolve(self.candidate)
        self.assertEqual(caught.exception.code, E.EXPIRED)
        self.assertFalse(self.source_http.calls)

    def test_selection_window_expires_by_clock(self):
        clock = [0]
        session = SelectedSourceSession(self.source, http=self.source_http, clock=lambda: clock[0])
        clock[0] = 901
        with self.assertRaises(DownloadFailure):
            session.resolve(self.candidate)
        self.assertFalse(self.source_http.calls)

    def test_search_evaluation_explanation_do_not_submit(self):
        with patch.object(SABClient, 'submit', side_effect=AssertionError), patch.object(SelectedSourceSession, 'resolve', side_effect=AssertionError):
            explain_release(evaluate_release(self.target, self.candidate, self.policy))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM acquisition_downloads').fetchone()[0], 0)


class NZBTests(TestCase):
    def test_valid_digest_and_lossless_bytes(self):
        self.assertEqual(validate_nzb(NZB), sha256(NZB).hexdigest())

    def test_invalid_document_matrix(self):
        cases = (b'', b'<html>login</html>', b'{}', b'<nzb', b'<nzb/>', b'<!DOCTYPE nzb><nzb/>',
                 b'<!DOCTYPE nzb [<!ENTITY x SYSTEM "file:///secret">]><nzb>&x;</nzb>',
                 NZB.replace(b'bytes="123"', b'bytes="0"'), NZB.replace(b'number="1"', b'number="x"'),
                 NZB.replace(b'message@example.invalid', b''), NZB.replace(b'<group>alt.binaries.comics</group>', b''),
                 b'<nzb>' + b'<a>' * 20 + b'</a>' * 20 + b'</nzb>', b'x' * (MAX_NZB_BYTES + 1))
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(DownloadFailure):
                validate_nzb(data)

    def test_utf16_doctype_rejected(self):
        with self.assertRaises(DownloadFailure):
            validate_nzb('<!DOCTYPE nzb><nzb/>'.encode('utf-16'))

    def test_no_network_database_or_files_in_validation(self):
        with ExitStack() as stack:
            for name in ('sqlite3.connect', 'socket.socket', 'builtins.open', 'os.stat'):
                stack.enter_context(patch(name, side_effect=AssertionError))
            validate_nzb(NZB)


class DownloadMigrationTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_58.removesuffix(WANTED_SCHEMA).removesuffix(INTAKE_SCHEMA).removesuffix(SCHEMA))
        self.db.execute("INSERT INTO config VALUES('database_version',55)")
        self.db.execute("INSERT INTO download_history(downloaded_at,file_title) VALUES(1,'legacy')")
        self.db.commit()

    def migrate(self):
        with patch('backend.internals.db_migration.get_db', side_effect=self.db.cursor):
            DatabaseMigrationHandler.handlers[55]()

    def test_upgrade_fresh_parity_preservation(self):
        tables = ('download_history', 'organization_jobs', 'monitor_roots', 'volumes', 'issues', 'files', 'volume_external_ids')
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables}
        self.migrate()
        self.migrate()
        self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables})
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 56)
        fresh = sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        fresh.executescript(SCHEMA_58.removesuffix(WANTED_SCHEMA).removesuffix(INTAKE_SCHEMA))
        query = "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'acquisition_%' ORDER BY type,name"
        self.assertEqual(self.db.execute(query).fetchall(), fresh.execute(query).fetchall())
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_failed_migration_rolls_back(self):
        with patch('backend.internals.download_schema.STATEMENTS', (*STATEMENTS[:1], 'INVALID SQL')):
            with self.assertRaises(sqlite3.Error):
                self.migrate()
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0], 55)
        self.assertFalse(self.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'acquisition_%'").fetchall())


class ResolutionSecurityTests(SABHarness, TestCase):
    def test_prowlarr_exact_proxy_then_explicitly_trusted_redirect(self):
        with fake_http(lambda p, q: (200, NZB, {})) as (upstream, upstream_calls):
            def proxy(path, query):
                if path == '/base/7/download':
                    return 301, b'', {'Location': upstream + '/get?apikey=upstream-private-key'}
                return 200, rss(item(url=prowlarr + '/base/7/download?link=opaque&file=comic')), {}
            with fake_http(proxy) as (prowlarr, calls):
                source = NewznabSource(config(url=prowlarr + '/base', mode='prowlarr'),
                                       BoundedHTTP(SearchBudget(SearchLimits())), indexer_id=7, name='Indexer X')
                candidate = source.search(ReleaseSearchRequest('Batman')).candidates[0]
                session = SelectedSourceSession(source, redirect_origins=(upstream,))
                self.addCleanup(session.close)
                resolved = session.resolve(candidate)
                self.assertEqual(resolved.data, NZB)
                self.assertEqual(calls[-1][2], SECRET)
                self.assertIsNone(upstream_calls[0][2])
                self.assertEqual(candidate.source.name, 'Indexer X')
                self.assertEqual(candidate.source.via, 'Fixture')
                with fake_sab() as (url, remote):
                    client = self.client(url)
                    evaluation = evaluate_release(self.target, candidate, self.policy)
                    intent = create_grab_intent(candidate, evaluation, self.target, self.policy, client.config)
                    result = submit_selected(self.store, intent, candidate, evaluation,
                                             self.target, self.policy, session, client)
                    self.assertEqual(result['state'], S.SUBMITTED.value)
                    self.assertNotIn(b'upstream-private-key', remote['uploads'][0][1])

    def test_untrusted_redirect_never_contacted(self):
        for location in ('http://other.invalid/nzb', 'file:///secret', 'http://user:pass@fixture.test/nzb',
                         'http://fixture.test/nzb#fragment', 'http://fixture.test:bad/nzb', 'http://[invalid'):
            with self.subTest(location=location):
                transport = StaticHTTP(status=301, location=location)
                session = SelectedSourceSession(self.source, http=transport)
                with self.assertRaises(DownloadFailure):
                    session.resolve(self.candidate)
                self.assertEqual(len(transport.calls), 1)

    def test_redirect_loop_bounded(self):
        transport = StaticHTTP(status=301, location='/nzb')
        session = SelectedSourceSession(self.source, http=transport)
        with self.assertRaises(DownloadFailure) as caught:
            session.resolve(self.candidate)
        self.assertEqual(caught.exception.code, E.REDIRECT)
        self.assertEqual(len(transport.calls), 4)

    def test_cross_origin_key_echo_redirect_refused(self):
        transport = StaticHTTP(status=301, location='http://trusted.invalid/nzb?apikey=' + SECRET)
        session = SelectedSourceSession(self.source, http=transport, redirect_origins=('http://trusted.invalid',))
        with self.assertRaises(DownloadFailure):
            session.resolve(self.candidate)
        self.assertEqual(len(transport.calls), 1)

    def test_bad_source_document_never_uploaded(self):
        with fake_sab() as (url, remote):
            self.source_http.response = PrivateResponse(200, b'<html>login</html>')
            with self.assertRaises(DownloadFailure) as caught:
                self.submit(self.client(url))
            self.assertEqual(caught.exception.code, E.INVALID_NZB)
            self.assertFalse(remote['uploads'])

    def test_known_secret_in_nzb_refused(self):
        self.source_http.response = PrivateResponse(200, NZB.replace(b'comic.cbz', SECRET.encode()))
        with self.assertRaises(DownloadFailure):
            self.session.resolve(self.candidate)

    def test_source_transport_failure_is_not_sab_failure(self):
        with fake_sab() as (url, remote), patch.object(self.source_http, 'request', side_effect=DownloadFailure(E.TIMEOUT)):
            client = self.client(url)
            intent = self.intent(client)
            with self.assertRaises(DownloadFailure) as caught:
                self.submit(client, intent)
            self.assertEqual(caught.exception.code, E.RESOLUTION)
            self.assertEqual(self.store.get(intent.request_id)['state'], S.PENDING.value)
            self.assertFalse(remote['uploads'])


class SABProtocolTests(TestCase):
    def test_real_bounded_body_and_status_failures(self):
        for status, body, headers, expected in (
                (401, b'', {}, E.AUTHENTICATION), (403, b'', {}, E.AUTHENTICATION),
                (429, b'', {}, E.UNAVAILABLE), (500, b'', {}, E.UNAVAILABLE),
                (200, b'x' * 101, {}, E.LIMIT),
                (200, b'x' * 101, {'Connection': 'close'}, E.LIMIT),
                (200, b'abc', {'Content-Encoding': 'gzip'}, E.INVALID_RESPONSE)):
            with self.subTest(status=status, expected=expected), fake_http(lambda p, q: (status, body, headers)) as (url, calls):
                with self.assertRaises(DownloadFailure) as caught:
                    DownloadHTTP().request(url, maximum=100)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(len(calls), 1)

    def test_sab_redirect_is_never_followed(self):
        with fake_http(lambda p, q: (302, b'', {'Location': 'http://not-requested.invalid'})) as (url, calls):
            with self.assertRaises(DownloadFailure):
                SABClient(SABConfig('x', 'X', url, SAB_KEY)).check()
            self.assertEqual(len(calls), 1)

    def test_timeout_is_typed(self):
        with patch('backend.implementations.download_transport.HTTPConnection.connect', side_effect=TimeoutError):
            with self.assertRaises(DownloadFailure) as caught:
                DownloadHTTP().request('http://localhost:1')
            self.assertEqual(caught.exception.code, E.TIMEOUT)

    def test_connection_refusal_typed_no_exception_echo(self):
        with patch('backend.implementations.download_transport.HTTPConnection.connect', side_effect=ConnectionRefusedError(SAB_KEY)):
            with self.assertRaises(DownloadFailure) as caught:
                DownloadHTTP().request('http://localhost:1')
            self.assertEqual(str(caught.exception), E.UNAVAILABLE.value)

    def test_invalid_json_and_wrong_job_ids_fail(self):
        for payload in (b'{', b'[' * 10000, {'queue': {'slots': [{'nzo_id': 'other', 'status': 'Completed'}]}},
                        {'queue': {'slots': 'bad'}}):
            with fake_sab() as (url, remote):
                remote['overrides']['queue'] = payload
                with self.assertRaises(DownloadFailure):
                    SABClient(SABConfig('x', 'X', url, SAB_KEY)).observe(('wanted',))

    def test_status_and_numeric_progress_matrix(self):
        with fake_sab() as (url, remote):
            client = SABClient(SABConfig('x', 'X', url, SAB_KEY))
            for status in ('Queued', 'Paused', 'Propagating', 'Downloading', 'Fetching', 'Checking', 'Grabbing', 'FutureState'):
                for progress in ('32.5', 'nan', 'inf', 'invalid', '-1', '101', None):
                    remote['queue']['one'] = dict(nzo_id='one', status=status, percentage=progress)
                    result = client.observe(('one',))['one']
                    self.assertEqual(result.progress, 32.5 if progress == '32.5' else None)
                    self.assertNotIn(result.state, (S.COMPLETED, S.FAILED))

    def test_all_history_states_and_path_sanitizing(self):
        with fake_sab() as (url, remote):
            client = SABClient(SABConfig('x', 'X', url, SAB_KEY))
            for status in ('Queued', 'QuickCheck', 'Verifying', 'Repairing', 'Fetching', 'Extracting', 'Moving', 'Running', 'Completed', 'Failed'):
                remote['history']['one'] = dict(nzo_id='one', status=status, storage='https://host/?apikey=' + SAB_KEY)
                result = client.observe(('one',))['one']
                expected = S.COMPLETED if status == 'Completed' else S.FAILED if status == 'Failed' else S.POST_PROCESSING
                self.assertEqual(result.state, expected)
                self.assertIsNone(result.storage)

    def test_priority_category_and_url_validation(self):
        for priority in (-100, -2, -1, 0, 1, 2):
            SABConfig('x', 'X', 'http://[::1]:8080/base', SAB_KEY, priority=priority)
        for args in ({'priority': 500}, {'priority': True}, {'url': 'file:///x'},
                     {'url': 'http://user:pass@host'}, {'url': 'http://host?apikey=x'},
                     {'category': ''}, {'api_key': ''}, {'enabled': 1}):
            with self.assertRaises(DownloadFailure):
                SABConfig(**dict(dict(key='x', name='X', url='http://host', api_key=SAB_KEY), **args))
        for field in ('name', 'category', 'url'):
            with self.assertRaises(DownloadFailure):
                SABConfig(**dict(dict(key='x', name='X', url='http://host', api_key=SAB_KEY),
                                 **{field: 'http://host/' + SAB_KEY}))


class PollingTests(SABHarness, TestCase):
    def test_batched_polling_1_100_1000(self):
        for count in (1, 100, 1000):
            client = self.client('http://fixture.test')
            calls = []
            for _ in range(count):
                intent = self.intent(client)
                self.store.create(intent)
                self.store.begin_submission(intent.request_id, self.session.resolve(self.candidate))
                self.store.submitted(intent.request_id, 'remote_' + intent.request_id)
            class FakeClient:
                def __init__(self, config):
                    pass
                def observe(self, ids):
                    calls.append(ids)
                    return {i: RemoteDownload(i, S.COMPLETED, 'Completed') for i in ids}
            poll_downloads(self.store, (client.config,), client_factory=FakeClient)
            self.assertEqual(len(calls), (count + 99) // 100)
            self.assertEqual(sum(map(len, calls)), count)
            print(f'SAB polling {count}: {len(calls)} bounded client batches; no submission')

    def test_no_rescoring_search_import_or_provider_during_submit_poll(self):
        with fake_sab() as (url, remote), ExitStack() as stack:
            client = self.client(url)
            for name in ('backend.implementations.release_scoring.evaluate_release',
                         'backend.implementations.release_scoring.evaluate_releases',
                         'backend.implementations.release_explanations.explain_release',
                         'backend.implementations.release_search.configured_search',
                         'backend.features.download_queue.DownloadHandler.add',
                         'backend.features.post_processing.PostProcessor.success'):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            self.submit(client)
            self.poll(client)
            self.assertEqual(len(remote['uploads']), 1)

    def test_unconfigured_worker_lifecycle_no_network(self):
        runtime = SABRuntime(self.path)
        with patch('socket.socket', side_effect=AssertionError):
            runtime.tick()
            runtime.start()
            runtime.stop()
        self.assertFalse(runtime.thread.is_alive())

    def test_poll_cancellation_does_not_touch_remote(self):
        client = self.client('http://localhost:1234')
        intent = self.intent(client)
        self.store.create(intent)
        self.store.begin_submission(intent.request_id, self.session.resolve(self.candidate))
        self.store.submitted(intent.request_id, 'one')
        with patch('socket.socket', side_effect=AssertionError):
            poll_downloads(self.store, (client.config,), cancelled=lambda: True)


class SABConfigurationTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE config(key TEXT PRIMARY KEY,value)')
        self.patch = patch('backend.internals.sab_clients.get_db', side_effect=self.db.cursor)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_secret_mask_edit_and_delete(self):
        data = dict(name='<script>client</script>', url='http://fixture.test/base', api_key=SAB_KEY)
        saved = save_sab_client(data)
        self.assertNotIn(SAB_KEY, json.dumps(saved.preview()))
        edited = save_sab_client(dict(data, api_key=''), saved.key)
        self.assertEqual(edited.api_key, SAB_KEY)
        self.assertEqual(load_sab_clients()[0], edited)
        delete_sab_client(saved.key)
        self.assertFalse(load_sab_clients())

    def test_secret_rotation_does_not_change_instance(self):
        a = SABConfig('x', 'X', 'http://fixture.test/base', SAB_KEY)
        self.assertEqual(a.instance, replace(a, api_key='new-private-key').instance)
        self.assertNotEqual(a.instance, replace(a, url=a.url + '/other').instance)

    def test_bad_configuration_never_writes_or_connects(self):
        for data in ({}, {'api_key': None}, {'arbitrary_url': 'http://x'}, []):
            with patch('socket.socket', side_effect=AssertionError), self.assertRaises(DownloadFailure):
                save_sab_client(data)
        self.assertFalse(load_sab_clients())

    def test_authenticated_config_api_no_submission_or_secret_echo(self):
        from frontend.api import api
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        client = app.test_client()
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('socket.socket', side_effect=AssertionError('network')):
            settings.return_value.sv.api_key = 'test-application-key'
            self.assertEqual(client.get('/api/sab-clients').status_code, 401)
            suffix = '?api_key=test-application-key'
            response = client.post('/api/sab-clients' + suffix, json=dict(
                name='<script>client</script>', url='http://fixture.test/base', api_key=SAB_KEY))
            self.assertEqual(response.status_code, 201)
            key = response.json['result']['id']
            self.assertNotIn(SAB_KEY, response.get_data(as_text=True))
            response = client.get('/api/sab-clients' + suffix)
            self.assertEqual(len(response.json['result']), 1)
            self.assertNotIn(SAB_KEY, response.get_data(as_text=True))
            response = client.put('/api/sab-clients/' + key + suffix, json=dict(
                name='Changed', url='http://fixture.test/base', api_key=''))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(load_sab_clients()[0].api_key, SAB_KEY)
            bad = client.post('/api/sab-clients' + suffix, json=dict(
                name='Bad', url='http://host/?apikey=' + SAB_KEY, api_key=SAB_KEY))
            self.assertEqual(bad.status_code, 400)
            self.assertNotIn(SAB_KEY, bad.get_data(as_text=True))
            self.assertEqual(client.delete('/api/sab-clients/' + key + suffix).status_code, 200)
            self.assertFalse(load_sab_clients())

    def test_api_connection_test_configured_only_post(self):
        from frontend.api import api
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        client = app.test_client()
        config = save_sab_client(dict(name='Fixture', url='http://fixture.test/base', api_key=SAB_KEY))
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.implementations.sabnzbd.SABClient') as sab:
            settings.return_value.sv.api_key = 'test-application-key'
            sab.return_value.check.return_value = dict(version='5.1.3', categories=['comics'])
            path = '/api/sab-clients/' + config.key + '/test?api_key=test-application-key'
            self.assertEqual(client.get(path).status_code, 405)
            self.assertEqual(client.post(path, json={'url': 'http://untrusted.test'}).status_code, 200)
            sab.assert_called_once_with(config)
            sab.return_value.submit.assert_not_called()


class SABStatusAPITests(SABHarness, TestCase):
    def test_status_reads_persisted_receipts_without_remote_requests(self):
        from frontend.api import api
        client = self.client('http://fixture.test')
        intent = self.intent(client)
        self.store.create(intent)
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.internals.db.DBConnection.default_file', self.path), \
                patch('socket.socket', side_effect=AssertionError('network')):
            settings.return_value.sv.api_key = 'test-application-key'
            api_client = app.test_client()
            self.assertEqual(api_client.get('/api/sab-downloads').status_code, 401)
            path = '/api/sab-downloads?api_key=test-application-key'
            response = api_client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['result'][0]['state'], S.PENDING.value)
            for secret in (SECRET, SAB_KEY, intent.resolver_key, 'resolver_key'):
                self.assertNotIn(secret, response.get_data(as_text=True))
            self.assertEqual(api_client.post(path, json={}).status_code, 405)

    def test_contradictory_submission_response_is_ambiguous(self):
        from backend.base.download_job import ResolvedNZB
        http = StaticHTTP(json.dumps(dict(
            status=False, error='Rejected', nzo_ids=['possibly_accepted'])).encode())
        client = SABClient(SABConfig('x', 'X', 'http://fixture.test', SAB_KEY), http)
        with self.assertRaises(DownloadFailure) as error:
            client.submit(ResolvedNZB('c', 's', NZB, validate_nzb(NZB), 'release.nzb'), 'Comic')
        self.assertEqual(error.exception.code, E.AMBIGUOUS)

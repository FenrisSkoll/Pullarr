"""Production HTTP transport against a stateful loopback-only GCD fixture."""

import asyncio
import json
import sqlite3
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest import TestCase
from unittest.mock import patch

from requests import Session

from backend.base.issue_facts import DatePrecision
from backend.implementations.metadata.gcd import GcdMetadataProvider
from backend.implementations.metadata.gcd_budget import GcdBudget
from backend.implementations.metadata.gcd_client import GcdClient, GcdError

_REQUEST = Session.request


class FakeGcd:
    def session(self):
        owner = self

        class LoopbackSession(Session):
            def request(self, method, url, **kwargs):
                if not url.startswith(owner.base):
                    raise AssertionError('Fixture permits only its own loopback origin')
                return _REQUEST(self, method, url, **kwargs)

        return LoopbackSession()

    def __init__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get('Authorization')))
                code, payload, content_type, headers = owner.respond(self.path)
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port) + '/api/'
        self.requests = []
        self.series = {'api_url': self.base + 'series/1/', 'name': 'Example',
                       'year_began': 2021, 'publisher': self.base + 'publisher/1/',
                       'language': 'en', 'active_issues': [self.base + 'issue/1/']}
        self.issues = {'1': {'api_url': self.base + 'issue/1/', 'series': self.base + 'series/1/',
            'number': '1', 'title': '', 'key_date': '2021-12-00', 'on_sale_date': '2021-00-00',
            'publication_date': 'Winter 2021', 'variant_of': None, 'story_set': []}}
        self.override = None
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def respond(self, path):
        if self.override:
            result = self.override(path)
            if result is not None:
                return result
        if path.startswith('/api/series/name/'):
            return 200, {'count': 1, 'next': None, 'results': [self.series]}, 'application/json', {}
        if path == '/api/series/1/':
            return 200, self.series, 'application/json', {}
        if path == '/api/publisher/1/':
            return 200, {'api_url': self.base + 'publisher/1/', 'name': 'Publisher'}, 'application/json', {}
        if path.startswith('/api/issue/'):
            row = self.issues.get(path.split('/')[-2])
            return (200 if row else 404), row, 'application/json', {}
        return 404, {}, 'application/json', {}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class GcdClientTests(TestCase):
    def test_nonfinite_budget_fails_closed(self):
        self.db.execute('INSERT INTO config VALUES(?,?)',
                        (GcdBudget.KEY, '{"attempts":[NaN],"until":0}'))
        self.db.commit()
        with self.assertRaises(GcdError) as error:
            self.budget.charge()
        self.assertEqual(error.exception.reason, 'budget_invalid')
        self.assertEqual(self.fake.requests, [])

    def test_timeout_charged_without_retry(self):
        from requests import Timeout
        client = self.client()
        self.addCleanup(client.close)
        with patch.object(client.session, 'get', side_effect=Timeout('private payload')):
            with self.assertRaises(GcdError) as error:
                client.get('series/1/')
        self.assertEqual(error.exception.reason, 'timeout')
        self.assertNotIn('private payload', str(error.exception.api_response))
        state = json.loads(self.db.execute('SELECT value FROM config').fetchone()[0])
        self.assertEqual(len(state['attempts']), 1)

    def test_search_pages_and_cap(self):
        from urllib.parse import parse_qs, urlsplit

        capped = False
        def override(path):
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            row = deepcopy(self.fake.series)
            row['api_url'] = self.fake.base + f'series/{page}/'
            next_url = self.fake.base + f'series/name/Example/?page={page + 1}'
            return 200, {'count': 6 if capped else 2, 'results': [row],
                         'next': next_url if capped or page == 1 else None}, 'application/json', {}
        self.fake.override = override
        with patch('backend.implementations.metadata.gcd.get_db', self.db.cursor):
            results = asyncio.run(self.provider().search_volumes('Example'))
            self.assertEqual([r.provider_id for r in results], ['1', '2'])
            capped = True
            before = len(self.fake.requests)
            with self.assertRaises(GcdError) as error:
                asyncio.run(self.provider().search_volumes('Example'))
            self.assertEqual(error.exception.reason, 'search_limit')
            self.assertEqual(len(self.fake.requests) - before, 5)

    def setUp(self):
        self.fake = FakeGcd()
        self.addCleanup(self.fake.close)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE volume_external_ids(provider TEXT,provider_id TEXT,volume_id INT)')
        self.now = 100000.0
        self.budget = GcdBudget(False, clock=lambda: self.now, cursor=self.db.cursor)

    def client(self, auth=False):
        return GcdClient(base=self.fake.base, username='fixture' if auth else '',
                         password='fixture-only' if auth else '', charge=self.budget.charge,
                         preflight=self.budget.preflight, limited=self.budget.limited)

    def provider(self):
        return GcdMetadataProvider(self.client, clock=lambda: self.now)

    def test_rich_snapshot_exact_requests_and_no_fake_day(self):
        snapshot = asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual(len(self.fake.requests), 4)  # N + 3, publisher explicit.
        self.assertEqual(snapshot.issues[0].legacy_number, 1.0)
        self.assertEqual(snapshot.issues[0].facts.operational_date.precision, DatePrecision.MONTH)
        self.assertIsNone(snapshot.issues[0].facts.operational_date.exact_day)
        self.assertEqual(len(snapshot.issues[0].facts.dates), 3)
        self.assertEqual(snapshot.volume.publisher, 'Publisher')

    def test_rich_number_and_variant_representation(self):
        for index, raw in enumerate(('1', '01', '1.0', '1.5', '1A', '[nn]', 'Annual', 'Special'), 1):
            identity = str(index)
            row = deepcopy(self.fake.issues['1'])
            row.update(api_url=self.fake.base + 'issue/' + identity + '/', number=raw)
            if index == 5:
                row['variant_of'] = self.fake.base + 'issue/1/'
            self.fake.issues[identity] = row
        self.fake.series['active_issues'] = [r['api_url'] for r in self.fake.issues.values()]
        snapshot = asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual([i.facts.number.raw_label for i in snapshot.issues],
                         ['1', '01', '1.0', '1.5', '1A', '[nn]', 'Annual', 'Special'])
        self.assertTrue(all(i.legacy_number is None for i in snapshot.issues[4:]))
        self.assertEqual(snapshot.issues[4].variant_of.provider_id, '1')

    def test_search_literal_year_and_exact_direct_id(self):
        with patch('backend.implementations.metadata.gcd.get_db', self.db.cursor):
            result = asyncio.run(self.provider().search_volumes('Étrange & Comic', 2021))
            self.assertEqual(result[0].provider_id, '1')
            self.assertIn('%C3%89trange%20%26%20Comic/year/2021/', self.fake.requests[0][0])
            asyncio.run(self.provider().search_volumes('gcd:1'))
            self.assertEqual(self.fake.requests[-1][0], '/api/series/1/')

    def test_budget_preflight_stops_before_issue_or_publisher_requests(self):
        self.fake.series['active_issues'] = [self.fake.base + f'issue/{i}/' for i in range(1, 101)]
        with self.assertRaises(GcdError) as error:
            asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual(error.exception.reason, 'snapshot_budget_unsupported')
        self.assertEqual(len(self.fake.requests), 1)

    def test_membership_race_rejects_snapshot(self):
        def override(path):
            if path.startswith('/api/issue/'):
                self.fake.series['active_issues'] = []
        self.fake.override = override
        with self.assertRaises(GcdError) as error:
            asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual(error.exception.reason, 'coherence')

    def test_duplicate_wrong_parent_and_missing_variant_base(self):
        self.fake.series['active_issues'] *= 2
        with self.assertRaises(GcdError):
            asyncio.run(self.provider().fetch_snapshot('1'))
        self.fake.series['active_issues'].pop()
        self.fake.issues['1']['series'] = self.fake.base + 'series/2/'
        with self.assertRaises(GcdError):
            asyncio.run(self.provider().fetch_snapshot('1'))
        self.fake.issues['1']['series'] = self.fake.base + 'series/1/'
        self.fake.issues['1']['variant_of'] = self.fake.base + 'issue/2/'
        with self.assertRaises(GcdError):
            asyncio.run(self.provider().fetch_snapshot('1'))

    def test_missing_detail_does_not_fetch_final_membership(self):
        self.fake.issues.clear()
        with self.assertRaises(GcdError) as error:
            asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual(error.exception.reason, 'not_found')
        self.assertEqual(len(self.fake.requests), 3)

    def test_http_errors_are_typed_and_charged(self):
        for code, reason in ((401, 'credentials'), (403, 'forbidden'), (404, 'not_found'), (500, 'unavailable')):
            self.fake.override = lambda path: (code, {'secret': 'not-public'}, 'application/json', {})
            with self.assertRaises(GcdError) as error:
                asyncio.run(self.provider().fetch_snapshot('1'))
            self.assertEqual(error.exception.reason, reason)
            self.assertNotIn('not-public', str(error.exception))
        ledger = json.loads(self.db.execute('SELECT value FROM config').fetchone()[0])
        self.assertEqual(len(ledger['attempts']), 4)

    def test_rate_limit_survives_new_budget_instance(self):
        self.fake.override = lambda path: (429, {}, 'application/json', {'Retry-After': '60'})
        with self.assertRaises(GcdError):
            asyncio.run(self.provider().fetch_snapshot('1'))
        budget = GcdBudget(True, clock=lambda: self.now, cursor=self.db.cursor)
        with self.assertRaises(GcdError) as error:
            budget.charge()
        self.assertEqual(error.exception.reason, 'rate_limited')
        self.assertEqual(len(self.fake.requests), 1)

    def test_redirect_never_forwards_basic_credentials(self):
        self.fake.override = lambda path: (302, {}, 'application/json', {'Location': 'https://invalid.example/'})
        client = self.client(True)
        self.addCleanup(client.close)
        with self.assertRaises(GcdError):
            client.get('series/1/')
        self.assertEqual(len(self.fake.requests), 1)
        self.assertTrue(self.fake.requests[0][1].startswith('Basic '))

    def test_resource_links_cannot_become_arbitrary_requests(self):
        client = self.client()
        self.addCleanup(client.close)
        for url in ('https://evil.invalid/api/issue/1/', self.fake.base + 'publisher/1/',
                    self.fake.base + 'issue/1/?x=1', self.fake.base + 'issue/../1/'):
            with self.assertRaises(GcdError):
                client.identity(url, 'issue')
        self.assertFalse(self.fake.requests)

    def test_invalid_json_response_limit_and_transaction_boundary(self):
        self.fake.override = lambda path: (200, b'not JSON', 'application/json', {})
        with self.assertRaises(GcdError) as error:
            asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual(error.exception.reason, 'invalid_json')
        self.fake.override = lambda path: (200, {'large': 'x' * 100}, 'application/json', {})
        with patch('backend.implementations.metadata.gcd_client.MAX_BODY', 10):
            with self.assertRaises(GcdError) as error:
                asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual(error.exception.reason, 'response_limit')
        self.db.execute('BEGIN')
        with self.assertRaises(GcdError) as error:
            self.budget.charge()
        self.assertEqual(error.exception.reason, 'transaction_boundary')
        self.db.rollback()

    def test_zero_issue_snapshot_is_explicitly_empty(self):
        self.fake.series['active_issues'] = []
        snapshot = asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual(snapshot.issues, ())
        self.assertEqual(len(self.fake.requests), 3)

    def test_oversized_series_refused_without_details(self):
        self.budget.authenticated = True
        self.fake.series['active_issues'] = [self.fake.base + f'issue/{i}/' for i in range(1, 2002)]
        with self.assertRaises(GcdError) as error:
            asyncio.run(self.provider().fetch_snapshot('1'))
        self.assertEqual(error.exception.reason, 'snapshot_budget_unsupported')
        self.assertEqual(len(self.fake.requests), 1)

    def test_search_does_not_imply_add_capability_for_large_series(self):
        self.fake.series['active_issues'] = [self.fake.base + f'issue/{i}/' for i in range(1, 2002)]
        with patch('backend.implementations.metadata.gcd.get_db', self.db.cursor):
            rows = asyncio.run(self.provider().search_volumes('Example'))
        self.assertEqual(rows[0].issue_count, 2001)
        self.assertEqual(len(self.fake.requests), 1)

    def test_search_repeated_identity_and_external_next_fail(self):
        def response(path):
            return 200, dict(count=2, results=[self.fake.series, self.fake.series], next=None), 'application/json', {}
        self.fake.override = response
        with self.assertRaises(GcdError) as error:
            asyncio.run(self.provider().search_volumes('Example'))
        self.assertEqual(error.exception.reason, 'pagination')
        self.fake.override = lambda path: (200, dict(count=2, results=[self.fake.series],
            next='https://evil.invalid/api/series/?page=2'), 'application/json', {})
        with self.assertRaises(GcdError):
            asyncio.run(self.provider().search_volumes('Example'))
        self.assertEqual(len(self.fake.requests), 2)

    def test_authentication_mode_does_not_reset_anonymous_history(self):
        for _ in range(30):
            self.budget.charge()
        with self.assertRaises(GcdError):
            self.budget.charge()
        authenticated = GcdBudget(True, clock=lambda: self.now, cursor=self.db.cursor)
        authenticated.charge()
        with self.assertRaises(GcdError):
            self.budget.charge()
        self.now += 3601
        self.budget.charge()
        self.assertEqual(len(json.loads(self.db.execute('SELECT value FROM config').fetchone()[0])['attempts']), 32)

    def test_background_daily_ceiling_preserves_interactive_capacity(self):
        background = GcdBudget(True, background=True, clock=lambda: self.now, cursor=self.db.cursor)
        for _ in range(250):
            background.charge()
        with self.assertRaises(GcdError):
            GcdBudget(True, background=True, clock=lambda: self.now, cursor=self.db.cursor).charge()
        GcdBudget(True, clock=lambda: self.now, cursor=self.db.cursor).charge()
        self.now += 86401
        background.charge()

    def test_daily_budget_counts_every_attempt_across_instances(self):
        self.db.execute('INSERT INTO config VALUES(?,?)', (GcdBudget.KEY,
            json.dumps(dict(attempts=[self.now] * 1999, until=0))))
        self.db.commit()
        authenticated = GcdBudget(True, clock=lambda: self.now, cursor=self.db.cursor)
        with self.assertRaises(GcdError):
            authenticated.preflight(2)
        authenticated.charge()
        with self.assertRaises(GcdError):
            GcdBudget(True, clock=lambda: self.now, cursor=self.db.cursor).charge()

    def test_duplicate_json_and_wrong_content_type_fail_closed(self):
        for body, kind in ((b'{"api_url":1,"api_url":2}', 'application/json'),
                           (b'{}', 'text/html')):
            self.fake.override = lambda path: (200, body, kind, {})
            with self.assertRaises(GcdError) as error:
                asyncio.run(self.provider().fetch_snapshot('1'))
            self.assertEqual(error.exception.reason, 'invalid_json')

    def test_snapshot_normalization_request_count_diagnostics(self):
        from time import perf_counter
        self.budget.authenticated = True
        for count in (10, 100, 1000):
            self.now += 86401
            template = deepcopy(self.fake.issues['1'])
            self.fake.issues = {}
            for i in range(1, count + 1):
                row = dict(template, api_url=self.fake.base + f'issue/{i}/', number=str(i))
                self.fake.issues[str(i)] = row
            self.fake.series['active_issues'] = [r['api_url'] for r in self.fake.issues.values()]
            before = len(self.fake.requests)
            start = perf_counter()
            snapshot = asyncio.run(self.provider().fetch_snapshot('1'))
            elapsed = perf_counter() - start
            self.assertEqual(len(snapshot.issues), count)
            self.assertEqual(len(self.fake.requests) - before, count + 3)
            print(f'GCD loopback snapshot {count}: {count + 3} HTTP, {elapsed:.3f}s (includes transport)')

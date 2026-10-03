"""Synthetic legacy and upstream release-5.2.4 WebAPI contracts; no torrents fetched."""
import json
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from fixtures.expanded_clients import HASH, services
from Tbackend.features.release_search import fake_http
from TExpandedClients import config

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, DownloadJobState as S)
from backend.base.torrent import ResolvedTorrent, TorrentIdentity
from backend.implementations.download_transport import (DownloadHTTP,
                                                        HTTPStatusFailure,
                                                        PrivateResponse)
from backend.implementations.qbittorrent import QBittorrentClient


class StatusContracts(TestCase):
    def test_statuses_are_opt_in_and_body_is_preserved(self):
        for status, body in ((200, b'Ok.'), (202, b'{}'), (204, b'')):
            with self.subTest(status=status), fake_http(lambda p, q: (status, body, {})) as (url, calls):
                if status != 200:
                    with self.assertRaises(DownloadFailure) as error:
                        DownloadHTTP().request(url)
                    self.assertEqual(error.exception.code, E.UNAVAILABLE)
                response = DownloadHTTP().request(url, success_statuses=(200, 202, 204))
                self.assertEqual((response.status, response.body), (status, body))

    def test_auth_errors_redirects_and_unexpected_statuses(self):
        for status in (401, 403, 404, 409, 429, 500):
            with self.subTest(status=status), fake_http(lambda p, q: (status, b'private', {})) as (url, calls):
                with self.assertRaises(DownloadFailure) as error:
                    DownloadHTTP().request(url, success_statuses=(200, 204))
                self.assertEqual(error.exception.code, E.AUTHENTICATION if status in (401, 403) else E.UNAVAILABLE)
                self.assertNotIn('private', str(error.exception))
                self.assertEqual(len(calls), 1)
        with fake_http(lambda p, q: (302, b'', {'Location': 'http://never-follow.invalid'})) as (url, calls):
            self.assertEqual(DownloadHTTP().request(url, success_statuses=(200, 204)).status, 302)
            self.assertEqual(len(calls), 1)

    def test_opt_in_retains_encoding_and_size_checks(self):
        for status, body, headers, code in (
                (202, b'x' * 101, {}, E.LIMIT),
                (204, b'', {'Content-Length': '101'}, E.LIMIT),
                (204, b'', {'Content-Length': '1'}, E.INVALID_RESPONSE),
                (202, b'abc', {'Content-Encoding': 'gzip'}, E.INVALID_RESPONSE),
                (204, b'', {'Content-Encoding': 'gzip'}, E.INVALID_RESPONSE)):
            with self.subTest(status=status, code=code), fake_http(lambda p, q: (status, body, headers)) as (url, calls):
                with self.assertRaises(DownloadFailure) as error:
                    DownloadHTTP().request(url, maximum=100, success_statuses=(200, 202, 204))
                self.assertEqual(error.exception.code, code)

    def test_invalid_success_contract_rejected_before_network(self):
        for statuses in ((), (401,), (302,), (500,), (True,)):
            with self.subTest(statuses=statuses), self.assertRaises(DownloadFailure) as error:
                DownloadHTTP().request('http://unused.invalid', success_statuses=statuses)
            self.assertEqual(error.exception.code, E.CONFIGURATION)


class ProfileHTTP:
    """Bounded scripted responses; transport contracts are tested separately."""
    def __init__(self):
        self.login = PrivateResponse(204, b'', cookie='QBT_SID_8080=synthetic-session-id; HttpOnly; Path=/; SameSite=Strict')
        self.calls = []
        self.overrides = {}
        self.empty_reads = 0
        self.receipt = PrivateResponse(200, json.dumps(dict(success_count=1, failure_count=0,
            pending_count=0, added_torrent_ids=[HASH])).encode())

    def request(self, url, **kwargs):
        path = url.split('/api/v2/')[1].split('?')[0]
        self.calls.append((path, kwargs))
        if path in self.overrides:
            result = self.overrides[path]
            if isinstance(result, list): result = result.pop(0)
            if isinstance(result, Exception): raise result
            return result
        if path == 'auth/login': return self.login
        if path == 'torrents/add': return self.receipt
        if path == 'torrents/info' and self.empty_reads:
            self.empty_reads -= 1
            return PrivateResponse(200, b'[]')
        values = {'app/version': b'v5.2.4', 'app/webapiVersion': b'2.15.1',
            'torrents/categories': b'{"pullarr":{}}',
            'torrents/info': json.dumps([dict(hash=HASH)]).encode(),
            'torrents/properties': json.dumps(dict(infohash_v1=HASH, infohash_v2='')).encode()}
        return PrivateResponse(200, values[path])


class QBittorrentCompatibility(TestCase):
    def setUp(self):
        self.http = ProfileHTTP()
        self.client = QBittorrentClient(config('qbittorrent'), self.http)
        self.torrent = ResolvedTorrent('b' * 64, 'fixture', b'', TorrentIdentity(v1=HASH),
                                       'magnet:?xt=urn:btih:' + HASH)

    def failure(self, code, function):
        with self.assertRaises(DownloadFailure) as error: function()
        self.assertEqual(error.exception.code, code)

    def test_legacy_and_current_login(self):
        for status, body, name in ((200, b'Ok.', 'SID'), (204, b'', 'QBT_SID_8080'),
                                  (204, b'', 'QBT_SID_65535')):
            with self.subTest(name=name):
                self.http.login = PrivateResponse(status, body, cookie=name + '=synthetic-session-id; HttpOnly')
                self.client._cookie = None
                result = self.client.check()
                self.assertEqual((result['product'], result['version'], result['api_version'], result['protocol']),
                                 ('qBittorrent', 'v5.2.4', '2.15.1', 'torrent'))
                self.assertEqual(self.http.calls[-1][1]['headers']['Cookie'], name + '=synthetic-session-id')
                self.assertEqual(self.http.calls[-1][1]['headers']['Referer'], 'http://client:8080/')

    def test_bad_login_or_cookie_never_authenticates(self):
        for cookie in ('', 'OTHER=synthetic-session-id', 'QBT_SID_0=synthetic-session-id',
                'QBT_SID_65536=synthetic-session-id', 'QBT_SID_08080=synthetic-session-id',
                'QBT_SID_8080=short', 'SID=synthetic-session-id; OTHER=synthetic-session-id',
                'SID=synthetic-session-id; SID=synthetic-session-id', 'SID=synthetic-session-id\r\nInjected: value',
                'SID="synthetic session id"'):
            with self.subTest(cookie=cookie):
                self.http.login = PrivateResponse(204, b'', cookie=cookie)
                self.failure(E.AUTHENTICATION, self.client.check)
                self.assertIsNone(self.client._cookie)
        for status, body in ((200, b'Fails.'), (200, b''), (204, b'Ok.')):
            self.http.login = PrivateResponse(status, body, cookie='SID=synthetic-session-id')
            self.failure(E.AUTHENTICATION, self.client.check)

    def test_upstream_base64_session_value_is_forwarded_exactly(self):
        value = '0123456789abcdefghijklmnopqrst+/'
        self.http.login = PrivateResponse(204, b'', cookie='QBT_SID_8080=' + value + '; HttpOnly')
        self.client.check()
        self.assertEqual(self.http.calls[-1][1]['headers']['Cookie'], 'QBT_SID_8080=' + value)

    def test_explicit_auth_rejection_reauthenticates_once(self):
        self.http.overrides['app/version'] = [DownloadFailure(E.AUTHENTICATION), PrivateResponse(200, b'v5.2.4')]
        self.client.check()
        self.assertEqual(sum(p == 'auth/login' for p, _ in self.http.calls), 2)
        self.http.overrides['app/version'] = DownloadFailure(E.AUTHENTICATION)
        self.failure(E.AUTHENTICATION, self.client.check)
        self.assertEqual(sum(p == 'auth/login' for p, _ in self.http.calls), 3)

    def test_redirect_transport_and_capability_failures(self):
        for response, code in ((PrivateResponse(302, b''), E.REDIRECT),
                (PrivateResponse(200, b'garbage'), E.CONFIGURATION),
                (PrivateResponse(204, b''), E.INVALID_RESPONSE),
                (DownloadFailure(E.TIMEOUT), E.TIMEOUT), (DownloadFailure(E.UNAVAILABLE), E.UNAVAILABLE)):
            self.http.overrides['app/version'] = response
            self.failure(code, self.client.check)
        self.http.overrides.clear()
        self.http.overrides['app/webapiVersion'] = PrivateResponse(200, b'2.8.0')
        self.failure(E.CONFIGURATION, self.client.check)
        self.http.overrides.clear()
        self.http.overrides['torrents/categories'] = PrivateResponse(200, b'{}')
        self.failure(E.CATEGORY, self.client.check)

    def test_modern_submission_receipt_and_exact_identity(self):
        self.assertEqual(self.client.submit(self.torrent, 'Ignored title'), HASH)
        upload = next(kwargs['body'] for p, kwargs in self.http.calls if p == 'torrents/add')
        self.assertIn(('pullarr-' + 'b' * 32).encode(), upload)
        self.http.overrides['torrents/properties'] = PrivateResponse(200, json.dumps(dict(infohash_v1='c' * 40)).encode())
        self.failure(E.AMBIGUOUS, lambda: self.client.submit(self.torrent, 'Ignored title'))

    def test_pending_resolution_is_bounded_without_resubmission(self):
        self.http.receipt = PrivateResponse(202, b'{"success_count":0,"failure_count":0,"pending_count":1,"added_torrent_ids":[]}')
        self.http.empty_reads = 2
        with patch('backend.implementations.qbittorrent.sleep') as pause:
            self.assertEqual(self.client.submit(self.torrent, 'Fixture'), HASH)
            self.assertEqual(pause.call_count, 2)
        self.assertEqual(sum(p == 'torrents/add' for p, _ in self.http.calls), 1)
        self.http.calls.clear(); self.http.empty_reads = 10
        with patch('backend.implementations.qbittorrent.sleep'):
            self.failure(E.AMBIGUOUS, lambda: self.client.submit(self.torrent, 'Fixture'))
        self.assertEqual(sum(p == 'torrents/info' for p, _ in self.http.calls), 3)
        self.assertEqual(sum(p == 'torrents/add' for p, _ in self.http.calls), 1)

    def test_malformed_receipts_errors_and_identity_mismatch(self):
        for response, code in ((PrivateResponse(200, b'Fails.'), E.REJECTED),
                (PrivateResponse(202, b'Ok.'), E.AMBIGUOUS),
                (PrivateResponse(204, b''), E.AMBIGUOUS),
                (PrivateResponse(200, b'{}'), E.AMBIGUOUS),
                (PrivateResponse(200, b'{"success_count":false,"failure_count":0,"pending_count":1,"added_torrent_ids":[]}'), E.AMBIGUOUS),
                (PrivateResponse(200, b'{"success_count":0,"failure_count":1,"pending_count":0,"added_torrent_ids":[]}'), E.REJECTED),
                (HTTPStatusFailure(409), E.REJECTED), (HTTPStatusFailure(415), E.REJECTED),
                (DownloadFailure(E.TIMEOUT), E.AMBIGUOUS), (HTTPStatusFailure(500), E.AMBIGUOUS)):
            self.http.calls.clear()
            self.http.overrides['torrents/add'] = response
            self.failure(code, lambda: self.client.submit(self.torrent, 'Fixture'))
            self.assertEqual(sum(p == 'torrents/add' for p, _ in self.http.calls), 1)
        self.http.overrides.clear()
        self.http.overrides['torrents/info'] = PrivateResponse(200, json.dumps([dict(hash='c' * 40)]).encode())
        self.failure(E.AMBIGUOUS, lambda: self.client.submit(self.torrent, 'Fixture'))

    def test_loopback_lifecycle_both_profiles(self):
        for profile in ('legacy', '5.2.4'):
            with self.subTest(profile=profile), services(profile) as remote:
                client = QBittorrentClient(replace(config('qbittorrent'), url=remote['url'] + '/qbit'))
                result = client.check()
                self.assertEqual(result['version'], 'v5.0.0' if profile == 'legacy' else 'v5.2.4')
                self.assertEqual(client.submit(self.torrent, 'Fixture'), HASH)
                self.assertEqual(client.observe((HASH,))[HASH].state, S.DOWNLOADING)
                remote['completed'] = True
                observation = client.observe((HASH,))[HASH]
                self.assertEqual(observation.state, S.COMPLETED)
                self.assertEqual(observation.paths, ('/complete/Batman 001 (2020).cbz',))
                self.assertEqual(observation.progress, 100)
                client.remove(HASH)
                self.assertFalse(remote['delete_data'])
                self.assertEqual(client.observe((HASH,))[HASH].error, E.MISSING)
                client.remove(HASH, delete_data=True)
                self.assertTrue(remote['delete_data'])
                bad = QBittorrentClient(replace(client.config, password='incorrect-fixture-password'))
                self.failure(E.AUTHENTICATION, bad.check)

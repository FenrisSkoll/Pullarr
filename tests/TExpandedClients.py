"""Offline protocol/retention contracts, synthetic credentials and bytes only."""
import base64
import json
from dataclasses import replace
from hashlib import sha1, sha256
from unittest import TestCase

from backend.base.download_job import (DownloadErrorCode as E, DownloadFailure,
                                       DownloadJobState as S, ResolvedNZB)
from backend.base.managed_client import (ManagedClientConfig, RetentionPolicy,
                                         retention_evaluation)
from backend.base.torrent import (ResolvedTorrent, TorrentIdentity,
                                  magnet_identity, torrent_identity)
from backend.implementations.download_transport import PrivateResponse
from backend.implementations.nzbget import NZBGetClient
from backend.implementations.qbittorrent import QBittorrentClient


def config(kind='nzbget'):
    return ManagedClientConfig('fixture', 'Fixture', 'http://client:8080', 'fixture-user', 'fixture-password', kind=kind)


class TorrentContracts(TestCase):
    def test_exact_v1_v2_hybrid_hashes(self):
        info = b'd6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:' + b'a' * 20 + b'e'
        self.assertEqual(torrent_identity(b'd4:info' + info + b'e').v1, sha1(info).hexdigest())
        v2 = b'd9:file treed1:xd0:d6:lengthi1eeee12:meta versioni2e4:name1:x12:piece lengthi16384ee'
        self.assertEqual(torrent_identity(b'd4:info' + v2 + b'e').v2, sha256(v2).hexdigest())
        hybrid = v2[:-1] + b'6:pieces20:' + b'a' * 20 + b'6:lengthi1ee'
        # Unsorted bencode is rejected, not reserialized before hashing.
        with self.assertRaises(DownloadFailure):
            torrent_identity(b'd4:info' + hybrid + b'e')
        hybrid = (b'd9:file treed1:xd0:d6:lengthi1eeee6:lengthi1e12:meta versioni2e'
                  b'4:name1:x12:piece lengthi16384e6:pieces20:' + b'a' * 20 + b'e')
        parsed = torrent_identity(b'd4:info' + hybrid + b'e')
        self.assertEqual(parsed.v1, sha1(hybrid).hexdigest())
        self.assertEqual(parsed.v2, sha256(hybrid).hexdigest())

    def test_magnet_normalization(self):
        digest = 'a' * 40
        encoded = base64.b32encode(bytes.fromhex(digest)).decode()
        first = magnet_identity('magnet:?xt=urn:btih:' + digest)
        self.assertEqual(first, magnet_identity('magnet:?dn=Different&xt=urn:btih:' + encoded))
        self.assertEqual(magnet_identity('magnet:?xt=urn:btmh:1220' + 'b' * 64).v2, 'b' * 64)
        with self.assertRaises(DownloadFailure):
            magnet_identity('magnet:?xt=urn:btih:' + digest + '&xt=urn:btih:' + 'b' * 40)

    def test_malformed_and_bounds(self):
        for data in (b'', b'd4:infoi1ee', b'd4:infod4:name1:xee', b'l' * 50, b'i01e', b'99999999:x'):
            with self.subTest(size=len(data)), self.assertRaises(DownloadFailure):
                torrent_identity(data)

    def test_all_retention_modes(self):
        for mode, expected in (('client_managed', False), ('keep', False), ('after_import', True),
                ('ratio', False), ('seedtime', True), ('either', True), ('both', False)):
            result = retention_evaluation(RetentionPolicy(mode, '1', 60), current_ratio='.5', seeding_seconds=60)
            self.assertEqual(result['eligible'], expected, mode)

    def test_tracker_predicates_conjoined(self):
        policy = RetentionPolicy('ratio', '.5', 60)
        source = {'seedtype': 'ratio', 'minimumratio': '1'}
        self.assertFalse(retention_evaluation(policy, current_ratio='.5', seeding_seconds=100, requirements=source)['eligible'])
        self.assertTrue(retention_evaluation(policy, current_ratio='1', seeding_seconds=100, requirements=source)['eligible'])
        for mode, expected in (('ratio', False), ('seedtime', True), ('both', False), ('either', True), ('unknown', False)):
            source = dict(seedtype=mode, minimumratio='1', minimumseedtime=60)
            self.assertEqual(retention_evaluation(policy, current_ratio='.5', seeding_seconds=60, requirements=source)['eligible'], expected)

    def test_bad_metrics_fail_closed(self):
        for value in ('NaN', 'Infinity', '-1', True, None):
            self.assertFalse(retention_evaluation(RetentionPolicy('ratio'), current_ratio=value, seeding_seconds=60)['eligible'])


class NZBGetContracts(TestCase):
    def test_submission_and_queue_history(self):
        class Transport:
            def request(self, url, **kwargs):
                call = json.loads(kwargs['body'])
                self.last = call
                values = {'version': '25.4', 'append': 17, 'listgroups': [],
                    'history': [dict(NZBID=17, Kind='NZB', Status='SUCCESS/ALL', DestDir='/downloads/fixture')]}
                return PrivateResponse(200, json.dumps(dict(id=1, result=values[call['method']])).encode())
        transport = Transport()
        client = NZBGetClient(config(), transport)
        self.assertEqual(client.check()['product'], 'NZBGet')
        nzb = ResolvedNZB('a', 'b', b'synthetic-nzb', 'digest', 'release.nzb')
        self.assertEqual(client.submit(nzb, 'Fixture'), '17')
        self.assertEqual(base64.b64decode(transport.last['params'][1]), nzb.data)
        self.assertEqual(client.observe(('17',))['17'].state, S.COMPLETED)
        self.assertEqual(client.observe(('18',))['18'].state, S.REMOTE_UNKNOWN)

    def test_failure_is_not_completion(self):
        for status in ('FAILURE/UNPACK', 'DELETED/MANUAL', 'WARNING/SCRIPT', 'SUCCESS/MARK'):
            class Transport:
                def request(self, url, **kwargs):
                    method = json.loads(kwargs['body'])['method']
                    result = [] if method == 'listgroups' else [dict(NZBID=1, Kind='NZB', Status=status)]
                    return PrivateResponse(200, json.dumps(dict(id=1, result=result)).encode())
            self.assertNotEqual(NZBGetClient(config(), Transport()).observe(('1',))['1'].state, S.COMPLETED)


class QBittorrentContracts(TestCase):
    def test_torrent_bytes_upload_never_passes_indexer_url(self):
        info = b'd6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:' + b'a' * 20 + b'e'
        data = b'd4:info' + info + b'e'
        identity = torrent_identity(data)
        class Transport:
            upload = b''
            def request(self, url, **kwargs):
                path = url.split('/api/v2/')[1].split('?')[0]
                if path == 'auth/login':
                    return PrivateResponse(200, b'Ok.', cookie='SID=synthetic-session-id; HttpOnly')
                values = {'app/version': b'v5.0.0', 'app/webapiVersion': b'2.11.0',
                    'torrents/categories': b'{"pullarr":{}}', 'torrents/add': b'Ok.',
                    'torrents/info': json.dumps([dict(hash=identity.v1)]).encode(),
                    'torrents/properties': json.dumps(dict(infohash_v1=identity.v1,infohash_v2='')).encode()}
                if path == 'torrents/add': self.upload = kwargs['body']
                return PrivateResponse(200, values[path])
        http = Transport()
        client = QBittorrentClient(config('qbittorrent'), http)
        self.assertEqual(client.submit(ResolvedTorrent('a' * 64,'fixture',data,identity),'Fixture'),identity.v1)
        self.assertIn(data,http.upload)
        self.assertIn(b'filename="release.torrent"',http.upload)
        self.assertNotIn(b'name="urls"',http.upload)
        self.assertNotIn(b'apikey',http.upload)

    def test_completed_selected_files_only_and_path_safety(self):
        class Transport:
            state = 'uploading'
            files = [dict(name='Book.cbz',size=10,priority=1,progress=1),
                     dict(name='Unselected.cbz',size=10,priority=0,progress=0)]
            def request(self,url,**kwargs):
                path = url.split('/api/v2/')[1].split('?')[0]
                if path == 'auth/login':
                    return PrivateResponse(200,b'Ok.',cookie='SID=synthetic-session-id; HttpOnly')
                value = self.files if path == 'torrents/files' else [dict(hash='a' * 40, category='pullarr',
                    state=self.state, progress=1,save_path='/complete')]
                return PrivateResponse(200,json.dumps(value).encode())
        http = Transport(); client = QBittorrentClient(config('qbittorrent'),http)
        result = client.observe(('a' * 40,))['a' * 40]
        self.assertEqual(result.state,S.COMPLETED)
        self.assertEqual(result.progress,100.)
        self.assertEqual(result.paths,('/complete/Book.cbz',))
        http.state = 'checkingUP'
        self.assertEqual(client.observe(('a' * 40,))['a' * 40].state,S.POST_PROCESSING)
        http.state = 'uploading'
        for name in ('../escape.cbz','/absolute.cbz','a/../../escape.cbz','a\\escape.cbz','NUL.cbz'):
            http.files = [dict(name=name,size=10,priority=1,progress=1)]
            with self.subTest(name=name),self.assertRaises(DownloadFailure):
                client.observe(('a' * 40,))

    def test_transient_sid_and_exact_identity(self):
        identity = TorrentIdentity(v2='b' * 64)
        class Transport:
            def __init__(self):
                self.login = 0
                self.expire = True
                self.removals = []

            def request(self, url, **kwargs):
                path = url.split('/api/v2/')[1].split('?')[0]
                if path == 'auth/login':
                    self.login += 1
                    return PrivateResponse(200, b'Ok.', cookie='SID=synthetic-session-id; HttpOnly')
                if self.expire:
                    self.expire = False
                    raise DownloadFailure(E.AUTHENTICATION)
                assert kwargs['headers']['Cookie'] == 'SID=synthetic-session-id'
                values = {'app/version': b'v5.0.0', 'app/webapiVersion': b'2.9.3',
                    'torrents/categories': b'{"pullarr":{}}', 'torrents/add': b'Ok.',
                    'torrents/info': json.dumps([dict(hash='c' * 40)]).encode(),
                    'torrents/properties': json.dumps(dict(infohash_v1='', infohash_v2=identity.v2)).encode(),
                    'torrents/delete': b''}
                if path == 'torrents/delete':
                    self.removals.append(kwargs['body'])
                return PrivateResponse(200, values[path])
        http = Transport()
        client = QBittorrentClient(config('qbittorrent'), http)
        self.assertEqual(client.check()['version'], 'v5.0.0')
        self.assertEqual(http.login, 2)
        torrent = ResolvedTorrent('a' * 64, 'fixture', b'', identity, 'magnet:?xt=urn:btmh:1220' + 'b' * 64)
        self.assertEqual(client.submit(torrent, 'Fixture'), 'c' * 40)
        client.remove('c' * 40)
        client.remove('c' * 40, delete_data=True)
        self.assertIn(b'deleteFiles=false', http.removals[0])
        self.assertIn(b'deleteFiles=true', http.removals[1])
        self.assertNotIn('synthetic-session', repr(config('qbittorrent')))

    def test_config_safe_preview(self):
        value = config('qbittorrent')
        self.assertNotIn(value.password, json.dumps(value.preview()))
        self.assertNotIn(value.password, repr(value))
        for url in ('file:///tmp', 'http://user:password@client', 'http://client?token=secret'):
            with self.assertRaises(DownloadFailure):
                replace(value, url=url)

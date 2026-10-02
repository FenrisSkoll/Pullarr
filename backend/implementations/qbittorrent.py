"""qBittorrent 5 / WebAPI v2: configured origin, transient SID, exact hashes."""
import json
import re
from http.cookies import CookieError, SimpleCookie
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlencode
from uuid import uuid4

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, DownloadJobState as S,
                                       RemoteDownload, endpoint)
from backend.implementations.download_transport import DownloadHTTP


class QBittorrentClient:
    def __init__(self, config, http=None):
        self.config, self.http = config, http or DownloadHTTP()
        self._sid = None

    def _login(self):
        result = self.http.request(endpoint(self.config.url) + '/api/v2/auth/login', method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': endpoint(self.config.url) + '/'},
            body=urlencode(dict(username=self.config.username, password=self.config.password)).encode(), maximum=4096)
        cookies = SimpleCookie()
        try:
            cookies.load(result.cookie)
            sid = cookies['SID'].value
            if result.body.strip() != b'Ok.' or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', sid):
                raise ValueError
        except (KeyError, ValueError, CookieError):
            raise DownloadFailure(E.AUTHENTICATION) from None
        self._sid = sid

    def _call(self, path, params=None, *, post=False, body=None, content_type=None, raw=False):
        if self._sid is None:
            self._login()
        for attempt in range(2):
            url = endpoint(self.config.url) + '/api/v2/' + path
            encoded = urlencode(params or {}).encode()
            if not post and encoded:
                url += '?' + encoded.decode()
            try:
                response = self.http.request(url, method='POST' if post else 'GET',
                    headers={'Cookie': 'SID=' + self._sid, 'Referer': endpoint(self.config.url) + '/',
                             'Content-Type': content_type or 'application/x-www-form-urlencoded'},
                    body=body if body is not None else encoded if post else b'', maximum=8 * 1024 * 1024)
                if response.status != 200:
                    raise DownloadFailure(E.INVALID_RESPONSE)
                if raw:
                    return response.body
                try:
                    return json.loads(response.body)
                except (ValueError, UnicodeError, RecursionError):
                    raise DownloadFailure(E.INVALID_RESPONSE) from None
            except DownloadFailure as error:
                # Only an explicit authentication refusal can retry a mutation.
                if error.code != E.AUTHENTICATION or attempt:
                    raise
                self._sid = None
                self._login()

    def check(self):
        try:
            app = self._call('app/version', raw=True).decode('ascii')
            api = self._call('app/webapiVersion', raw=True).decode('ascii')
        except UnicodeError:
            raise DownloadFailure(E.INVALID_RESPONSE) from None
        if not re.fullmatch(r'v?5\.[0-9]+\.[0-9]+(?:[A-Za-z0-9._-]*)', app) or not re.fullmatch(r'2\.[0-9]+\.[0-9]+', api):
            raise DownloadFailure(E.CONFIGURATION)
        if tuple(map(int, api.split('.'))) < (2, 9, 3):
            raise DownloadFailure(E.CONFIGURATION)
        categories = self._call('torrents/categories')
        if not isinstance(categories, dict) or len(categories) > 256:
            raise DownloadFailure(E.INVALID_RESPONSE)
        if self.config.category and self.config.category not in categories:
            raise DownloadFailure(E.CATEGORY)
        return dict(product='qBittorrent', version=app, api_version=api, protocol='torrent',
                    capabilities=['submit', 'exact_hash', 'files', 'seeding', 'remove'])

    def info(self, hashes):
        if not 0 < len(hashes) <= 100 or any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', h) for h in hashes):
            raise DownloadFailure(E.CONFIGURATION)
        rows = self._call('torrents/info', {'hashes': '|'.join(hashes)})
        if not isinstance(rows, list) or len(rows) > len(hashes):
            raise DownloadFailure(E.INVALID_RESPONSE)
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or row.get('hash') not in hashes or row['hash'] in seen:
                raise DownloadFailure(E.INVALID_RESPONSE)
            seen.add(row['hash'])
        return rows

    def properties(self, remote_hash):
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', remote_hash):
            raise DownloadFailure(E.CONFIGURATION)
        value = self._call('torrents/properties', {'hash': remote_hash})
        if not isinstance(value, dict):
            raise DownloadFailure(E.INVALID_RESPONSE)
        return value

    def observe(self, hashes):
        """Completion is independent of seeding; absence never means success.

        Only selected, fully completed file identities are handed to intake.
        Remote paths remain untrusted until client-scoped mapping admits them.
        """
        rows = self.info(hashes)
        results = {}
        complete_states = {'uploading', 'stalledUP', 'queuedUP', 'pausedUP', 'stoppedUP', 'forcedUP'}
        download_states = {'downloading', 'stalledDL', 'forcedDL', 'metaDL', 'forcedMetaDL'}
        for row in rows:
            identifier = row['hash']
            status = row.get('state')
            progress = row.get('progress')
            if type(progress) not in (int, float) or not 0 <= progress <= 1:
                raise DownloadFailure(E.INVALID_RESPONSE)
            state = S.REMOTE_UNKNOWN
            paths = ()
            if row.get('category') != self.config.category:
                results[identifier] = RemoteDownload(identifier, state, 'Category changed', error=E.CATEGORY)
                continue
            if status in complete_states and progress == 1:
                files = self.files(identifier)
                selected = [f for f in files if f['priority'] > 0]
                save = row.get('save_path')
                if (not isinstance(save, str) or not 0 < len(save) <= 4096
                        or any(ord(c) < 32 for c in save)
                        or self.config.password and self.config.password in save):
                    raise DownloadFailure(E.INVALID_RESPONSE)
                base = PureWindowsPath(save) if '\\' in save or re.match(r'^[A-Za-z]:', save) else PurePosixPath(save)
                if not base.is_absolute() or '..' in base.parts:
                    raise DownloadFailure(E.INVALID_RESPONSE)
                if selected and all(f['progress'] == 1 for f in selected):
                    paths = tuple(str(base.joinpath(*PurePosixPath(f['name']).parts)) for f in selected)
                    state = S.COMPLETED
            elif status in download_states:
                state = S.DOWNLOADING
            elif status in ('queuedDL', 'pausedDL', 'stoppedDL'):
                state = S.QUEUED
            elif status in ('checkingUP', 'checkingDL', 'checkingResumeData', 'moving', 'allocating'):
                state = S.POST_PROCESSING
            elif status in ('error', 'missingFiles'):
                state = S.FAILED
            results[identifier] = RemoteDownload(identifier, state,
                status if state != S.REMOTE_UNKNOWN else 'Unrecognized', self.config.category,
                progress=float(progress) * 100, paths=paths, error=E.REMOTE_FAILED if state == S.FAILED else None)
        for identifier in hashes:
            results.setdefault(identifier, RemoteDownload(identifier, S.REMOTE_UNKNOWN, 'Missing', error=E.MISSING))
        return results

    def files(self, remote_hash):
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', remote_hash):
            raise DownloadFailure(E.CONFIGURATION)
        values = self._call('torrents/files', {'hash': remote_hash})
        if not isinstance(values, list) or not 0 < len(values) <= 1000:
            raise DownloadFailure(E.LIMIT)
        from backend.base.acquisition_intake import IntakeFailure
        from backend.implementations.acquisition_preparation import _member
        names = set()
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get('name'), str):
                raise DownloadFailure(E.INVALID_RESPONSE)
            try:
                name = _member(value['name'])
            except IntakeFailure:
                raise DownloadFailure(E.INVALID_RESPONSE) from None
            if name.casefold() in names or type(value.get('size')) is not int or value['size'] < 0:
                raise DownloadFailure(E.INVALID_RESPONSE)
            names.add(name.casefold())
            if (type(value.get('priority')) is not int or value['priority'] < 0
                    or type(value.get('progress')) not in (int, float) or not 0 <= value['progress'] <= 1):
                raise DownloadFailure(E.INVALID_RESPONSE)
        return values

    def submit(self, torrent, title):
        # Category must already exist; no implicit category/global-setting writes.
        self.check()
        boundary = 'pullarr-' + uuid4().hex
        tag = 'pullarr-' + torrent.candidate_id[:32]
        if not re.fullmatch(r'pullarr-[0-9a-f]{32}', tag):
            raise DownloadFailure(E.SELECTION)
        fields = {'category': self.config.category, 'tags': tag}
        if torrent.magnet:
            fields['urls'] = torrent.magnet
        parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                 for key, value in fields.items()]
        if torrent.data:
            parts.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="release.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n').encode(), torrent.data, b'\r\n'])
        parts.append(f'--{boundary}--\r\n'.encode())
        try:
            result = self._call('torrents/add', post=True, body=b''.join(parts),
                content_type='multipart/form-data; boundary=' + boundary, raw=True)
            if result.strip() != b'Ok.':
                raise DownloadFailure(E.REJECTED)
        except DownloadFailure as error:
            if error.code in (E.AUTHENTICATION, E.REJECTED):
                raise
            raise DownloadFailure(E.AMBIGUOUS) from None
        return self.find_submission(torrent.identity, torrent.candidate_id)

    def find_submission(self, identity, candidate_id):
        tag = 'pullarr-' + candidate_id[:32]
        if not re.fullmatch(r'pullarr-[0-9a-f]{32}', tag):
            raise DownloadFailure(E.SELECTION)
        # Upload response has no ID. Resolve only exact protocol hashes, never name.
        rows = self._call('torrents/info', {'tag': tag, 'limit': 2})
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise DownloadFailure(E.AMBIGUOUS)
        remote = rows[0].get('hash', '')
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', remote):
            raise DownloadFailure(E.AMBIGUOUS)
        properties = self.properties(remote)
        if (identity.v1 and properties.get('infohash_v1') != identity.v1
                or identity.v2 and properties.get('infohash_v2') != identity.v2):
            raise DownloadFailure(E.AMBIGUOUS)
        return remote  # Client-issued TorrentID, separate from full protocol hashes.

    def remove(self, remote_hash, *, delete_data=False):
        # Service must admit the durable owned job and current cleanup review first.
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', remote_hash) or type(delete_data) is not bool:
            raise DownloadFailure(E.CONFIGURATION)
        self._call('torrents/delete', {'hashes': remote_hash, 'deleteFiles': str(delete_data).lower()}, post=True, raw=True)

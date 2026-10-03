"""qBittorrent 5 / WebAPI v2: configured origin, transient session, exact hashes."""
import json
import re
from http.cookies import CookieError, SimpleCookie
from pathlib import PurePosixPath, PureWindowsPath
from time import sleep
from urllib.parse import urlencode
from uuid import uuid4

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, DownloadJobState as S,
                                       RemoteDownload, endpoint)
from backend.implementations.download_transport import (DownloadHTTP,
                                                        HTTPStatusFailure)


class QBittorrentClient:
    def __init__(self, config, http=None):
        self.config, self.http = config, http or DownloadHTTP()
        self._cookie = None

    def _login(self):
        result = self.http.request(endpoint(self.config.url) + '/api/v2/auth/login', method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': endpoint(self.config.url) + '/'},
            body=urlencode(dict(username=self.config.username, password=self.config.password)).encode(), maximum=4096,
            success_statuses=(200, 204))
        if result.status in (301, 302, 303, 307, 308):
            raise DownloadFailure(E.REDIRECT)
        cookies = SimpleCookie()
        try:
            if any(ord(c) < 32 or ord(c) == 127 for c in result.cookie):
                raise ValueError
            cookies.load(result.cookie)
            if len(cookies) != 1:
                raise ValueError
            name, cookie = next(iter(cookies.items()))
            if (not result.cookie.startswith(name + '=')
                    or len(re.findall(r'(?:^|[;,]\s*)' + re.escape(name) + '=', result.cookie)) != 1):
                raise ValueError
            # Upstream uses its configured WebUI port, which a reverse proxy
            # need not expose as the configured client's URL port.
            modern = re.fullmatch(r'QBT_SID_([1-9][0-9]{0,4})', name)
            if name != 'SID' and not (modern and int(modern[1]) <= 65535):
                raise ValueError
            if (not re.fullmatch(r'[A-Za-z0-9_+/-]{16,256}', cookie.value)
                    or not ((result.status == 200 and result.body.strip() == b'Ok.')
                            or (result.status == 204 and result.body == b''))):
                raise ValueError
        except (KeyError, ValueError, CookieError):
            raise DownloadFailure(E.AUTHENTICATION) from None
        self._cookie = name + '=' + cookie.value

    def _call(self, path, params=None, *, post=False, body=None, content_type=None, raw=False,
              success_statuses=(200,), full_response=False):
        if self._cookie is None:
            self._login()
        for attempt in range(2):
            url = endpoint(self.config.url) + '/api/v2/' + path
            encoded = urlencode(params or {}).encode()
            if not post and encoded:
                url += '?' + encoded.decode()
            try:
                response = self.http.request(url, method='POST' if post else 'GET',
                    headers={'Cookie': self._cookie, 'Referer': endpoint(self.config.url) + '/',
                             'Content-Type': content_type or 'application/x-www-form-urlencoded'},
                    body=body if body is not None else encoded if post else b'', maximum=8 * 1024 * 1024,
                    success_statuses=success_statuses)
                if response.status in (301, 302, 303, 307, 308):
                    raise DownloadFailure(E.REDIRECT)
                if response.status not in success_statuses:
                    raise DownloadFailure(E.INVALID_RESPONSE)
                if full_response:
                    return response
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
                self._cookie = None
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
                content_type='multipart/form-data; boundary=' + boundary,
                success_statuses=(200, 202), full_response=True)
            pending, acknowledged = False, None
            if result.status == 200 and result.body.strip() == b'Fails.':
                raise DownloadFailure(E.REJECTED)
            if not (result.status == 200 and result.body.strip() == b'Ok.'):
                try:
                    receipt = json.loads(result.body)
                except (ValueError, UnicodeError, RecursionError):
                    raise DownloadFailure(E.AMBIGUOUS) from None
                if not isinstance(receipt, dict):
                    raise DownloadFailure(E.AMBIGUOUS)
                counts = [receipt.get(k) for k in ('success_count', 'failure_count', 'pending_count')]
                ids = receipt.get('added_torrent_ids')
                if (any(type(n) is not int or n not in (0, 1) for n in counts)
                        or sum(counts) != 1 or not isinstance(ids, list)
                        or len(ids) != counts[0]
                        or any(not isinstance(h, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', h) for h in ids)
                        or (result.status == 202) != bool(counts[2])):
                    raise DownloadFailure(E.AMBIGUOUS)
                if counts[1]:
                    raise DownloadFailure(E.REJECTED)
                pending = bool(counts[2])
                acknowledged = ids[0] if ids else None
        except DownloadFailure as error:
            if isinstance(error, HTTPStatusFailure) and error.status in (400, 409, 415):
                raise DownloadFailure(E.REJECTED) from None
            if error.code in (E.AUTHENTICATION, E.REJECTED):
                raise
            raise DownloadFailure(E.AMBIGUOUS) from None
        # Retry reads only for an explicitly pending receipt. Never resubmit.
        return self.find_submission(torrent.identity, torrent.candidate_id,
                                    attempts=3 if pending else 1, acknowledged=acknowledged)

    def find_submission(self, identity, candidate_id, *, attempts=1, acknowledged=None):
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise DownloadFailure(E.CONFIGURATION)
        tag = 'pullarr-' + candidate_id[:32]
        if not re.fullmatch(r'pullarr-[0-9a-f]{32}', tag):
            raise DownloadFailure(E.SELECTION)
        # Confirm candidate correlation and full protocol hashes, never name.
        for attempt in range(attempts):
            try:
                rows = self._call('torrents/info', {'tag': tag, 'limit': 2})
            except DownloadFailure:
                raise DownloadFailure(E.AMBIGUOUS) from None
            if rows != [] or attempt == attempts - 1:
                break
            sleep(.25)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise DownloadFailure(E.AMBIGUOUS)
        remote = rows[0].get('hash', '')
        if (not isinstance(remote, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', remote)
                or acknowledged is not None and remote != acknowledged):
            raise DownloadFailure(E.AMBIGUOUS)
        try:
            properties = self.properties(remote)
        except DownloadFailure:
            raise DownloadFailure(E.AMBIGUOUS) from None
        if (identity.v1 and properties.get('infohash_v1') != identity.v1
                or identity.v2 and properties.get('infohash_v2') != identity.v2):
            raise DownloadFailure(E.AMBIGUOUS)
        return remote  # Client-issued TorrentID, separate from full protocol hashes.

    def remove(self, remote_hash, *, delete_data=False):
        # Service must admit the durable owned job and current cleanup review first.
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', remote_hash) or type(delete_data) is not bool:
            raise DownloadFailure(E.CONFIGURATION)
        self._call('torrents/delete', {'hashes': remote_hash, 'deleteFiles': str(delete_data).lower()},
                   post=True, raw=True, success_statuses=(200, 204))

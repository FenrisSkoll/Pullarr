"""SAB 5.1 documented API subset; byte upload and exact-ID observations only."""

import json
import math
import re
from urllib.parse import urlencode
from uuid import uuid4

from backend.base.download_job import (DownloadErrorCode as E, DownloadFailure,
                                       DownloadJobState as S, RemoteDownload,
                                       ResolvedNZB, SABConfig, endpoint)
from backend.implementations.download_transport import DownloadHTTP

STATUS_BATCH = 100
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_QUEUE = {'Queued': S.QUEUED, 'Paused': S.QUEUED, 'Propagating': S.QUEUED,
          'Downloading': S.DOWNLOADING, 'Fetching': S.DOWNLOADING,
          'Checking': S.DOWNLOADING, 'Grabbing': S.DOWNLOADING}
_HISTORY = {s: S.POST_PROCESSING for s in ('Queued', 'QuickCheck', 'Verifying',
            'Repairing', 'Fetching', 'Extracting', 'Moving', 'Running')}
_HISTORY.update(Completed=S.COMPLETED, Failed=S.FAILED)


def safe_text(value, maximum, secret):
    if (not isinstance(value, str) or len(value) > maximum or secret in value
            or re.search(r'(?i)(https?://|apikey\s*=|token\s*=|authorization:|cookie:)', value)
            or any(ord(c) < 32 for c in value)):
        return None
    return value


class SABClient:
    def __init__(self, config: SABConfig, http=None):
        self.config, self.http = config, http or DownloadHTTP()

    def _call(self, mode, params=None, *, body=b'', content_type=None):
        fields = {'mode': mode, 'output': 'json', 'apikey': self.config.api_key, **(params or {})}
        url = endpoint(self.config.url) + '/api'
        if body:
            response = self.http.request(url, method='POST', body=body,
                headers={'Content-Type': content_type}, maximum=1024 * 1024)
        else:
            response = self.http.request(url + '?' + urlencode(fields), maximum=1024 * 1024)
        if response.status != 200:
            raise DownloadFailure(E.INVALID_RESPONSE)
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise DownloadFailure(E.INVALID_RESPONSE) from None
        if not isinstance(data, dict):
            raise DownloadFailure(E.INVALID_RESPONSE)
        if data.get('error') or data.get('status') is False:
            if mode == 'addfile' and data.get('nzo_ids'):
                # Contradictory receipt may still describe an accepted upload.
                raise DownloadFailure(E.AMBIGUOUS)
            if data.get('error') in ('API Key Incorrect', 'API Key Required'):
                raise DownloadFailure(E.AUTHENTICATION)
            raise DownloadFailure(E.REJECTED if mode == 'addfile' else E.INVALID_RESPONSE)
        return data

    def categories(self):
        cats = self._call('get_cats').get('categories')
        if (not isinstance(cats, list) or len(cats) > 256
                or any(not c or safe_text(c, 128, self.config.api_key) is None for c in cats)):
            raise DownloadFailure(E.INVALID_RESPONSE)
        return tuple(dict.fromkeys(cats))

    def check(self):
        """No upload: exercise full-key queue/history access, version and category."""
        version = self._call('version').get('version')
        if not isinstance(version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,63}', version):
            raise DownloadFailure(E.INVALID_RESPONSE)
        for mode in ('queue', 'history'):
            self._slots(mode, ('Kapowarr_connection_test_no_job',))
        cats = self.categories()
        if self.config.category != '*' and self.config.category not in cats:
            raise DownloadFailure(E.CATEGORY)
        return dict(version=version, categories=cats, full_api_access=True)

    def submit(self, nzb: ResolvedNZB, title: str) -> str:
        """Exactly one attempt. Caller must durably mark SUBMITTING first."""
        boundary = 'kapowarr-' + uuid4().hex
        name = safe_text(title, 512, self.config.api_key)
        if not name or any(c in name for c in '/\\{}'):
            name = 'Comic release'
        fields = dict(mode='addfile', output='json', apikey=self.config.api_key,
                      nzbname=name, cat=self.config.category, priority=str(self.config.priority), pp='-1')
        parts = []
        for key, value in fields.items():
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                          f'{value}\r\n').encode('utf-8'))
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="name"; '
                      'filename="release.nzb"\r\nContent-Type: application/x-nzb\r\n\r\n').encode())
        parts.extend((nzb.data, f'\r\n--{boundary}--\r\n'.encode()))
        try:
            data = self._call('addfile', body=b''.join(parts),
                              content_type='multipart/form-data; boundary=' + boundary)
        except DownloadFailure as exc:
            if exc.code in (E.AUTHENTICATION, E.REJECTED):
                raise
            raise DownloadFailure(E.AMBIGUOUS) from None
        ids = data.get('nzo_ids')
        if (data.get('status') is not True or not isinstance(ids, list) or len(ids) != 1
                or not isinstance(ids[0], str) or not _ID.fullmatch(ids[0])
                or self.config.api_key in ids[0]):
            raise DownloadFailure(E.AMBIGUOUS)
        return ids[0]

    def _slots(self, mode, ids):
        payload = self._call(mode, {'nzo_ids': ','.join(ids), 'start': 0, 'limit': len(ids)})
        data = payload.get(mode)
        if not isinstance(data, dict) or not isinstance(data.get('slots'), list):
            raise DownloadFailure(E.INVALID_RESPONSE)
        slots = data['slots']
        if len(slots) > len(ids):
            raise DownloadFailure(E.INVALID_RESPONSE)
        seen = set()
        for slot in slots:
            if (not isinstance(slot, dict) or slot.get('nzo_id') not in ids
                    or slot['nzo_id'] in seen):
                raise DownloadFailure(E.INVALID_RESPONSE)
            seen.add(slot['nzo_id'])
        return slots

    def observe(self, ids):
        """At most two targeted calls per 100 IDs. No remote administration."""
        if (not 0 < len(ids) <= STATUS_BATCH or len(set(ids)) != len(ids)
                or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in ids)):
            raise DownloadFailure(E.CONFIGURATION)
        results = {}
        for mode in ('queue', 'history'):
            remaining = tuple(i for i in ids if i not in results)
            if not remaining:
                break
            for slot in self._slots(mode, remaining):
                status = slot.get('status')
                mapping = _QUEUE if mode == 'queue' else _HISTORY
                state = mapping.get(status, S.REMOTE_UNKNOWN) if isinstance(status, str) else S.REMOTE_UNKNOWN
                progress = None
                try:
                    raw = slot.get('percentage')
                    if isinstance(raw, (int, float, str)) and not isinstance(raw, bool):
                        number = float(raw)
                        if math.isfinite(number) and 0 <= number <= 100:
                            progress = number
                except (ValueError, OverflowError):
                    pass
                completed = slot.get('completed')
                if type(completed) is not int or not 0 < completed < 10**12:
                    completed = None
                results[slot['nzo_id']] = RemoteDownload(slot['nzo_id'], state,
                    status if state != S.REMOTE_UNKNOWN else 'Unrecognized',
                    safe_text(slot.get('cat' if mode == 'queue' else 'category'), 128, self.config.api_key),
                    progress, safe_text(slot.get('storage'), 4096, self.config.api_key), completed,
                    E.REMOTE_FAILED if state == S.FAILED else None)
        for identifier in ids:
            results.setdefault(identifier, RemoteDownload(identifier, S.REMOTE_UNKNOWN, 'Missing', error=E.MISSING))
        return results

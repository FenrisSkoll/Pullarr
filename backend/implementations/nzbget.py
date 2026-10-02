"""NZBGet JSON-RPC: exact NZBID, bounded queue/history, no remote administration."""
import base64
import json
import re

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, DownloadJobState as S,
                                       RemoteDownload, endpoint)
from backend.implementations.download_transport import DownloadHTTP


class NZBGetClient:
    def __init__(self, config, http=None):
        self.config, self.http = config, http or DownloadHTTP()

    def _call(self, method, params=()):
        token = base64.b64encode((self.config.username + ':' + self.config.password).encode()).decode()
        response = self.http.request(endpoint(self.config.url) + '/jsonrpc', method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Basic ' + token},
            body=json.dumps(dict(method=method, params=list(params), id=1)).encode(), maximum=8 * 1024 * 1024)
        try:
            value = json.loads(response.body)
            if not isinstance(value, dict) or value.get('id') != 1 or value.get('error') or 'result' not in value:
                raise ValueError
            return value['result']
        except (ValueError, UnicodeError, RecursionError):
            raise DownloadFailure(E.INVALID_RESPONSE) from None

    def check(self):
        version = self._call('version')
        if not isinstance(version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,63}', version):
            raise DownloadFailure(E.INVALID_RESPONSE)
        if int(re.match(r'[0-9]+', version).group()) < 21:
            raise DownloadFailure(E.CONFIGURATION)
        self._rows('listgroups', (0,))
        self._rows('history', (False,))
        return dict(product='NZBGet', version=version, protocol='usenet', capabilities=['submit', 'queue', 'history'])

    def submit(self, nzb, title):
        try:
            identifier = self._call('append', ('release.nzb', base64.b64encode(nzb.data).decode(),
                self.config.category, self.config.priority, False, False, '', 0, 'SCORE'))
        except DownloadFailure as error:
            if error.code == E.AUTHENTICATION:
                raise
            raise DownloadFailure(E.AMBIGUOUS) from None
        if type(identifier) is not int:
            raise DownloadFailure(E.AMBIGUOUS)
        if identifier <= 0:
            raise DownloadFailure(E.REJECTED)
        return str(identifier)

    def _rows(self, method, params):
        rows = self._call(method, params)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise DownloadFailure(E.LIMIT)
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('NZBID')) is not int
                    or row['NZBID'] <= 0 or row['NZBID'] in seen):
                raise DownloadFailure(E.INVALID_RESPONSE)
            seen.add(row['NZBID'])
        return rows

    def observe(self, ids):
        if not 0 < len(ids) <= 1000 or any(not re.fullmatch(r'[1-9][0-9]{0,18}', i) for i in ids):
            raise DownloadFailure(E.CONFIGURATION)
        wanted = set(ids)
        results = {}
        for method, params in (('listgroups', (0,)), ('history', (False,))):
            for row in self._rows(method, params):
                identifier = str(row['NZBID'])
                if identifier not in wanted or identifier in results:
                    continue
                status = row.get('Status')
                state = S.REMOTE_UNKNOWN
                if method == 'listgroups':
                    if status in ('QUEUED', 'PAUSED'):
                        state = S.QUEUED
                    elif status in ('DOWNLOADING', 'FETCHING'):
                        state = S.DOWNLOADING
                    elif status in ('PP_QUEUED', 'LOADING_PARS', 'VERIFYING_SOURCES', 'REPAIRING',
                            'VERIFYING_REPAIRED', 'RENAMING', 'UNPACKING', 'MOVING', 'EXECUTING_SCRIPT',
                            'POST_RENAMING', 'POST_UNPACK_RENAMING'):
                        state = S.POST_PROCESSING
                elif row.get('Kind') == 'NZB':
                    if status in ('SUCCESS/ALL', 'SUCCESS/UNPACK', 'SUCCESS/PAR', 'SUCCESS/HEALTH'):
                        state = S.COMPLETED
                    elif isinstance(status, str) and status.startswith(('FAILURE/', 'DELETED/', 'WARNING/')):
                        state = S.FAILED
                storage = row.get('FinalDir') or row.get('DestDir')
                if (not isinstance(storage, str) or len(storage) > 4096 or any(ord(c) < 32 for c in storage)
                        or self.config.password in storage):
                    storage = None
                progress = 100. if state == S.COMPLETED else None
                total, remaining = row.get('FileSizeMB'), row.get('RemainingSizeMB')
                if (type(total) in (int, float) and type(remaining) in (int, float)
                        and 0 < total <= 10**15
                        and 0 <= remaining <= total):
                    progress = 100. * (1. - remaining / total)
                results[identifier] = RemoteDownload(identifier, state,
                    status if state != S.REMOTE_UNKNOWN else 'Unrecognized', self.config.category,
                    progress=progress, storage=storage, error=E.REMOTE_FAILED if state == S.FAILED else None)
            if len(results) == len(wanted):
                break
        for identifier in ids:
            results.setdefault(identifier, RemoteDownload(identifier, S.REMOTE_UNKNOWN, 'Missing', error=E.MISSING))
        return results

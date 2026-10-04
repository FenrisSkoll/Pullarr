"""Focused token-only Metron transport. No implicit retries or CV fallback."""

import json
from email.utils import parsedate_to_datetime
from threading import RLock
from time import monotonic, sleep, time
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urljoin, urlsplit

from requests import RequestException, Session
from requests.utils import get_environ_proxies

from backend.implementations.metadata.errors import MetadataProviderError

API_ROOT = 'https://metron.cloud/api/'
RATE_STATE: Dict[str, float] = {}
REQUEST_LOCK = RLock()


class MetronError(MetadataProviderError):
    """Only controlled reason codes, never response bodies or credentials."""

    def __init__(self, reason: str, retry_at: Optional[float] = None):
        super().__init__('metron', reason, retry_at)


class MetronClient:
    def __init__(self, token: Optional[str] = None,
                 before_request: Optional[Callable[[], None]] = None):
        if token is None:
            from backend.internals.settings import Settings
            token = Settings().sv.metron_api_token
        self._token = token
        self.before_request = before_request
        self.request_count = 0

    @staticmethod
    def api_url(path: str) -> str:
        url = urljoin(API_ROOT, path)
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.netloc != 'metron.cloud'
                or not parts.path.startswith('/api/') or parts.fragment
                or '..' in parts.path or '%' in parts.path):
            raise MetronError('malformed')
        return url

    @staticmethod
    def observe(headers: Mapping[str, str]) -> None:
        headers = {key.lower(): value for key, value in headers.items()}
        for window in ('burst', 'sustained'):
            for field in ('limit', 'remaining', 'reset'):
                value = headers.get('x-ratelimit-' + window + '-' + field)
                if value is not None:
                    try:
                        number = float(value)
                        if 0 <= number < 10**12:
                            RATE_STATE[window + '_' + field] = number
                    except (ValueError, TypeError):
                        pass
            if RATE_STATE.get(window + '_remaining') == 0 and window + '_reset' not in RATE_STATE:
                RATE_STATE[window + '_reset'] = time() + 60

    @staticmethod
    def wait_for_capacity() -> None:
        now = time()
        until = RATE_STATE.get('retry_at', 0)
        if until > now:
            raise MetronError('rate_limited', until)
        for window in ('sustained', 'burst'):
            if RATE_STATE.get(window + '_remaining') != 0:
                continue
            until = RATE_STATE.get(window + '_reset', now + 60)
            delay = until - now
            if delay <= 0:
                continue
            if window == 'sustained' or delay > 60:
                raise MetronError('rate_limited', until)
            # One serialized request stream; only a short burst wait is allowed.
            sleep(delay + 0.1)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, *, bounded: bool = False) -> Dict[str, Any]:
        url = self.api_url(path)
        if not self._token.strip():
            raise MetronError('credentials')
        if any(c.isspace() for c in self._token):
            raise MetronError('credentials')
        with REQUEST_LOCK:
            if bounded and any(RATE_STATE.get(w + '_remaining') == 0 and RATE_STATE.get(w + '_reset', time() + 60) > time() for w in ('burst', 'sustained')):
                raise MetronError('rate_limited')
            self.wait_for_capacity()
            if self.before_request is not None:
                self.before_request()
            self.request_count += 1
            try:
                with Session() as session:
                    # Keep configured environment proxies, but prohibit .netrc
                    # replacing Bearer authentication. Never use FlareSolverr.
                    session.trust_env = False
                    started = monotonic()
                    response = session.get(
                        url, params=params,
                        headers={'Authorization': 'Bearer ' + self._token,
                                 'Accept': 'application/json',
                                 **({'Accept-Encoding': 'identity'} if bounded else {}),
                                 'User-Agent': 'Pullarr Metron metadata integration'},
                        proxies={} if bounded else get_environ_proxies(url),
                        timeout=(5, 5) if bounded else (10, 30), allow_redirects=False,
                        **({'stream': True} if bounded else {}))
                    self.observe(response.headers)
                    status = response.status_code
                    if status == 429:
                        if bounded:
                            response.close()
                        raw = response.headers.get('Retry-After', '60')
                        try:
                            retry_at = time() + max(1, float(raw))
                        except (ValueError, TypeError):
                            try:
                                retry_at = parsedate_to_datetime(raw).timestamp()
                            except (ValueError, TypeError, OverflowError):
                                retry_at = time() + 60
                        RATE_STATE['retry_at'] = max(time() + 1, retry_at)
                        raise MetronError('rate_limited', RATE_STATE['retry_at'])
                    if status != 200:
                        if bounded:
                            response.close()
                        raise MetronError({401: 'credentials', 403: 'forbidden',
                                           404: 'not_found'}.get(status, 'unavailable'))
                    try:
                        if bounded:
                            try:
                                if response.headers.get('Content-Encoding', 'identity').lower() not in ('identity', ''):
                                    raise MetronError('malformed')
                                content = bytearray()
                                for chunk in response.iter_content(65536):
                                    content.extend(chunk)
                                    if len(content) > 2 * 1024 * 1024 or monotonic() - started > 15:
                                        raise MetronError('response_limit')
                                data = json.loads(content)
                            finally:
                                response.close()
                        else:
                            data = response.json()
                    except ValueError:
                        raise MetronError('malformed') from None
                    if not isinstance(data, dict):
                        raise MetronError('malformed')
                    return data
            except RequestException:
                raise MetronError('unavailable') from None

    def pages(self, path: str, params: Optional[Dict[str, Any]] = None, *,
              max_pages: Optional[int] = None, max_results: Optional[int] = None) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        seen = set()
        count = None
        while path:
            if max_pages is not None and len(seen) >= max_pages:
                raise MetronError('search_limit')
            url = self.api_url(path)
            if url in seen:
                raise MetronError('malformed')
            seen.add(url)
            data = self.get(path, params)
            params = None
            if type(data.get('count')) is not int or data['count'] < 0:
                raise MetronError('malformed')
            if count is None:
                count = data['count']
            if count != data['count'] or not isinstance(data.get('results'), list):
                raise MetronError('malformed')
            if max_results is not None and (count > max_results or len(result) + len(data['results']) > max_results):
                raise MetronError('search_limit')
            if not all(isinstance(row, dict) for row in data['results']):
                raise MetronError('malformed')
            result.extend(data['results'])
            if len(result) > count:
                raise MetronError('malformed')
            next_path = data.get('next')
            if next_path is not None and not isinstance(next_path, str):
                raise MetronError('malformed')
            path = next_path or ''
        if len(result) != count:
            raise MetronError('malformed')
        return result

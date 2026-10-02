"""Bounded, origin-bound read-only GCD REST transport.

Contract: gcd-django beta 3d9d4b5516ce2a439ed8ede26787b8db28685caf.
No metadata persistence, source fallback, response cache or implicit retries.
"""

import json
import re
from time import monotonic
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from requests import RequestException, Session, Timeout

from backend.implementations.metadata.errors import MetadataProviderError

API_BASE = 'https://www.comics.org/api/'
MAX_BODY = 8 * 1024 * 1024


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON field')
        value[key] = item
    return value


class GcdError(MetadataProviderError):
    def __init__(self, reason: str, retry_at: Optional[float] = None):
        super().__init__('gcd', reason, retry_at)


def resource_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise GcdError('malformed')
    return value


class GcdClient:
    """An injected base is an internal test seam, never user configuration.

    Charge is called immediately before every attempt. Preflight does not spend
    requests; concurrent consumers can still exhaust capacity, in which case
    acquisition aborts without admitting a partial snapshot.
    """

    def __init__(self, *, username: Any = '', password: Any = '',
                 base: str = API_BASE, session: Optional[Session] = None,
                 charge: Callable[[], None], preflight: Callable[[int], None],
                 limited: Callable[[Optional[str]], None]):
        if (not isinstance(username, str) or not isinstance(password, str)
                or bool(username) != bool(password) or ':' in username
                or any(ord(c) < 32 for c in username + password)):
            raise GcdError('credentials')
        try:
            (username + password).encode('latin-1')
        except UnicodeError:
            raise GcdError('credentials') from None
        parsed = urlsplit(base)
        if (parsed.scheme not in ('http', 'https') or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path != '/api/'):
            raise ValueError('Invalid internal GCD transport base')
        self.base = base
        self.origin = (parsed.scheme, parsed.netloc)
        self.session = session or Session()
        self.session.trust_env = False  # No netrc credentials or implicit proxy.
        self.auth = (username, password) if username else None
        self.charge = charge
        self.preflight = preflight
        self.limited = limited

    def close(self) -> None:
        self.session.close()

    def identity(self, value: Any, resource: str) -> str:
        if not isinstance(value, str):
            raise GcdError('malformed')
        parsed = urlsplit(value)
        if ((parsed.scheme, parsed.netloc) != self.origin
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise GcdError('malformed')
        match = re.fullmatch('/api/' + re.escape(resource) + r'/([1-9][0-9]{0,18})/', parsed.path)
        if match is None:
            raise GcdError('malformed')
        return resource_id(match[1])

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # Only internally constructed relative paths reach transport. Returned
        # hyperlinks are parsed into identities, never fetched as URLs.
        if (path.startswith('/') or any(p in ('.', '..') for p in path.split('/')) or '\\' in path
                or '?' in path or '#' in path or not path.startswith(('series/', 'issue/', 'publisher/'))):
            raise GcdError('malformed')
        self.charge()
        deadline = monotonic() + 60
        try:
            with self.session.get(self.base + path, params=params, auth=self.auth,
                                  headers={'Accept': 'application/json',
                                           'User-Agent': 'Pullarr-GCD/1 (read-only metadata)'},
                                  timeout=(5, 20), allow_redirects=False, stream=True) as response:
                if response.status_code == 429:
                    self.limited(response.headers.get('Retry-After'))
                    raise GcdError('rate_limited')
                if response.status_code in (401, 403):
                    raise GcdError('credentials' if response.status_code == 401 else 'forbidden')
                if response.status_code == 404:
                    raise GcdError('not_found')
                if response.status_code != 200:
                    raise GcdError('unavailable')
                if response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    raise GcdError('invalid_json')
                body = bytearray()
                for chunk in response.iter_content(65536):
                    if monotonic() > deadline:
                        raise GcdError('timeout')
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise GcdError('response_limit')
                value = json.loads(body, object_pairs_hook=unique_object)
                if not isinstance(value, dict):
                    raise GcdError('malformed')
                return value
        except Timeout:
            raise GcdError('timeout') from None
        except RequestException:
            raise GcdError('unavailable') from None
        except (ValueError, UnicodeError):
            raise GcdError('invalid_json') from None

"""Narrow public HTTPS CBL transport and existing-client ordered provider input."""

import http.client
import ipaddress
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from hashlib import sha256
from threading import BoundedSemaphore
from time import monotonic
from urllib.parse import urljoin, urlsplit, urlunsplit

from backend.base.reading_orders import (MAX_BYTES, MAX_ENTRIES,
                                         ReadingOrderError, digest,
                                         entry, parse_cbl)
from backend.implementations.metadata.metron_client import MetronClient


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise ReadingOrderError('blocked_destination')
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        if parsed.scheme.lower() != 'https' or not host or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise ValueError()
        if '%' in host or host == 'localhost' or host.endswith('.localhost'):
            raise ValueError()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            public_addresses([str(literal)])
        # Queries may contain signed credentials. Private/signed feeds are v2.
        if parsed.query:
            raise ValueError()
        netloc = '[' + host + ']' if ':' in host else host
        return urlunsplit(('https', netloc, parsed.path or '/', '', ''))
    except (ValueError, UnicodeError):
        raise ReadingOrderError('blocked_destination') from None


def public_addresses(addresses):
    result = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ReadingOrderError('blocked_destination') from None
        if (not address.is_global or address.is_multicast or address.is_reserved or getattr(address, 'ipv4_mapped', None)
                or getattr(address, 'sixtofour', None) or getattr(address, 'teredo', None)):
            raise ReadingOrderError('blocked_destination')
        result.append(str(address))
    if not result:
        raise ReadingOrderError('blocked_destination')
    return tuple(sorted(set(result)))


def resolve(host):
    return [r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        family = socket.AF_INET6 if ':' in self.address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.address, 443))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class CBLFetcher:
    """Owned by the application. Test seams cannot be selected through HTTP."""
    def __init__(self, resolver=resolve, connection=PinnedHTTPS, clock=monotonic):
        self.resolver, self.connection, self.clock = resolver, connection, clock
        self.dns = ThreadPoolExecutor(max_workers=2, thread_name_prefix='cbl-dns')
        self.slots = BoundedSemaphore(2)

    def addresses(self, host, timeout):
        if not self.slots.acquire(blocking=False):
            raise ReadingOrderError('source_busy')
        future = self.dns.submit(self.resolver, host)
        future.add_done_callback(lambda f: self.slots.release())
        return public_addresses(future.result(timeout=max(.01, timeout)))

    def fetch(self, value, etag=None, modified=None):
        url, deadline = normalize_url(value), self.clock() + 30
        headers = {'Accept': 'application/xml, text/xml, application/octet-stream',
            'Accept-Encoding': 'identity', 'User-Agent': 'Pullarr CBL subscriptions'}
        for key, val in (('If-None-Match', etag), ('If-Modified-Since', modified)):
            if val and len(val) <= 500 and not any(ord(c) < 32 or ord(c) == 127 for c in val):
                headers[key] = val
        try:
            for hop in range(4):
                parsed = urlsplit(url)
                addresses = self.addresses(parsed.hostname, min(5, deadline - self.clock()))
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ReadingOrderError('source_timeout')
                conn = self.connection(parsed.hostname, addresses[0], min(10, remaining))
                try:
                    conn.request('GET', parsed.path or '/', headers=headers)
                    response = conn.getresponse()
                    if response.status in (301, 302, 303, 307, 308):
                        if hop == 3:
                            raise ReadingOrderError('redirect_limit')
                        url = normalize_url(urljoin(url, response.getheader('Location', '')))
                        # Do not forward validators to another resource.
                        headers.pop('If-None-Match', None)
                        headers.pop('If-Modified-Since', None)
                        continue
                    if response.status == 304:
                        if not (etag or modified):
                            raise ReadingOrderError('source_http_error')
                        return dict(unchanged=True)
                    if response.status != 200:
                        raise ReadingOrderError('source_http_error')
                    if response.getheader('Content-Encoding', 'identity').lower() not in ('identity', ''):
                        raise ReadingOrderError('unsupported_encoding')
                    mime = response.getheader('Content-Type', '').split(';')[0].lower().strip()
                    if mime not in ('application/xml', 'text/xml', 'text/plain', 'application/octet-stream', 'application/x-cbl', 'application/cbl+xml', ''):
                        raise ReadingOrderError('unsupported_cbl')
                    length = response.getheader('Content-Length')
                    if length and (not length.isdecimal() or int(length) > MAX_BYTES):
                        raise ReadingOrderError('bounded')
                    data = bytearray()
                    while True:
                        remaining = deadline - self.clock()
                        if remaining <= 0:
                            raise ReadingOrderError('source_timeout')
                        if conn.sock is not None:
                            conn.sock.settimeout(min(5, remaining))
                        chunk = response.read1(min(65536, MAX_BYTES + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ReadingOrderError('bounded')
                    model = parse_cbl(bytes(data))
                    def validator(name):
                        raw = response.getheader(name, '')
                        return raw if len(raw) <= 500 and not any(ord(c) < 32 for c in raw) else None
                    return dict(unchanged=False, model=model, digest=sha256(data).hexdigest(),
                        etag=validator('ETag'), last_modified=validator('Last-Modified'))
                finally:
                    conn.close()
        except (TimeoutError, socket.timeout):
            raise ReadingOrderError('source_timeout') from None
        except (OSError, http.client.HTTPException):
            raise ReadingOrderError('source_unavailable') from None
        raise ReadingOrderError('redirect_limit')


class MetronLists:
    """Only the documented ordered items contract; no invented arc chronology."""
    def search(self, query):
        values = MetronClient().pages('reading_list/', {'name': query, 'is_private': 'false'}, max_pages=2, max_results=100)
        return [dict(id=str(v['id']), title=str(v['name'])[:500], provider='metron') for v in values]

    def fetch(self, identity):
        client = MetronClient()
        detail = client.get('reading_list/' + identity + '/')
        values = client.pages('reading_list/' + identity + '/items/', max_pages=40, max_results=MAX_ENTRIES)
        if not values or any(type(v.get('order')) is not int for v in values) or len({v['order'] for v in values}) != len(values):
            raise ReadingOrderError('unsupported_source_order')
        books = []
        for value in sorted(values, key=lambda v: v['order']):
            issue = value['issue']
            series = issue['series']
            books.append(entry(str(series['name']), str(issue['number']), volume=str(series.get('volume') or ''),
                refs=[dict(provider='metron', issue_id=str(issue['id']), volume_id=str(series['id']))]))
        model = dict(title=str(detail['name'])[:500], description='', entries=books, warnings=[])
        return dict(unchanged=False, model=model, digest=digest(model), etag=None, last_modified=None)

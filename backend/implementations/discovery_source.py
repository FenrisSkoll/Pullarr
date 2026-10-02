"""Fixed-source public HTTPS observations; no FlareSolverr/cookies/proxies."""

import http.client
import socket
from concurrent.futures import TimeoutError
from email.utils import parsedate_to_datetime
from threading import Timer
from time import time
from urllib.parse import urljoin, urlsplit

from backend.base.discovery import MAX_BYTES, DiscoveryError, source_url
from backend.base.reading_orders import ReadingOrderError
from backend.implementations.reading_order_sources import CBLFetcher


class DiscoveryHTTP(CBLFetcher):
    """Reuse pinned public DNS/TLS infrastructure, not the CBL source protocol."""
    def get(self, url, etag=None, modified=None):
        url = source_url(url)
        deadline = self.clock() + 25
        headers = {'User-Agent': 'Pullarr Discover', 'Accept-Encoding': 'identity',
                   'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/html'}
        for key, value in (('If-None-Match', etag), ('If-Modified-Since', modified)):
            if value and len(value) <= 500 and not any(ord(c) < 32 or ord(c) == 127 for c in value):
                headers[key] = value
        try:
            for hop in range(4):
                parsed = urlsplit(url)
                addresses = self.addresses(parsed.hostname, min(5, deadline-self.clock()))
                remaining = deadline-self.clock()
                if remaining <= 0:
                    raise DiscoveryError('source_timeout')
                connection = self.connection(parsed.hostname, addresses[0], min(5, remaining))
                def abort():
                    if connection.sock is not None:
                        try:
                            connection.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    connection.close()
                watchdog = Timer(remaining, abort)
                watchdog.daemon = True
                watchdog.start()
                try:
                    connection.request('GET', parsed.path, headers=headers)
                    response = connection.getresponse()
                    if response.status in (301, 302, 303, 307, 308):
                        if hop == 3:
                            raise DiscoveryError('redirect_limit')
                        url = source_url(urljoin(url, response.getheader('Location', '')))
                        headers.pop('If-None-Match', None)
                        headers.pop('If-Modified-Since', None)
                        continue
                    if response.status == 304:
                        if not (etag or modified):
                            raise DiscoveryError('source_http_error')
                        return dict(unchanged=True)
                    if response.status == 429:
                        value = response.getheader('Retry-After', '')
                        try:
                            delay = int(value) if value.isdecimal() else int(parsedate_to_datetime(value).timestamp()-time())
                        except (ValueError, TypeError, OverflowError):
                            delay = 3600
                        raise DiscoveryError('rate_limited', delay)
                    if response.status != 200:
                        if response.status in (401,403):
                            raise DiscoveryError('source_access_blocked')
                        raise DiscoveryError('source_unavailable')
                    if response.getheader('Content-Encoding', 'identity').lower() not in ('identity', ''):
                        raise DiscoveryError('unsupported_encoding')
                    mime = response.getheader('Content-Type', '').split(';')[0].lower().strip()
                    if mime not in ('application/rss+xml', 'application/atom+xml', 'application/xml', 'text/xml', 'text/html', 'application/xhtml+xml'):
                        raise DiscoveryError('invalid_content_type')
                    length = response.getheader('Content-Length')
                    if length and (not length.isdecimal() or int(length) > MAX_BYTES):
                        raise DiscoveryError('oversized_response')
                    data = bytearray()
                    while True:
                        remaining = deadline-self.clock()
                        if remaining <= 0:
                            raise DiscoveryError('source_timeout')
                        if connection.sock is not None:
                            connection.sock.settimeout(min(5, remaining))
                        chunk = response.read1(min(65536, MAX_BYTES+1-len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise DiscoveryError('oversized_response')
                    def validator(name):
                        value = response.getheader(name, '')
                        return value if len(value) <= 500 and not any(ord(c) < 32 or ord(c) == 127 for c in value) else None
                    return dict(unchanged=False, data=bytes(data), mime=mime, etag=validator('ETag'), last_modified=validator('Last-Modified'))
                finally:
                    watchdog.cancel()
                    connection.close()
        except (TimeoutError, socket.timeout):
            raise DiscoveryError('source_timeout') from None
        except ReadingOrderError as error:
            raise DiscoveryError(str(error)) from None
        except (OSError, http.client.HTTPException):
            raise DiscoveryError('network_unavailable') from None
        raise DiscoveryError('redirect_limit')

    def fetch(self, base, url):
        """Existing GetComics offering parser's HTTP seam, still fixed-source."""
        source_url(base)
        return self.get(url)['data']

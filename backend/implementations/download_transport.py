"""Bounded, non-retrying HTTP for private resolution and SAB operations.

No proxy/netrc/cookie inheritance, automatic redirects or request logging.
An upload failure is conservatively ambiguous once the request is attempted.
"""

from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from time import monotonic
from urllib.parse import urlsplit

from backend.base.download_job import DownloadErrorCode as E, DownloadFailure
from backend.implementations.configured_network import (ReadDeadline,
                                                        bounded_socket)


@dataclass(frozen=True)
class PrivateResponse:
    status: int
    body: bytes = field(repr=False)
    location: str = field(default='', repr=False)
    cookie: str = field(default='', repr=False)


class HTTPStatusFailure(DownloadFailure):
    """Unexpected HTTP status, without retaining a remote body or headers."""
    def __init__(self, status: int):
        super().__init__(E.UNAVAILABLE)
        self.status = status


class DownloadHTTP:
    def __init__(self, connect_timeout: float = 5, read_timeout: float = 10):
        if not 0 < connect_timeout <= 10 or not 0 < read_timeout <= 30:
            raise DownloadFailure(E.CONFIGURATION)
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout

    def request(self, url: str, *, method: str = 'GET', headers=None,
                body: bytes = b'', maximum: int = 1024 * 1024,
                success_statuses: tuple[int, ...] = (200,)) -> PrivateResponse:
        if (not success_statuses or any(type(s) is not int or not 200 <= s < 300
                                       for s in success_statuses)):
            raise DownloadFailure(E.CONFIGURATION)
        try:
            p = urlsplit(url)
            if (p.scheme not in ('http', 'https') or not p.hostname or p.username
                    or p.password or p.fragment or len(url) > 8192
                    or any(ord(c) < 33 or c == '\\' for c in url)):
                raise DownloadFailure(E.CONFIGURATION)
            port = p.port
        except ValueError:
            raise DownloadFailure(E.CONFIGURATION) from None
        connection = (HTTPSConnection if p.scheme == 'https' else HTTPConnection)(
            p.hostname, port=port, timeout=self.connect_timeout)
        connection._create_connection = bounded_socket
        guard = None
        try:
            connection.connect()
            if connection.sock is None:
                raise DownloadFailure(E.UNAVAILABLE)
            guard = ReadDeadline(connection.sock, self.read_timeout)
            connection.sock.settimeout(self.read_timeout)
            connection.request(method, (p.path or '/') + ('?' + p.query if p.query else ''), body=body,
                headers={'Accept-Encoding': 'identity', 'User-Agent': 'Pullarr', **(headers or {})})
            response = connection.getresponse()
            if guard.expired.is_set():
                raise DownloadFailure(E.TIMEOUT)
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location', '')
                if len(location) > 4096:
                    raise DownloadFailure(E.REDIRECT)
                return PrivateResponse(response.status, b'', location)
            if response.status in (401, 403):
                raise DownloadFailure(E.AUTHENTICATION)
            if response.status not in success_statuses:
                raise HTTPStatusFailure(response.status)
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise DownloadFailure(E.INVALID_RESPONSE)
            length = response.getheader('Content-Length')
            if length is not None and (not length.isascii() or not length.isdecimal()
                                        or len(length) > 12 or int(length) > maximum):
                raise DownloadFailure(E.LIMIT)
            output = bytearray()
            deadline = monotonic() + self.read_timeout
            while True:
                if monotonic() >= deadline:
                    raise DownloadFailure(E.TIMEOUT)
                if connection.sock is not None:
                    connection.sock.settimeout(max(.001, deadline - monotonic()))
                chunk = response.read1(min(65536, maximum + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > maximum:
                    raise DownloadFailure(E.LIMIT)
            if length is not None and len(output) != int(length):
                raise DownloadFailure(E.INVALID_RESPONSE)
            cookie = response.getheader('Set-Cookie', '')
            if len(cookie) > 4096:
                raise DownloadFailure(E.INVALID_RESPONSE)
            if guard.expired.is_set():
                raise DownloadFailure(E.TIMEOUT)
            return PrivateResponse(response.status, bytes(output), cookie=cookie)
        except TimeoutError:
            raise DownloadFailure(E.TIMEOUT) from None
        except (OSError, HTTPException, ValueError, UnicodeError):
            raise DownloadFailure(E.TIMEOUT if guard and guard.expired.is_set() else E.UNAVAILABLE) from None
        finally:
            if guard:
                guard.cancel()
            connection.close()

"""Credential-free, pinned public HTTPS images, decoded and reduced before display."""

import http.client
import re
from concurrent.futures import TimeoutError
from io import BytesIO
from threading import Timer
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from backend.base.reading_orders import ReadingOrderError
from backend.implementations.reading_order_sources import CBLFetcher

MAX_BYTES = 2 * 1024 * 1024
MAX_THUMBNAIL = 65536


def artwork_url(provider, value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise ValueError('Invalid artwork URL')
    parts = urlsplit(value)
    if parts.scheme != 'https' or parts.query or parts.fragment or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError('Invalid artwork URL')
    if provider == 'gcd':
        valid = parts.netloc in ('files1.comics.org', 'images.comics.org') and re.fullmatch(r'/{1,2}img/gcd/covers_by_id/[0-9]+/w400/[0-9]+\.jpg', parts.path)
    elif provider == 'metron':
        valid = parts.netloc in ('static.metron.cloud', 'metron.cloud') and re.fullmatch(r'/media/issue/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp)', parts.path, re.I)
    else:
        valid = False
    if not valid or '..' in parts.path:
        raise ValueError('Invalid artwork URL')
    return value


def thumbnail(data):
    if len(data) > MAX_BYTES:
        raise ValueError('Oversized image')
    try:
        with Image.open(BytesIO(data)) as picture:
            if picture.format not in ('JPEG', 'PNG', 'WEBP') or picture.width * picture.height > 16000000:
                raise ValueError('Unsupported image')
            picture.verify()
        with Image.open(BytesIO(data)) as picture:
            picture.thumbnail((240, 360))
            output = BytesIO()
            picture.convert('RGB').save(output, 'JPEG', quality=75)
        result = output.getvalue()
        if len(result) > MAX_THUMBNAIL:
            raise ValueError('Oversized thumbnail')
        return result
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError('Invalid image') from None


class ArtworkHTTP(CBLFetcher):
    def fetch_image(self, provider, value):
        parsed = urlsplit(artwork_url(provider, value))
        deadline = self.clock() + 15
        conn = None
        timer = None
        try:
            addresses = self.addresses(parsed.hostname, 3)
            conn = self.connection(parsed.hostname, addresses[0], min(5, deadline - self.clock()))
            timer = Timer(max(.01, deadline - self.clock()), conn.close)
            timer.daemon = True
            timer.start()
            conn.request('GET', parsed.path, headers={'Accept': 'image/jpeg,image/png,image/webp',
                'Accept-Encoding': 'identity', 'User-Agent': 'Pullarr search artwork'})
            response = conn.getresponse()
            if response.status != 200 or response.getheader('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise ValueError('Artwork unavailable')
            if response.getheader('Content-Type', '').split(';')[0].strip().lower() not in ('image/jpeg', 'image/png', 'image/webp'):
                raise ValueError('Unsupported image')
            length = response.getheader('Content-Length')
            if length and (not length.isdecimal() or int(length) > MAX_BYTES):
                raise ValueError('Oversized image')
            data = bytearray()
            while True:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ValueError('Artwork timeout')
                if conn.sock:
                    conn.sock.settimeout(min(3, remaining))
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError('Oversized image')
            return thumbnail(bytes(data))
        except (OSError, TimeoutError, http.client.HTTPException, ReadingOrderError):
            raise ValueError('Artwork unavailable') from None
        finally:
            if timer:
                timer.cancel()
            if conn:
                conn.close()

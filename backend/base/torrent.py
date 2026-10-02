"""Bounded torrent identity evidence. Never truncate a v2 hash to invent identity."""
import base64
import re
from dataclasses import dataclass, field
from hashlib import sha1, sha256
from urllib.parse import parse_qsl, urlsplit

from backend.base.download_job import DownloadErrorCode as E, DownloadFailure

MAX_TORRENT = 4 * 1024 * 1024


@dataclass(frozen=True)
class TorrentIdentity:
    v1: str | None = None
    v2: str | None = None

    def __post_init__(self):
        if not (self.v1 or self.v2) or any(value is not None and
                (not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(size) + '}', value))
                for value, size in ((self.v1, 40), (self.v2, 64))):
            raise DownloadFailure(E.INVALID_RESPONSE)

    @property
    def key(self):
        return 'btih:' + self.v1 if self.v1 else 'btmh:1220' + (self.v2 or '')


def magnet_identity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) < 32 for c in value):
        raise DownloadFailure(E.INVALID_RESPONSE)
    parts = urlsplit(value)
    if parts.scheme != 'magnet' or parts.netloc or parts.path or parts.fragment:
        raise DownloadFailure(E.INVALID_RESPONSE)
    hashes = {'v1': set(), 'v2': set()}
    try:
        for key, text in parse_qsl(parts.query, max_num_fields=64):
            if key != 'xt':
                continue
            if text.lower().startswith('urn:btih:'):
                raw = text[9:]
                if re.fullmatch('[A-Fa-f0-9]{40}', raw):
                    hashes['v1'].add(raw.lower())
                elif re.fullmatch('[A-Za-z2-7]{32}', raw):
                    hashes['v1'].add(base64.b32decode(raw.upper()).hex())
                else:
                    raise ValueError
            elif text.lower().startswith('urn:btmh:1220') and re.fullmatch('[A-Fa-f0-9]{64}', text[13:]):
                hashes['v2'].add(text[13:].lower())
            else:
                raise ValueError
        if any(len(values) > 1 for values in hashes.values()):
            raise ValueError
        return TorrentIdentity(next(iter(hashes['v1']), None), next(iter(hashes['v2']), None))
    except (ValueError, TypeError):
        raise DownloadFailure(E.INVALID_RESPONSE) from None


def torrent_identity(data):
    """Hash the exact encoded info slice, with strict canonical bencode validation."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_TORRENT:
        raise DownloadFailure(E.LIMIT)
    position = count = 0
    info_slice = None

    def read(depth=0):
        nonlocal position, count, info_slice
        count += 1
        if depth > 32 or count > 100000 or position >= len(data):
            raise ValueError
        token = data[position:position + 1]
        if token == b'i':
            end = data.index(b'e', position)
            raw = data[position + 1:end]
            if len(raw) > 20 or not re.fullmatch(rb'0|-?[1-9][0-9]*', raw):
                raise ValueError
            position = end + 1
            return int(raw)
        if token in (b'l', b'd'):
            position += 1
            values = {} if token == b'd' else []
            previous = None
            while data[position:position + 1] != b'e':
                if token == b'd':
                    key = read(depth + 1)
                    if not isinstance(key, bytes) or previous is not None and key <= previous:
                        raise ValueError
                    previous = key
                    start = position
                    values[key] = read(depth + 1)
                    if depth == 0 and key == b'info':
                        info_slice = data[start:position]
                else:
                    values.append(read(depth + 1))
            position += 1
            return values
        end = data.index(b':', position)
        raw = data[position:end]
        if len(raw) > 8 or not re.fullmatch(rb'0|[1-9][0-9]*', raw):
            raise ValueError
        size = int(raw)
        position = end + 1
        if position + size > len(data):
            raise ValueError
        value = data[position:position + size]
        position += size
        return value

    try:
        root = read()
        info = root[b'info']
        if (position != len(data) or not isinstance(info, dict) or not info_slice
                or not isinstance(info.get(b'name'), bytes) or not info[b'name']
                or type(info.get(b'piece length')) is not int or info[b'piece length'] <= 0):
            raise ValueError
        v1 = isinstance(info.get(b'pieces'), bytes) and len(info[b'pieces']) > 0 and len(info[b'pieces']) % 20 == 0
        v2 = info.get(b'meta version') == 2 and isinstance(info.get(b'file tree'), dict) and bool(info[b'file tree'])
        from backend.base.acquisition_intake import IntakeFailure
        from backend.implementations.acquisition_preparation import _member

        def name(value):
            if not isinstance(value, bytes) or len(value) > 1024:
                raise ValueError
            text = value.decode('utf-8')
            if '/' in text or '\\' in text:
                raise ValueError
            try:
                return _member(text)
            except IntakeFailure:
                raise ValueError from None

        name(info[b'name'])
        if b'files' in info:
            entries = info[b'files']
            if not isinstance(entries, list) or not 0 < len(entries) <= 1000:
                raise ValueError
            seen = set()
            for entry in entries:
                if (not isinstance(entry, dict) or type(entry.get(b'length')) is not int or entry[b'length'] < 0
                        or not isinstance(entry.get(b'path'), list) or not 0 < len(entry[b'path']) <= 32):
                    raise ValueError
                path = '/'.join(name(part) for part in entry[b'path']).casefold()
                if path in seen:
                    raise ValueError
                seen.add(path)
        if v2:
            files = 0
            def tree(node, depth=0):
                nonlocal files
                if not isinstance(node, dict) or not node or depth > 32:
                    raise ValueError
                if b'' in node:
                    leaf = node[b'']
                    if len(node) != 1 or not isinstance(leaf, dict) or type(leaf.get(b'length')) is not int or leaf[b'length'] < 0:
                        raise ValueError
                    files += 1
                    if files > 1000:
                        raise ValueError
                else:
                    names = [name(key).casefold() for key in node]
                    if len(set(names)) != len(names):
                        raise ValueError
                    for child in node.values():
                        tree(child, depth + 1)
            tree(info[b'file tree'])
        if v1 and not (type(info.get(b'length')) is int and info[b'length'] >= 0 or isinstance(info.get(b'files'), list)):
            raise ValueError
        return TorrentIdentity(sha1(info_slice).hexdigest() if v1 else None, sha256(info_slice).hexdigest() if v2 else None)
    except (ValueError, KeyError, TypeError, IndexError, RecursionError, UnicodeError):
        raise DownloadFailure(E.INVALID_RESPONSE) from None


@dataclass(frozen=True)
class ResolvedTorrent:
    candidate_id: str
    source_key: str
    data: bytes = field(repr=False)
    identity: TorrentIdentity
    magnet: str = field(default='', repr=False)

    @property
    def digest(self):
        return sha256(self.data or self.magnet.encode()).hexdigest()

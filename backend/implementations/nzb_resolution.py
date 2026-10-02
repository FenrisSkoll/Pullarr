"""Exact selected-source resolution. Private locators never leave this module."""

from hashlib import sha256
from time import monotonic
from urllib.parse import (parse_qsl, unquote, urlencode,
                          urljoin, urlsplit, urlunsplit)
from xml.etree.ElementTree import (ParseError, TreeBuilder,
                                   XMLParser, fromstring)

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, ResolvedNZB, endpoint)
from backend.base.release_candidate import (AcquisitionMechanism,
                                            ReleaseCandidate)
from backend.base.release_search import SourceFailure
from backend.base.torrent import (MAX_TORRENT, ResolvedTorrent,
                                  magnet_identity, torrent_identity)
from backend.implementations.download_transport import DownloadHTTP
from backend.implementations.newznab import NewznabSource

MAX_NZB_BYTES = 16 * 1024 * 1024
NZB_NAMESPACE = 'http://www.newzbin.com/DTD/2003/nzb'


class NZBTree(TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = self.count = 0

    def doctype(self, name, pubid, system):
        raise DownloadFailure(E.INVALID_NZB)

    def start(self, tag, attrs):
        self.depth += 1
        self.count += 1
        if self.depth > 12 or self.count > 200000 or len(attrs) > 16:
            raise DownloadFailure(E.INVALID_NZB)
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


def validate_nzb(data) -> str:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_NZB_BYTES:
        raise DownloadFailure(E.INVALID_NZB)
    try:
        root = fromstring(data, parser=XMLParser(target=NZBTree()))
    except (ParseError, ValueError):
        raise DownloadFailure(E.INVALID_NZB) from None
    namespace = '{' + NZB_NAMESPACE + '}' if root.tag.startswith('{') else ''
    if root.tag != namespace + 'nzb':
        raise DownloadFailure(E.INVALID_NZB)
    useful = False
    for file in root.findall(namespace + 'file'):
        groups, segments = file.find(namespace + 'groups'), file.find(namespace + 'segments')
        if groups is None or segments is None or not file.get('subject'):
            raise DownloadFailure(E.INVALID_NZB)
        if not any((g.text or '').strip() for g in groups.findall(namespace + 'group')):
            raise DownloadFailure(E.INVALID_NZB)
        found = False
        for segment in segments.findall(namespace + 'segment'):
            for attribute in ('bytes', 'number'):
                value = segment.get(attribute, '')
                if not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) <= 0:
                    raise DownloadFailure(E.INVALID_NZB)
            if not (segment.text or '').strip():
                raise DownloadFailure(E.INVALID_NZB)
            found = useful = True
        if not found:
            raise DownloadFailure(E.INVALID_NZB)
    if not useful:
        raise DownloadFailure(E.INVALID_NZB)
    return sha256(data).hexdigest()


def origin(url: str) -> tuple:
    try:
        p = urlsplit(url)
        return p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)
    except ValueError:
        raise DownloadFailure(E.REDIRECT) from None


class SelectedSourceSession:
    """Caller-owned, maximum 15-minute selection window; close expires source.

    Redirect origins are trusted server-side configuration, never result/client
    supplied. Default accepts only the configured origin. Cross-origin Prowlarr
    redirects require explicit allowlisting; subsequent hops remain allowlisted.
    """
    def __init__(self, source: NewznabSource, *, http=None, redirect_origins=(),
                 lifetime: float = 900, clock=monotonic):
        if not 0 < lifetime <= 900 or len(redirect_origins) > 16:
            raise DownloadFailure(E.CONFIGURATION)
        for value in redirect_origins:
            if urlsplit(endpoint(value)).path:
                raise DownloadFailure(E.CONFIGURATION)
        self.source = source
        self.http = http or DownloadHTTP()
        self.origins = frozenset(origin(v) for v in redirect_origins)
        self.clock, self.deadline = clock, clock() + lifetime
        self.closed = False

    def close(self):
        self.closed = True
        self.source.close()

    def check(self, candidate: ReleaseCandidate):
        if self.closed or self.clock() >= self.deadline:
            raise DownloadFailure(E.EXPIRED)
        try:
            return self.source.selected_reference(candidate)
        except SourceFailure:
            raise DownloadFailure(E.SELECTION) from None

    def resolve(self, candidate: ReleaseCandidate) -> ResolvedNZB | ResolvedTorrent:
        ref = self.check(candidate)
        config = self.source.config
        url = ref.url
        torrent = candidate.acquisition.mechanism == AcquisitionMechanism.TORRENT
        if torrent and url.startswith('magnet:'):
            if config.api_key and config.api_key in url:
                raise DownloadFailure(E.SELECTION)
            return ResolvedTorrent(candidate.candidate_id or '', candidate.source.key, b'', magnet_identity(url), url)
        home = origin(config.url)
        if origin(url) != home:
            raise DownloadFailure(E.SELECTION)
        if config.mode == 'prowlarr':
            # Bind the indexer route as well as the origin and exact stored URL.
            base = urlsplit(config.url).path.rstrip('/')
            indexer = self.source.suffix.split('/')[1]
            if urlsplit(url).path not in (f'{base}/{indexer}/download', f'{base}/api/v1/indexer/{indexer}/download'):
                raise DownloadFailure(E.RESOLUTION)
        else:
            p = urlsplit(url)
            params = parse_qsl(p.query, keep_blank_values=True)
            if not any(k.lower() == 'apikey' for k, _ in params):
                url = urlunsplit(p._replace(query=urlencode([*params, ('apikey', config.api_key)])))
        for hop in range(4):
            self.check(candidate)
            p = urlsplit(url)
            current = origin(url)
            if (p.username or p.password or p.fragment or p.scheme not in ('http', 'https')
                    or current not in {home, *self.origins}
                    or any(ord(c) < 33 or c == '\\' for c in url)):
                raise DownloadFailure(E.REDIRECT)
            # Credentials are only attached to the initial configured-origin request.
            headers = {'X-Api-Key': config.api_key} if hop == 0 and config.mode == 'prowlarr' else {}
            try:
                response = self.http.request(url, headers=headers, maximum=MAX_TORRENT if torrent else MAX_NZB_BYTES)
            except DownloadFailure:
                raise DownloadFailure(E.RESOLUTION) from None
            if response.location:
                try:
                    next_url = urljoin(url, response.location)
                except ValueError:
                    raise DownloadFailure(E.REDIRECT) from None
                if (hop == 3 or len(next_url) > 4096
                        or urlsplit(url).scheme == 'https' and urlsplit(next_url).scheme != 'https'
                        or origin(next_url) != home and config.api_key in unquote(next_url)):
                    raise DownloadFailure(E.REDIRECT)
                url = next_url
                continue
            if response.status != 200:
                raise DownloadFailure(E.REDIRECT)
            # Refuse known credential echoes even inside otherwise valid XML.
            if config.api_key and config.api_key.encode() in response.body:
                raise DownloadFailure(E.INVALID_NZB)
            if torrent:
                identity = torrent_identity(response.body)
                claimed = dict(candidate.torrent_facts)
                if (claimed.get('infohash_v1') and claimed['infohash_v1'] != identity.v1
                        or claimed.get('infohash_v2') and claimed['infohash_v2'] != identity.v2):
                    raise DownloadFailure(E.SELECTION)
                return ResolvedTorrent(candidate.candidate_id or '', candidate.source.key, response.body, identity)
            digest = validate_nzb(response.body)
            return ResolvedNZB(candidate.candidate_id or '', candidate.source.key,
                               response.body, digest, 'release.nzb')
        raise DownloadFailure(E.REDIRECT)

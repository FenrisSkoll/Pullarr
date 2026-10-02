"""GetComics acquisition facts only. No target refinement or release selection."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from json import dumps, loads
from time import monotonic
from typing import Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from backend.base.definitions import GCDownloadService
from backend.base.download_job import DownloadErrorCode, DownloadFailure
from backend.base.release_candidate import ReleaseCandidate
from backend.implementations.download_transport import DownloadHTTP
from backend.implementations.indexer_clients.ddl.GetComics import \
    GetComicsIndexer
from backend.implementations.release_candidates import adapt_ddl_result

MAX_HTML = 4 * 1024 * 1024


class DDLError(Exception):
    """Safe machine code, never remote text/URL/exception payload."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def public_url(url: str, *, allow_fragment=False):
    try:
        p = urlsplit(url)
        if (p.scheme not in ('http', 'https') or not p.hostname or p.username
                or p.password or (p.fragment and not allow_fragment) or len(url) > 4096
                or any(ord(c) < 33 or c == '\\' for c in url)
                or any(k.lower() in ('apikey', 'api_key', 'token', 'auth', 'key', 'password')
                       for k in parse_qs(p.query))):
            raise ValueError
        p.port
        return p
    except (ValueError, TypeError):
        raise DDLError('unsafe_url') from None


def origin(url: str):
    p = public_url(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)


def page_url(base: str, url: str) -> str:
    if not url or any(ord(c) < 33 or c == '\\' for c in url):
        raise DDLError('unsafe_url')
    resolved = urljoin(base.rstrip('/') + '/', url)
    if origin(base) != origin(resolved):
        raise DDLError('source_mismatch')
    return resolved


@dataclass(frozen=True)
class DDLSourceConfig:
    id: int
    name: str
    url: str
    services: Tuple[str, ...]
    avoid_large: bool = False

    def __post_init__(self):
        public_url(self.url)
        if self.id <= 0 or not self.name.strip() or urlsplit(self.url).query:
            raise DDLError('configuration')
        if set(self.services) != {s.value for s in GCDownloadService}:
            raise DDLError('configuration')

    @property
    def identity(self):
        # Display-name edits are harmless; endpoint changes invalidate selections.
        return sha256(repr((self.id, self.url, self.services, self.avoid_large)).encode()).hexdigest()


class DDLPageHTTP:
    """Bounded public HTML, same-origin redirects, existing configured CF solver.

    The legacy helper eagerly reads/logs error bodies and follows unrestricted
    redirects, so it is not used for the selected-page authority boundary.
    """

    def __init__(self, transport=None, solver=None):
        self.transport = transport or DownloadHTTP()
        self.solver = solver or bounded_cf_solution

    def fetch(self, base: str, url: str) -> str:
        from backend.implementations.flaresolverr import FSCache

        current = page_url(base, url)
        for _ in range(4):
            ua, cookie = FSCache.get_ua_cookies(current)
            headers = {'User-Agent': ua}
            if cookie:
                headers['Cookie'] = 'cf_clearance=' + cookie.split(';')[0]
            try:
                response = self.transport.request(current, headers=headers, maximum=MAX_HTML)
            except DownloadFailure as error:
                if error.code == DownloadErrorCode.AUTHENTICATION:
                    solved = self.solver(base, current)
                    if solved is not None:
                        return solved
                raise DDLError('source_' + error.code.value) from None
            if response.status == 200:
                if len(response.body) > MAX_HTML:
                    raise DDLError('response_limit')
                return response.body.decode('utf-8', errors='replace')
            current = page_url(base, urljoin(current, response.location))
        raise DDLError('redirect_limit')


def bounded_cf_solution(base: str, url: str):
    """Same configured FlareSolverr protocol, bounded response and final origin.

    The explicitly configured browser helper owns its navigation. Its response
    is not trusted as a new page authority, and no source credentials are sent.
    """
    from backend.base.definitions import Constants
    from backend.implementations.flaresolverr import FlareSolverr

    solver = FlareSolverr()
    if not solver.base_url:
        return None
    endpoint = solver.base_url.rstrip('/') + Constants.FS_API_BASE
    public_url(endpoint)
    try:
        response = DownloadHTTP(read_timeout=30).request(endpoint, method='POST',
            headers={'Content-Type': 'application/json'}, maximum=MAX_HTML * 4,
            body=dumps({'cmd': 'request.get', 'url': page_url(base, url),
                        'maxTimeout': 20000, **(solver.proxy_data or {})}).encode())
        if response.status != 200:
            raise DDLError('solver_unavailable')
        solution = loads(response.body)['solution']
        page_url(base, solution['url'])
        html = solution['response']
        if solution['status'] != 200 or not isinstance(html, str) or len(html.encode()) > MAX_HTML:
            raise DDLError('solver_invalid_response')
        return html
    except (DownloadFailure, ValueError, KeyError, TypeError):
        raise DDLError('solver_unavailable') from None


class GetComicsSource:
    """Native HTML pages, shared literal query requests; no fictitious NZB caps.

    Private raw records live only for the bounded selection operation.
    """

    def __init__(self, config: DDLSourceConfig, http=None):
        self.config = config
        self.http = http or DDLPageHTTP()
        self.records = {}

    def search(self, query: str, page: int):
        url = self.config.url.rstrip('/') + '/page/' + str(page) + '?' + urlencode({'s': query})
        soup = BeautifulSoup(self.http.fetch(self.config.url, url), 'html.parser')
        # Reuse the actual source's raw article extraction, not target-refining search.
        articles = GetComicsIndexer.extract_articles(soup)
        candidates, invalid = [], 0
        for link, title, size in articles[:200]:
            try:
                record = {'link': page_url(self.config.url, link), 'display_title': title,
                          'size': size, 'indexer_id': self.config.id, 'indexer_title': self.config.name}
                candidate = adapt_ddl_result(record)
            except (DDLError, ValueError):
                invalid += 1
                continue
            previous = self.records.get(candidate.candidate_id)
            if previous is not None and previous != record:
                raise DDLError('source_result_changed')
            self.records[candidate.candidate_id] = record
            candidates.append(candidate)
        links = soup.select('.page-numbers')
        more = bool(links and not (links[-1].name == 'span' and 'current' in links[-1].get('class', [])))
        return tuple(candidates), more, invalid, len(articles) > 200

    def discover(self, cutoff):
        """Bounded date-sitemap observation, not an invented RSS protocol.

        A malformed/truncated sitemap is failure, never an empty-success cursor.
        The caller retains the old cursor and retries the overlap window.
        """
        soup = BeautifulSoup(self.http.fetch(self.config.url, self.config.url.rstrip('/') + '/sitemap/'), 'html.parser')
        root = soup.select_one('.post-contents > div:first-of-type ul.lcp_catlist')
        if root is None:
            raise DDLError('discovery_parse_failure')
        entries = root.select('li')
        if len(entries) > 2000:
            raise DDLError('discovery_entry_limit')
        months = ('January February March April May June July August September October November December').split()
        candidates = {}
        for entry in entries:
            strings = list(entry.stripped_strings)
            anchor = entry.find('a')
            try:
                if len(strings) != 2 or anchor is None:
                    raise ValueError
                title, date = strings
                month, day, year = date.split()
                published = datetime(int(year), months.index(month) + 1, int(day.rstrip(',')), tzinfo=timezone.utc)
                if published.timestamp() < cutoff:
                    continue
                record = {'link': page_url(self.config.url, anchor.get('href')), 'display_title': title,
                          'size': -1, 'indexer_id': self.config.id, 'indexer_title': self.config.name}
                candidate = adapt_ddl_result(record)
            except (ValueError, TypeError, DDLError):
                raise DDLError('discovery_parse_failure') from None
            candidates[candidate.candidate_id] = candidate
            self.records[candidate.candidate_id] = record
            if len(candidates) > 200:
                raise DDLError('discovery_result_limit')
        return tuple(candidates.values())


def offering_candidate(article: ReleaseCandidate, subtitle: str, size: int, identity: str, source_year=None):
    """Subtitle is new evidence, never merged with selected title to force a match."""
    from backend.base.release_candidate import (ObservationOrigin,
                                                ReleaseObservation)
    from backend.implementations.release_candidates import normalize_release

    return normalize_release(article.source, subtitle, article.acquisition,
        result_id='offering:' + identity, size=size if size >= 0 else None,
        structured=(ReleaseObservation(ObservationOrigin.STRUCTURED, 'GetComics.page.year', year=source_year),)
        if source_year is not None else (),
        adapter='kapowarr-getcomics-offering/v1')


def resolve_mirror(service, link, validate):
    """Selected offering only; bounded validated redirects before legacy client.

    Host-specific clients retain their existing file/folder execution. This
    preliminary step does not accept an arbitrary HTML link or redirect origin.
    """
    from hashlib import sha1

    import requests
    from bencoding import bencode

    from backend.base.custom_exceptions import (
        DownloadLinkBroken, DownloadServiceRateLimitReached)
    from backend.base.definitions import (DownloadClientIdentifier as Client,
                                          DownloadService)
    from backend.base.helpers import get_torrent_info

    if not validate(link):
        raise DownloadLinkBroken('selected mirror')
    if service == GCDownloadService.GETCOMICS_TORRENT and link.startswith('magnet:'):
        return link, Client.TORRENT
    with requests.Session() as session:
        session.trust_env = False
        current = link
        try:
            for _ in range(4):
                with session.get(current, stream=True, allow_redirects=False, timeout=(5, 10)) as response:
                    if response.is_redirect:
                        location = response.headers.get('Location', '')
                        if not location or any(ord(c) < 33 for c in location):
                            break
                        next_url = urljoin(current, location)
                        if not validate(next_url) or (urlsplit(current).scheme == 'https' and urlsplit(next_url).scheme != 'https'):
                            break
                        current = next_url
                        continue
                    if response.status_code == 429:
                        raise DownloadServiceRateLimitReached(DownloadService(service.value))
                    response.raise_for_status()
                    if service == GCDownloadService.GETCOMICS_TORRENT:
                        if response.headers.get('Content-Type', '').split(';')[0] != 'application/x-bittorrent':
                            break
                        data = bytearray()
                        deadline = monotonic() + 10
                        for chunk in response.iter_content(65536):
                            data.extend(chunk)
                            if len(data) > 2 * 1024 * 1024 or monotonic() >= deadline:
                                raise DownloadLinkBroken('selected mirror')
                        digest = sha1(bencode(get_torrent_info(bytes(data)))).hexdigest()
                        return 'magnet:?xt=urn:btih:' + digest, Client.TORRENT
                    if service == GCDownloadService.MEGA:
                        return current, Client.MEGA_FOLDER if '#F!' in current or '/folder/' in current else Client.MEGA
                    if service == GCDownloadService.MEDIAFIRE:
                        if 'error.php' in current:
                            break
                        if '/folder/' in current:
                            return current, Client.MEDIAFIRE_FOLDER
                        return current, Client.DDL if urlsplit(current).hostname.startswith('download') else Client.MEDIAFIRE
                    if service == GCDownloadService.PIXELDRAIN:
                        return current, Client.PIXELDRAIN_FOLDER if '/l/' in current else Client.PIXELDRAIN
                    if service == GCDownloadService.WETRANSFER:
                        return current, Client.WETRANSFER
                    return current, Client.DDL
        except requests.RequestException:
            pass
    raise DownloadLinkBroken('selected mirror')

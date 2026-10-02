"""Torznab over shared XML/query/evaluation contracts; no second scorer."""
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

from backend.base.download_job import DownloadFailure
from backend.base.release_candidate import (AcquisitionMechanism,
                                            AcquisitionReference, LocatorKind,
                                            ObservationOrigin,
                                            ReleaseObservation, SourceKind)
from backend.base.release_search import SearchError, SearchPage, SourceFailure
from backend.base.torrent import TorrentIdentity, magnet_identity
from backend.implementations.newznab import (MAX_SEARCH_BYTES, NewznabSource,
                                             PrivateNZBReference, _integer,
                                             _text, _xml)
from backend.implementations.release_candidates import (normalize_release,
                                                        resolver_key)

NAMESPACE = 'http://torznab.com/schemas/2015/feed'
NUMBERS = ('seeders', 'leechers', 'peers', 'minimumseedtime', 'password')
DECIMALS = ('minimumratio', 'downloadvolumefactor', 'uploadvolumefactor')


def parse_torznab(data):
    root = _xml(data, MAX_SEARCH_BYTES)
    channel = root.find('channel')
    if root.tag != 'rss' or channel is None:
        raise SourceFailure(SearchError.INVALID_RESPONSE)
    items = channel.findall('item')
    if len(items) > 1000:
        raise SourceFailure(SearchError.LIMIT)
    rows, invalid = [], []
    for index, item in enumerate(items):
        try:
            title = _text(item, 'title', 16384)
            if not title or not title.strip():
                raise ValueError
            attributes = {}
            nodes = item.findall('{' + NAMESPACE + '}attr')
            if len(nodes) > 64:
                raise ValueError
            for node in nodes:
                name, value = node.get('name', ''), node.get('value', '')
                if len(name) > 64 or len(value) > 8192:
                    raise ValueError
                attributes.setdefault(name.lower(), set()).add(value)

            def single(name):
                values = attributes.get(name, set())
                if len(values) > 1:
                    raise ValueError
                return next(iter(values), None)

            enclosure = item.find('enclosure')
            link = enclosure.get('url') if enclosure is not None else _text(item, 'link', 8192)
            magnet = single('magneturl') or (link if link and link.startswith('magnet:') else None)
            identity = magnet_identity(magnet) if magnet else None
            raw_hash = single('infohash')
            if raw_hash:
                evidence = TorrentIdentity(v1=raw_hash.lower()) if len(raw_hash) == 40 else TorrentIdentity(v2=raw_hash.lower())
                if identity and (evidence.v1 and identity.v1 != evidence.v1 or evidence.v2 and identity.v2 != evidence.v2):
                    raise ValueError
                identity = identity or evidence
            facts = {}
            if identity:
                if identity.v1:
                    facts['infohash_v1'] = identity.v1
                if identity.v2:
                    facts['infohash_v2'] = identity.v2
            for name in (*NUMBERS, *DECIMALS):
                value = single(name)
                if value is None:
                    continue
                number = Decimal(value)
                if len(value) > 32 or not number.is_finite() or number < 0 or number > 10**15:
                    raise ValueError
                if name in NUMBERS and number != int(number):
                    if name != 'minimumseedtime':
                        raise ValueError
                    number = number.to_integral_value(rounding='ROUND_CEILING')
                facts[name] = str(number)
            mode = single('seedtype')
            if mode:
                facts['seedtype'] = mode if mode in ('ratio', 'seedtime', 'both', 'either') else 'unknown'
            categories = tuple(sorted({int(v) for v in attributes.get('category', ()) if v.isdecimal() and len(v) < 9}))
            tags = tuple(sorted(attributes.get('tag', ())))
            if len(tags) > 16 or any(len(t) > 128 for t in tags) or len(categories) > 16:
                raise ValueError
            size = single('size') or (enclosure.get('length') if enclosure is not None else None)
            rows.append(dict(position=index, title=title, guid=single('guid') or _text(item, 'guid'),
                locator=magnet or link, size=_integer(size), published=_text(item, 'pubDate', 128),
                categories=categories, tags=tags, facts=tuple(sorted(facts.items()))))
        except (ValueError, InvalidOperation, DownloadFailure):
            invalid.append(index)
    response = channel.find('{' + NAMESPACE + '}response')
    offset = _integer(response.get('offset')) if response is not None else None
    total = _integer(response.get('total')) if response is not None else None
    if response is not None and (offset is None or total is None):
        raise SourceFailure(SearchError.PAGINATION)
    return rows, len(items), offset, total, tuple(invalid)


class TorznabSource(NewznabSource):
    def __init__(self, config, transport, indexer_id=None, name=None):
        super().__init__(config, transport, indexer_id, name)
        self.source = replace(self.source, kind=SourceKind.TORZNAB)

    def search(self, request):
        if self._closed:
            raise SourceFailure(SearchError.CONFIGURATION)
        params = dict(t='search', q=request.query, limit=request.limit, offset=request.offset, extended=1)
        if request.categories:
            params['cat'] = ','.join(map(str, request.categories))
        rows, count, offset, total, invalid = parse_torznab(self.transport.get(self.config, self.suffix, params, MAX_SEARCH_BYTES))
        candidates, failures = [], list(invalid)
        for raw in rows:
            if self.config.api_key and any(self.config.api_key in value for value in (raw['title'], *raw['tags'])):
                failures.append(raw['position'])
                continue
            locator = raw['locator'] or ''
            bound = locator if locator.startswith('magnet:') else self._bound_locator(locator)
            if bound and self.config.api_key and bound.startswith('magnet:') and self.config.api_key in bound:
                bound = None
            identity = raw['guid'] or bound
            key = resolver_key(self.source, identity) if identity else None
            acquisition = AcquisitionReference(AcquisitionMechanism.TORRENT,
                LocatorKind.SOURCE_RECORD if bound else LocatorKind.UNAVAILABLE, key if bound else None)
            published = raw['published']
            if published:
                try:
                    published = parsedate_to_datetime(published)
                except (ValueError, TypeError, OverflowError):
                    pass
            observation = ReleaseObservation(ObservationOrigin.STRUCTURED, 'torznab.item',
                tags=raw['tags'] + tuple('torznab-category:' + str(c) for c in raw['categories']), policy='torznab/v1')
            candidate = normalize_release(self.source, raw['title'], acquisition,
                result_id='source-sha256:' + key if key else None, structured=(observation,),
                size=raw['size'], published=published, adapter='torznab/v1')
            candidate = replace(candidate, torrent_facts=raw['facts'])
            candidates.append(candidate)
            if bound and key and len(self._resolvers) < 1000:
                self._resolvers[(candidate.candidate_id, key)] = PrivateNZBReference(candidate, raw['guid'], bound)
        return SearchPage(tuple(candidates), count, offset, total, tuple(failures))

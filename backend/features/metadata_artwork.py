"""Optional search artwork: bounded tickets, identity cache and one active batch."""

from base64 import b64encode
from collections import OrderedDict
from secrets import token_hex
from threading import Lock
from time import monotonic

from backend.base.custom_exceptions import InvalidKeyValue
from backend.implementations.metadata.artwork import ArtworkHTTP
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.provider import MetadataArtworkProvider
from backend.implementations.metadata.registry import get_search_provider

MAX_BATCH = 4
MAX_ENRICHMENTS = 12
MAX_CACHE = 128  # Each JPEG is <=64 KiB; at most 8 MiB of image bytes.
POSITIVE_TTL = 86400
NEGATIVE_TTL = 600
TICKET_TTL = 600


class SearchArtwork:
    def __init__(self, clock=monotonic, fetcher=None):
        self.clock = clock
        self.fetcher = fetcher or ArtworkHTTP()
        self.cache = OrderedDict()
        self.tickets = OrderedDict()
        self.lock = Lock()
        self.active = Lock()

    def register(self, results):
        hints = {(r.provider + ':' + r.provider_id): (r.provider, r.provider_id, r.artwork_hint)
                 for r in results if r.artwork_hint and r.provider in ('gcd', 'metron')}
        if not hints:
            return None
        with self.lock:
            token = token_hex(16)
            self.tickets[token] = (self.clock() + TICKET_TTL, hints, set())
            while len(self.tickets) > 32:
                self.tickets.popitem(last=False)
        return token

    def batch(self, token, identities):
        if (not isinstance(token, str) or len(token) != 32 or not isinstance(identities, list)
                or not 1 <= len(identities) <= MAX_BATCH
                or any(not isinstance(k, str) or len(k) > 40 for k in identities)
                or len(set(identities)) != len(identities)):
            raise InvalidKeyValue('artwork', 'Invalid artwork batch')
        with self.lock:
            ticket = self.tickets.get(token)
            if not ticket or ticket[0] <= self.clock() or any(k not in ticket[1] for k in identities):
                raise InvalidKeyValue('artwork', 'Expired or invalid search')
            if len(ticket[2] | set(identities)) > MAX_ENRICHMENTS:
                raise InvalidKeyValue('artwork', 'Search artwork limit reached')
            new_keys = set(identities) - ticket[2]
            ticket[2].update(identities)
            hints = [ticket[1][k] for k in identities]
        if not self.active.acquire(blocking=False):
            return [dict(result_key=k, artwork_state='unavailable') for k in identities]
        try:
            results = []
            for key, (provider, identity, hint) in zip(identities, hints):
                cache_key = (provider, identity, hint)
                cached = self.cache.pop(cache_key, None)
                if cached and cached[0] <= self.clock():
                    cached = None
                if cached is None and key not in new_keys:
                    # Eviction or concurrent batches must not create retries
                    # beyond this ticket's twelve enrichment attempts.
                    results.append(dict(result_key=key, artwork_state='unavailable'))
                    continue
                if cached is None:
                    image = None
                    try:
                        instance = get_search_provider(provider)
                        if isinstance(instance, MetadataArtworkProvider) and not instance.search_unavailable():
                            url = instance.search_artwork_url(identity, hint)
                            if url:
                                image = self.fetcher.fetch_image(provider, url)
                    except (MetadataProviderError, ValueError):
                        pass
                    cached = (self.clock() + (POSITIVE_TTL if image else NEGATIVE_TTL), image)
                self.cache[cache_key] = cached
                while len(self.cache) > MAX_CACHE:
                    self.cache.popitem(last=False)
                row = dict(result_key=key, artwork_state='available' if cached[1] else 'unavailable')
                if cached[1]:
                    row['image'] = 'data:image/jpeg;base64,' + b64encode(cached[1]).decode('ascii')
                results.append(row)
            return results
        finally:
            self.active.release()


ARTWORK = SearchArtwork()

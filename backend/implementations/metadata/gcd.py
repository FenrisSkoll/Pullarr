"""Production GCD REST search and complete-membership snapshot adapter."""

from dataclasses import replace
from hashlib import sha256
from html import escape
from time import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

from backend.base.bibliography import MAX_SNAPSHOT_TEXT_BYTES
from backend.base.issue_facts import BibliographicDate, DateKind
from backend.implementations.metadata.gcd_bibliography import (bibliography,
                                                               publication)
from backend.implementations.metadata.gcd_budget import GcdBudget
from backend.implementations.metadata.gcd_client import (GcdClient, GcdError,
                                                         resource_id)
from backend.implementations.metadata.gcd_issue_facts import canonical_issue
from backend.implementations.metadata.gcd_staging import (GcdIssueNumber,
                                                          GcdIssueSnapshot,
                                                          GcdPartialDate,
                                                          GcdVariantRelation)
from backend.implementations.metadata.models import (VolumeMetadata,
                                                     VolumeSearchResult)
from backend.implementations.metadata.provider import (MetadataArtworkProvider,
                                                       MetadataSearchProvider)
from backend.implementations.metadata.snapshot import (
    MetadataSnapshotProvider, ProviderVolumeSnapshot,
    SnapshotIssue, SnapshotReceipt)
from backend.internals.db import get_db
from backend.internals.settings import Settings

SNAPSHOT_POLICY = 'kapowarr-gcd-membership-snapshot/v1'
MAX_ISSUES = 1997  # N + first/final series + publisher fits the daily ceiling.


def text(value: Any, *, required: bool = False) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > 100000 or required and not value.strip():
        raise GcdError('malformed')
    return value


def production_client(*, background: bool = False) -> GcdClient:
    settings = Settings().sv
    if not getattr(settings, 'gcd_enabled', False):
        raise GcdError('disabled')
    username = getattr(settings, 'gcd_username', '')
    password = getattr(settings, 'gcd_password', '')
    budget = GcdBudget(bool(username and password), background=background)
    return GcdClient(username=username, password=password, charge=budget.charge,
                     preflight=budget.preflight, limited=budget.limited)


def series(client: GcdClient, data: Dict[str, Any]) -> Tuple[str, Tuple[str, ...]]:
    try:
        identity = client.identity(data['api_url'], 'series')
        text(data['name'], required=True)
        if data['year_began'] is not None and (type(data['year_began']) is not int
                                              or not 1 <= data['year_began'] <= 9999):
            raise GcdError('malformed')
        members = data['active_issues']
        if not isinstance(members, list):
            raise GcdError('malformed')
        if len(members) > 100000:
            raise GcdError('response_limit')
        ids = tuple(client.identity(value, 'issue') for value in members)
        if len(set(ids)) != len(ids):
            raise GcdError('coherence')
        client.identity(data['publisher'], 'publisher')
        return identity, ids
    except (KeyError, TypeError):
        raise GcdError('malformed') from None


def issue(client: GcdClient, data: Dict[str, Any], parent: str, expected: str) -> SnapshotIssue:
    try:
        identity = client.identity(data['api_url'], 'issue')
        if identity != expected or client.identity(data['series'], 'series') != parent:
            raise GcdError('coherence')
        variant = data['variant_of']
        relation = None if variant is None else GcdVariantRelation(identity,
            client.identity(variant, 'issue'), 'gcd-rest/v1:variant_of')
        staged = GcdIssueSnapshot(identity, parent, GcdIssueNumber(text(data['number'])),
            text(data['title']), GcdPartialDate(text(data['key_date'])),
            GcdPartialDate(text(data['on_sale_date'])), relation)
        canonical = canonical_issue(staged, 'key_date')
        # Source text is an additional date fact, not an exact-day substitute.
        facts = replace(canonical.facts,
            number=replace(canonical.facts.number, provenance='gcd-rest/v1'),
            dates=tuple(replace(d, provenance='gcd-rest/v1') for d in canonical.facts.dates) + (
                BibliographicDate.interpret(text(data['publication_date']), DateKind.PUBLICATION,
                    'gcd-rest/v1', 'publication_date'),))
        return SnapshotIssue('gcd', identity, parent, staged.title, facts, canonical.variant_of,
                             bibliography(data))
    except (KeyError, ValueError, TypeError):
        raise GcdError('malformed') from None


class GcdMetadataProvider(MetadataSearchProvider, MetadataSnapshotProvider, MetadataArtworkProvider):
    search_label = 'GCD'
    search_supports_year = True

    def search_unavailable(self):
        return None if Settings().sv.gcd_enabled else 'disabled'

    async def search_aggregate(self, query, year=None):
        return await self.search_volumes(query, year)

    def __init__(self, client_factory: Callable[[], GcdClient] = production_client,
                 clock: Callable[[], float] = time):
        self.client_factory = client_factory
        self.clock = clock

    async def fetch_snapshot_scheduled(self, provider_id: str) -> ProviderVolumeSnapshot:
        # Internal transport injection remains useful for deterministic tests.
        factory = (lambda: production_client(background=True)) if self.client_factory is production_client else self.client_factory
        return await GcdMetadataProvider(factory, self.clock).fetch_snapshot(provider_id)

    @staticmethod
    def search_result(client: GcdClient, data: Dict[str, Any]) -> VolumeSearchResult:
        identity, members = series(client, data)
        return VolumeSearchResult('gcd', identity, data['name'], data['year_began'],
            1, None, None, 'https://www.comics.org/series/' + identity + '/', [],
            None, len(members), data.get('language') not in (None, '', 'en'), None,
            artwork_hint=members[0] if members else None)

    def search_artwork_url(self, provider_id, hint):
        client = self.client_factory()
        try:
            data = client.get('issue/' + resource_id(hint) + '/')
            if (client.identity(data.get('api_url'), 'issue') != hint
                    or client.identity(data.get('series'), 'series') != provider_id):
                raise GcdError('coherence')
            return text(data.get('cover')) or None
        finally:
            client.close()

    async def search_volumes(self, query: Any, year: Optional[int] = None) -> List[VolumeSearchResult]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise GcdError('query')
        if year is not None and (type(year) is not int or not 1 <= year <= 9999):
            raise GcdError('query')
        client = self.client_factory()
        try:
            if query.startswith('gcd:'):
                identity = resource_id(query[4:])
                results = [self.search_result(client, client.get('series/' + identity + '/'))]
                if results[0].provider_id != identity:
                    raise GcdError('coherence')
            else:
                path = 'series/name/' + quote(query, safe='') + '/'
                if year is not None:
                    path += 'year/' + str(year) + '/'
                results, seen = [], set()
                for page in range(1, 6):
                    data = client.get(path, {'page': page})
                    rows = data.get('results')
                    if (not isinstance(rows, list) or len(rows) > 50
                            or type(data.get('count')) is not int or data['count'] < 0
                            or 'next' not in data):
                        raise GcdError('pagination')
                    for row in rows:
                        if not isinstance(row, dict):
                            raise GcdError('malformed')
                        result = self.search_result(client, row)
                        if result.provider_id in seen:
                            raise GcdError('pagination')
                        seen.add(result.provider_id)
                        results.append(result)
                    next_url = data['next']
                    if next_url is None:
                        break
                    if page == 5 or not rows or not isinstance(next_url, str):
                        raise GcdError('search_limit')
                    parsed = urlsplit(next_url)
                    if ((parsed.scheme, parsed.netloc) != client.origin or parsed.fragment
                            or unquote(parsed.path) != '/api/' + unquote(path)
                            or parse_qs(parsed.query) != {'page': [str(page + 1)]}):
                        raise GcdError('pagination')
            existing = dict(get_db().execute(
                "SELECT provider_id,volume_id FROM volume_external_ids WHERE provider='gcd'"))
            for result in results:
                result.already_added = existing.get(result.provider_id)
            return results
        finally:
            client.close()

    async def fetch_snapshot(self, provider_id: str) -> ProviderVolumeSnapshot:
        identity = resource_id(provider_id)
        client = self.client_factory()
        try:
            start = client.get('series/' + identity + '/')
            actual, members = series(client, start)
            if actual != identity:
                raise GcdError('coherence')
            if len(members) > MAX_ISSUES:
                raise GcdError('snapshot_budget_unsupported')
            # One publisher lookup is explicit metadata cost, not hidden artwork
            # or per-search-result N+1 work. No covers are fetched in v1.
            client.preflight(len(members) + 2)
            publisher_id = client.identity(start['publisher'], 'publisher')
            publisher = client.get('publisher/' + publisher_id + '/')
            if client.identity(publisher.get('api_url'), 'publisher') != publisher_id:
                raise GcdError('coherence')
            publisher_name = text(publisher.get('name'), required=True)
            acquired = []
            rich_bytes = 0
            for member in sorted(members, key=int):
                child = issue(client, client.get('issue/' + member + '/'), identity, member)
                rich_bytes += child.bibliography.text_bytes if child.bibliography else 0
                if rich_bytes > MAX_SNAPSHOT_TEXT_BYTES:
                    raise GcdError('bibliography_snapshot_limit')
                acquired.append(child)
            issues = tuple(acquired)
            end = client.get('series/' + identity + '/')
            end_id, end_members = series(client, end)
            if (end_id != identity or set(end_members) != set(members)
                    or any(start.get(k) != end.get(k) for k in ('name', 'year_began', 'publisher'))):
                raise GcdError('coherence')
            digest = sha256('\n'.join(sorted(members)).encode()).hexdigest()
            volume = VolumeMetadata('gcd', identity, start['name'], start['year_began'], 1,
                None, None, escape(text(start.get('notes')) or ''),
                'https://www.comics.org/series/' + identity + '/', [], publisher_name,
                len(issues), start.get('language') not in (None, '', 'en'), None)
            try:
                return ProviderVolumeSnapshot(volume, issues,
                    SnapshotReceipt(SNAPSHOT_POLICY, digest, self.clock(), len(issues)), publication(start))
            except ValueError:
                raise GcdError('coherence') from None
        finally:
            client.close()

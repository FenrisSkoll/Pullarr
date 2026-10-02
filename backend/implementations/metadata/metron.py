"""Metron series/issue adapter; selected authority is always Metron."""

import json
from html import escape
from os import replace
from pathlib import Path
from re import fullmatch
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from requests import RequestException, Session
from typing_extensions import Literal

from backend.base.definitions import DateType
from backend.base.file_extraction import extract_issue_number
from backend.base.issue_facts import (BibliographicDate, DateKind,
                                      IssueFacts, IssueNumberFacts)
from backend.implementations.metadata.enrichment import (
    IdentityAssertion, MetadataEnrichmentProvider, MetadataScheduledProvider,
    ProviderIssueFacts, VolumeFetchResult)
from backend.implementations.metadata.format_evidence import (
    PhysicalFormat, ProviderFormatEvidence)
from backend.implementations.metadata.metron_client import (MetronClient,
                                                            MetronError)
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata,
                                                     VolumeSearchResult)
from backend.implementations.metadata.provider import (MetadataSearchProvider,
                                                       MetadataVolumeProvider)
from backend.implementations.metadata.publication_evidence import (
    ProviderPublicationEvidence, PublicationKind)
from backend.implementations.metadata.switch_target import \
    MetadataReviewProvider
from backend.internals.db import DBConnection, get_db
from backend.internals.settings import Settings


def resource_id(value: Any) -> str:
    if type(value) is not int and not isinstance(value, str):
        raise MetronError('malformed')
    value = str(value)
    if not fullmatch(r'[1-9][0-9]*', value) or int(value) >= 2**63:
        raise MetronError('malformed')
    return value


def optional_text(value: Any) -> Optional[str]:
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise MetronError('malformed')
    return value


def references(data: Dict[str, Any], entity: Literal['volume', 'issue']) -> List[IdentityAssertion]:
    return [IdentityAssertion(entity, 'metron', resource_id(data['id']),
                              provider, resource_id(data[key]), 'metron')
            for key, provider in (('cv_id', 'comicvine'), ('gcd_id', 'gcd'))
            if data.get(key) is not None]


class MetronMetadataProvider(MetadataSearchProvider, MetadataVolumeProvider,
                             MetadataEnrichmentProvider, MetadataScheduledProvider, MetadataReviewProvider):
    """Deliberately no bulk capability: sequential full snapshots only."""
    search_label = 'Metron'

    def search_unavailable(self):
        return None if Settings().sv.metron_api_token.strip() else 'auth_required'

    async def search_aggregate(self, query, year=None):
        return self._search(query, bounded=True)

    @staticmethod
    def issue_metadata(data: Dict[str, Any], parent: str, date_type: DateType) -> IssueMetadata:
        try:
            identity = resource_id(data['id'])
            if resource_id(data['series']['id']) != parent:
                raise MetronError('malformed')
            number = data['number']
            if not isinstance(number, str) or not number.strip():
                raise MetronError('malformed')
            calculated = extract_issue_number(number)
            if isinstance(calculated, tuple):
                calculated = calculated[0]
            stories = data.get('name') or []
            if not isinstance(stories, list) or not all(isinstance(s, str) for s in stories):
                raise MetronError('malformed')
            title = optional_text(data.get('title')) or '; '.join(stories) or None
            description = optional_text(data.get('desc'))
            references(data, 'issue')  # Validate assertions without attaching them.
            return IssueMetadata(
                'metron', identity, parent, number, float(calculated or 0.0),
                title, optional_text(data.get(date_type.value)),
                escape(description) if description else None)
        except (KeyError, TypeError, ValueError, IndexError):
            raise MetronError('malformed') from None

    @staticmethod
    def volume_metadata(data: Dict[str, Any], issues: List[IssueMetadata]) -> VolumeMetadata:
        try:
            identity = resource_id(data['id'])
            title = optional_text(data['name'])
            aliases = data.get('alt_names') or []
            if not title or not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise MetronError('malformed')
            if type(data['volume']) is not int or type(data['issue_count']) is not int or data['issue_count'] < 0:
                raise MetronError('malformed')
            year = data.get('year_began')
            if year is not None and type(year) is not int:
                raise MetronError('malformed')
            description = optional_text(data.get('desc'))
            references(data, 'volume')
            site = optional_text(data.get('resource_url'))
            if site and (urlsplit(site).scheme != 'https' or urlsplit(site).netloc != 'metron.cloud'):
                raise MetronError('malformed')
            return VolumeMetadata(
                'metron', identity, title, year, data['volume'], None, None,
                escape(description) if description else None, site, aliases,
                optional_text((data.get('publisher') or {}).get('name')),
                data['issue_count'], data.get('language') not in (None, '', 'en'),
                issues)
        except (KeyError, TypeError, AttributeError):
            raise MetronError('malformed') from None

    @classmethod
    def volume_result(cls, data: Dict[str, Any], issue_data: List[Dict[str, Any]],
                      date_type: DateType) -> VolumeFetchResult:
        """Map one response snapshot into independent metadata and assertions."""
        parent = resource_id(data.get('id'))
        issues = [cls.issue_metadata(row, parent, date_type) for row in issue_data]
        metadata = cls.volume_metadata(data, issues)
        enrichment = references(data, 'volume')
        for row in issue_data:
            enrichment.extend(references(row, 'issue'))
        source_type = data.get('series_type')
        raw_type = source_type.get('name') if isinstance(source_type, dict) else None
        evidence = None
        publication = None
        if isinstance(raw_type, str) and raw_type.strip():
            # Exact documented/captured labels only; unknowns retain provenance.
            physical = {'Hardcover': PhysicalFormat.HARDCOVER,
                        'Trade Paperback': PhysicalFormat.TRADE_PAPERBACK}.get(raw_type)
            evidence = ProviderFormatEvidence('metron', parent, 'series_type.name', raw_type, physical)
            kind = {'One-Shot': PublicationKind.ONE_SHOT,
                    'Omnibus': PublicationKind.OMNIBUS}.get(raw_type)
            publication = ProviderPublicationEvidence('metron', parent, 'series_type.name', raw_type, kind)
        facts = tuple(ProviderIssueFacts('metron', resource_id(row['id']), parent,
            IssueFacts(IssueNumberFacts.interpret(row['number'], 'metron_field', 'number'),
                tuple(BibliographicDate.interpret(optional_text(row.get(field)), kind,
                    'metron_field', field) for field, kind in
                    (('cover_date', DateKind.COVER), ('store_date', DateKind.ON_SALE))),
                date_type.value)) for row in issue_data)
        return VolumeFetchResult(metadata, tuple(enrichment), evidence, publication, facts)

    @classmethod
    def search_result(cls, data: Dict[str, Any]) -> VolumeSearchResult:
        value = cls.volume_metadata(dict(data, name=data.get('series')), [])
        return VolumeSearchResult(
            value.provider, value.provider_id, value.title, value.year,
            value.volume_number, None, None,
            'https://metron.cloud/api/series/' + value.provider_id + '/',
            [], value.publisher, value.issue_count, value.translated, None)

    async def search_volumes(self, query: str) -> List[VolumeSearchResult]:
        return self._search(query)

    def _search(self, query: str, bounded: bool = False) -> List[VolumeSearchResult]:
        client = MetronClient()
        if query.startswith('metron:'):
            data = client.get('series/' + resource_id(query[7:]) + '/')
            result = self.search_result(dict(data, series=data.get('name')))
            result.site_url = data.get('resource_url') or result.site_url
            results = [result]
        else:
            rows = (client.pages('series/', {'name': query}, max_pages=5, max_results=250) if bounded
                    else client.pages('series/', {'name': query}))
            results = [self.search_result(data) for data in rows]
        existing = dict(get_db().execute(
            "SELECT provider_id,volume_id FROM volume_external_ids WHERE provider='metron'"))
        for result in results:
            result.already_added = existing.get(result.provider_id)
        return results

    @staticmethod
    def cache_directory() -> Optional[Path]:
        if not DBConnection.default_file:
            return None
        return Path(DBConnection.default_file).resolve().parent / 'metron-issue-cache'

    def issue_detail(self, client: MetronClient, summary: Dict[str, Any], parent: str,
                     *, cache_write: bool = True) -> Dict[str, Any]:
        identity = resource_id(summary.get('id'))
        modified = optional_text(summary.get('modified'))
        directory = self.cache_directory()
        path = directory / (identity + '.json') if directory else None
        if path and modified:
            try:
                with path.open(encoding='utf-8') as handle:
                    cached = json.load(handle)
                if (cached.get('modified') == modified
                        and resource_id(cached.get('id')) == identity
                        and resource_id(cached['series']['id']) == parent):
                    self.issue_metadata(cached, parent, Settings().sv.date_type)
                    return cached
            except (OSError, ValueError, KeyError, TypeError, AttributeError, MetronError):
                pass
        data = client.get('issue/' + identity + '/')
        if resource_id(data.get('id')) != identity:
            raise MetronError('malformed')
        self.issue_metadata(data, parent, Settings().sv.date_type)
        if modified and data.get('modified') != modified:
            # Changed while enumerating: do not reconcile an unstable snapshot.
            raise MetronError('unavailable')
        if path and modified and cache_write:
            # Only public fields needed by this adapter, never accounts/headers.
            keep = {key: data.get(key) for key in (
                'id', 'series', 'number', 'title', 'name', 'cover_date',
                'store_date', 'desc', 'cv_id', 'gcd_id', 'modified')}
            temporary = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                        prefix=identity + '-', suffix='.tmp', delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(keep, handle)
                replace(str(temporary), str(path))
            except OSError:
                # A cache failure does not change valid metadata or DB authority.
                pass
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
        return data

    @staticmethod
    def cover(url: Optional[str]) -> Optional[bytes]:
        if not url:
            return None
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.netloc not in ('static.metron.cloud', 'metron.cloud'):
            return None
        try:
            with Session() as session:
                session.trust_env = False
                response = session.get(url, timeout=(10, 30), allow_redirects=False)
                if response.status_code == 200 and len(response.content) <= 10 * 1024 * 1024:
                    return response.content
        except RequestException:
            pass
        return None

    async def fetch_volume(self, provider_id: str) -> VolumeMetadata:
        return (await self.fetch_volume_enriched(provider_id)).metadata

    async def fetch_volume_enriched(self, provider_id: str) -> VolumeFetchResult:
        return await self._fetch(provider_id)

    async def fetch_review(self, provider_id, issue_limit):
        return await self._fetch(provider_id, review_limit=issue_limit)

    async def fetch_volume_scheduled(self, provider_id: str) -> VolumeFetchResult:
        from backend.implementations.metadata.metron_budget import \
            charge_background_request
        return await self._fetch(provider_id, charge_background_request)

    async def _fetch(self, provider_id: str,
                     before_request: Optional[Callable[[], None]] = None,
                     *, review_limit: Optional[int] = None) -> VolumeFetchResult:
        identity = resource_id(provider_id)
        client = MetronClient(before_request=before_request)
        data = client.get('series/' + identity + '/')
        if resource_id(data.get('id')) != identity:
            raise MetronError('malformed')
        if review_limit is not None and (type(data.get('issue_count')) is not int
                or not 0 <= data['issue_count'] <= review_limit):
            raise MetronError('malformed')
        summaries = (client.pages('series/' + identity + '/issue_list/') if review_limit is None else
                     client.pages('series/' + identity + '/issue_list/', max_results=review_limit,
                                  max_pages=review_limit + 1))
        issue_ids = [resource_id(row.get('id')) for row in summaries]
        if len(set(issue_ids)) != len(issue_ids) or len(issue_ids) != data.get('issue_count'):
            raise MetronError('malformed')
        issues = ([self.issue_detail(client, row, identity) for row in summaries] if review_limit is None else
                  [self.issue_detail(client, row, identity, cache_write=False) for row in summaries])
        result = self.volume_result(data, issues, Settings().sv.date_type)
        value = result.metadata
        value.cover_link = optional_text(summaries[0].get('image')) if summaries else None
        value.cover = self.cover(value.cover_link)
        return result

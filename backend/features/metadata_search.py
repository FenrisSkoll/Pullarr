"""Interactive aggregation of result sets, never provider identities.

Serial execution deliberately retains request-thread DB/client ownership. Known
provider failures are isolated; programming errors still reach the normal 500.
"""

import json
import re
from dataclasses import asdict, dataclass, replace
from time import monotonic
from typing import List, Optional

from backend.base.custom_exceptions import (InvalidKeyValue,
                                            MetadataSourceRateLimitReached)
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.models import VolumeSearchResult
from backend.implementations.metadata.provider import MetadataSearchProvider
from backend.implementations.metadata.registry import (PROVIDERS,
                                                       get_search_provider)
from backend.internals.db import get_db

MAX_RELATED = 4
MAX_RELATIONS_PER_RESULT = 2


def normalized_title(value):
    # Punctuation and whitespace only: meaningful title terms remain intact.
    return ' '.join(re.findall(r'\w+', value.casefold()))


def rank_results(query, results, year=None):
    title = normalized_title(query)
    tokens = set(title.split())
    for result in results:
        candidate = normalized_title(result.title)
        overlap = len(tokens & set(candidate.split()))
        exact = candidate == title
        alias = any(normalized_title(a) == title for a in result.aliases)
        result.rank_components = dict(exact_title=exact, exact_alias=alias,
            query_tokens_matched=overlap, query_tokens_total=len(tokens),
            year_match=year is not None and result.year == year,
            origin=result.search_origin)
    def key(result):
        c = result.rank_components
        tier = (2 if result.search_origin == 'relation' else
                0 if c['exact_title'] or c['exact_alias'] else
                1 if c['query_tokens_matched'] else 3)
        return (tier, not c['exact_title'], not c['exact_alias'],
                -c['query_tokens_matched'], not c['year_match'],
                normalized_title(result.title))
    return sorted(results, key=key)


async def expand_related(instance, provider, results, remaining):
    """One hop, same namespace, exact ID, no issue/cover fetch or recursion."""
    visited = {r.provider_id for r in results}
    expanded = []
    for source in tuple(results):
        for relation in source.relations[:MAX_RELATIONS_PER_RESULT]:
            if (not remaining or relation.relation_type not in ('continues_as', 'continues_from')
                    or relation.source_provider != provider or relation.source_id != source.provider_id
                    or relation.target_provider != provider or relation.target_id in visited):
                continue
            visited.add(relation.target_id)
            remaining -= 1
            try:
                found = await instance.search_aggregate(('cv' if provider == 'comicvine' else provider) + ':' + relation.target_id)
                if len(found) != 1 or found[0].provider != provider or found[0].provider_id != relation.target_id:
                    continue
                expanded.append(replace(found[0], search_origin='relation', relation_reason=relation))
            except (MetadataProviderError, MetadataSourceRateLimitReached):
                continue
    return results + expanded, remaining


@dataclass
class ProviderSearchReceipt:
    provider: str
    label: str
    status: str
    result_limit: int
    results: List[VolumeSearchResult]
    reason: Optional[str] = None
    retry_at: Optional[float] = None
    duplicate_count: int = 0
    elapsed_ms: float = 0
    year_applied: bool = False


def search_scope(query):
    """Qualified IDs route once; bare numbers remain human title queries."""
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise InvalidKeyValue('query', 'Expected 1–500 characters')
    match = re.fullmatch(r'([A-Za-z][A-Za-z0-9_-]*):(.*)', query)
    if match:
        prefix, identity = match.groups()
        provider = 'comicvine' if prefix == 'cv' else prefix
        if provider in PROVIDERS:
            if not re.fullmatch(r'(?:4050-)?[1-9][0-9]*' if provider == 'comicvine' else r'[1-9][0-9]*', identity):
                raise InvalidKeyValue('query', 'Invalid qualified volume ID')
            return [provider], ('cv:' + identity if provider == 'comicvine' else query)
        if re.fullmatch(r'[0-9-]+', identity):
            raise InvalidKeyValue('provider', 'Unknown qualified prefix')
    if query.startswith('4050-'):
        if not re.fullmatch(r'4050-[1-9][0-9]*', query):
            raise InvalidKeyValue('query', 'Invalid ComicVine volume ID')
        return ['comicvine'], query
    return list(PROVIDERS), query


async def aggregated_search(query: str, year: Optional[int] = None, *, selected_provider: Optional[str] = None, expand_relations: bool = True):
    providers, routed_query = search_scope(query)
    if selected_provider is not None:
        if selected_provider not in PROVIDERS or selected_provider not in providers:
            raise InvalidKeyValue('provider', 'Invalid search scope')
        providers = [selected_provider]
    receipts = []
    remaining = MAX_RELATED
    for key in providers:
        if not issubclass(PROVIDERS[key], MetadataSearchProvider):
            receipts.append(ProviderSearchReceipt(key, key, 'unavailable', 0, [], 'unsupported_capability'))
            continue
        instance = get_search_provider(key)
        receipt = ProviderSearchReceipt(key, instance.search_label or key, 'complete', instance.search_result_limit, [])
        started = monotonic()
        try:
            unavailable = instance.search_unavailable()
            if unavailable:
                receipt.status = unavailable
                receipt.reason = unavailable
            else:
                receipt.year_applied = year is not None and instance.search_supports_year and len(providers) > 1
                found = await instance.search_aggregate(routed_query, year)
                unique = {}
                for result in found:
                    if result.provider != key or not result.provider_id:
                        raise MetadataProviderError(key, 'inconsistent_results')
                    previous = unique.get(result.provider_id)
                    if previous is not None:
                        if previous != result:
                            raise MetadataProviderError(key, 'inconsistent_results')
                        receipt.duplicate_count += 1
                    else:
                        unique[result.provider_id] = result
                if len(unique) > receipt.result_limit:
                    raise MetadataProviderError(key, 'search_limit')
                receipt.results = list(unique.values())
                if len(json.dumps([asdict(r) for r in receipt.results], ensure_ascii=True).encode()) > 8 * 1024 * 1024:
                    raise MetadataProviderError(key, 'response_limit')
                # CV cannot expose a total count through the legacy adapter.
                if len(receipt.results) == receipt.result_limit:
                    receipt.status = 'limited'
                    receipt.reason = 'at_result_limit'
                if expand_relations:
                    receipt.results, remaining = await expand_related(instance, key, receipt.results, remaining)
                receipt.results = rank_results(query, receipt.results, year)
        except MetadataProviderError as error:
            receipt.status = {'credentials': 'auth_required', 'disabled': 'disabled',
                'rate_limited': 'rate_limited', 'deferred': 'rate_limited', 'budget': 'rate_limited',
                'search_limit': 'limited', 'not_found': 'unavailable', 'unavailable': 'unavailable',
                'unsupported_capability': 'unavailable'}.get(error.reason, 'failed')
            # Controlled allowlist only, never exception/body text.
            receipt.reason = error.reason if error.reason in {
                'credentials', 'disabled', 'rate_limited', 'deferred', 'budget', 'search_limit',
                'not_found', 'unavailable', 'unsupported_capability', 'forbidden', 'malformed',
                'pagination', 'coherence', 'timeout', 'response_limit', 'inconsistent_results'} else 'provider_failure'
            receipt.retry_at = error.retry_at
            receipt.results = []
        except MetadataSourceRateLimitReached:
            # Legacy CV also uses this exception for network/JSON failures.
            receipt.status, receipt.reason = 'rate_limited', 'legacy_rate_or_transport_failure'
        receipt.elapsed_ms = round((monotonic() - started) * 1000, 3)
        receipts.append(receipt)
    return receipts


def local_search_annotations(identities):
    """Batched exact references; selected authority is not a cross-reference."""
    result = {}
    identities = tuple(dict.fromkeys(identities))
    cursor = get_db()
    for start in range(0, len(identities), 300):
        batch = identities[start:start + 300]
        sql = '''SELECT e.provider,e.provider_id,e.volume_id,v.metadata_provider,e.provenance
            FROM volume_external_ids e JOIN volumes v ON v.id=e.volume_id WHERE '''
        sql += ' OR '.join('(e.provider=? AND e.provider_id=?)' for _ in batch)
        sql += ' ORDER BY e.provider,e.provider_id,e.volume_id'
        for provider, identity, local, authority, provenance in cursor.execute(sql, tuple(v for pair in batch for v in pair)):
            result.setdefault((provider, identity), []).append(dict(volume_id=local,
                selected_provider=authority, provenance=provenance,
                kind='exact_selected_identity' if authority == provider else 'local_persisted_cross_reference'))
    return result


def aggregate_response(query, receipts, serialize, year=None):
    """Reuse explicit-provider DTOs; annotations never become add authority."""
    links = local_search_annotations((r.provider, r.provider_id) for group in receipts for r in group.results)
    groups = []
    for receipt in receipts:
        results = []
        for result in receipt.results:
            data = serialize(result)
            refs = links.get((result.provider, result.provider_id), [])
            selected = sorted(r['volume_id'] for r in refs if r['kind'] == 'exact_selected_identity')
            data.update(result_key=result.provider + ':' + result.provider_id,
                already_added=selected[0] if len(selected) == 1 else None,
                local_identity_annotations=refs,
                identity_conflict=len({r['volume_id'] for r in refs}) > 1)
            results.append(data)
        # Bound serialized response independently of provider body limits.
        if len(json.dumps(results, ensure_ascii=True).encode()) > 8 * 1024 * 1024:
            results = []
            receipt.status, receipt.reason = 'failed', 'response_limit'
        groups.append(dict(provider=receipt.provider, label=receipt.label, status=receipt.status,
            result_count=len(results), result_limit=receipt.result_limit, reason=receipt.reason,
            retry_at=receipt.retry_at, duplicate_count=receipt.duplicate_count,
            elapsed_ms=receipt.elapsed_ms, year_applied=receipt.year_applied, results=results))
    success = [g['status'] == 'complete' or bool(g['results']) for g in groups]
    status = ('complete' if all(g['status'] == 'complete' for g in groups)
              else 'partial' if any(success) else 'unavailable')
    return dict(schema='metadata-search/v2', query=query, year=year, status=status, providers=groups)

"""Explicit bounded search orchestration; no selection, DB reads or grabs."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from json import dumps
from typing import Iterable, Tuple
from uuid import uuid4

from backend.base.definitions import SpecialVersion
from backend.base.release_evaluation import (ReleaseEvaluation,
                                             ScoringPolicy, SourcePriority,
                                             TargetKind, WantedTarget)
from backend.base.release_search import (QUERY_POLICY, QueryAttempt,
                                         ReleaseSearchRequest,
                                         ReleaseSearchSource, SearchDiagnostic,
                                         SearchError, SearchLimits,
                                         SearchState, SourceConfig,
                                         SourceFailure, SourceSearchResult)
from backend.implementations.newznab import (BoundedHTTP, NewznabSource,
                                             SearchBudget, discover_prowlarr)
from backend.implementations.release_explanations import (explain_releases,
                                                          preview_explanation)
from backend.implementations.release_scoring import (evaluate_releases,
                                                     rank_evaluations)


def plan_queries(target: WantedTarget, categories=(), limits=SearchLimits()):
    """Literal labels; explicit aliases only; no numeric conversion or fuzzy titles.

Three ordered recall variants at most. All planned queries are attempted even
when a source returns a wrong release. Source acquisition never evaluates hits.
"""
    p = target.publication
    title = ' '.join(p.title.split())
    if not title:
        raise SourceFailure(SearchError.CONFIGURATION)
    issues = {i.id: i.raw_number for i in target.catalog}
    label = issues[target.issue_ids[0]] if target.kind == TargetKind.ISSUES and len(target.issue_ids) == 1 else ''
    if p.special_version == SpecialVersion.VOLUME_AS_ISSUE and label:
        label = 'Vol ' + label
    marker = {SpecialVersion.TPB: 'TPB', SpecialVersion.HARD_COVER: 'Hardcover',
              SpecialVersion.OMNIBUS: 'Omnibus', SpecialVersion.ONE_SHOT: 'One-Shot'}.get(p.special_version, '')
    if target.kind == TargetKind.COLLECTION or marker:
        label = ''
    suffix = label or marker
    year = str(p.year) if p.year is not None else ''
    variants = [' '.join(filter(None, (title, suffix, year))),
                ' '.join(filter(None, (title, suffix))), title]
    aliases = sorted({a for a in p.aliases if ' '.join(a.split()).casefold() != title.casefold()})
    if aliases:
        variants[-1] = ' '.join(filter(None, (' '.join(aliases[0].split()), suffix)))
    if len(target.issue_ids) == 1:
        issue = next(i for i in target.catalog if i.id == target.issue_ids[0])
        from re import fullmatch

        from backend.implementations.identification import title_key
        if issue.title and fullmatch(r'(?:book|volume|part)\s+\d+', title_key(issue.title)):
            issue_title = ' '.join(issue.title.split())
            issue_year = str(issue.year or p.year or '')
            variants = [' '.join(filter(None, (title, issue_title, issue_year))),
                        f'{title} {issue_title}', *variants]
    unique, seen = [], set()
    for query in variants:
        if query.casefold() not in seen:
            seen.add(query.casefold())
            unique.append(ReleaseSearchRequest(query, tuple(sorted(set(categories))), limit=limits.page_size))
    return tuple(unique[:limits.queries])


def search_source(target: WantedTarget, source: ReleaseSearchSource,
                  limits=SearchLimits()) -> SourceSearchResult:
    candidates, attempts, diagnostics = {}, [], []
    successful = False
    try:
        caps = source.capabilities()
        if not caps.search or (source.categories and not set(source.categories) <= set(caps.categories)):
            raise SourceFailure(SearchError.UNSUPPORTED)
        names = dict(caps.category_names)
        forbidden = {c for c in caps.categories if 2000 <= c < 3000 or 5000 <= c < 6000
                     or any(word in names.get(c, '').casefold().split('/') for word in ('movies', 'tv'))}
        categories = source.categories
        if set(categories) & forbidden:
            raise SourceFailure(SearchError.CONFIGURATION)
        if not categories:
            categories = tuple(c for c in caps.categories if c not in forbidden
                and (c == 7030 or 'comic' in names.get(c, '').casefold()))
            if not categories:
                categories = tuple(c for c in caps.categories if c in (7020, 7000)
                    or names.get(c, '').casefold() in ('books', 'ebooks', 'books/ebooks'))
                if 7020 in categories:
                    categories = (7020,)
            if not categories:
                raise SourceFailure(SearchError.UNSUPPORTED)
        plan = plan_queries(target, categories, limits)
        for planned in plan:
            offset, pages_seen = 0, set()
            for page_number in range(limits.pages):
                request = replace(planned, offset=offset, limit=min(limits.page_size, caps.limit))
                try:
                    page = source.search(request)
                except SourceFailure as error:
                    attempts.append(QueryAttempt(source.source.key, request, 0, error.code))
                    diagnostics.append(SearchDiagnostic(source.source.key, error.code,
                                       request.query_id, offset, retry_after=error.retry_after))
                    # Authentication, unavailable and limits stop this source, never its peers.
                    return SourceSearchResult(source.source, SearchState.PARTIAL if successful else SearchState.FAILED,
                        tuple(candidates.values()), tuple(attempts), tuple(diagnostics))
                successful = True
                attempts.append(QueryAttempt(source.source.key, request, page.count,
                    candidate_ids=tuple(c.candidate_id for c in page.candidates[:request.limit])))
                diagnostics.extend(SearchDiagnostic(source.source.key, SearchError.ITEM,
                    request.query_id, offset, i) for i in page.invalid_items)
                signature = tuple(sorted(c.candidate_id or repr(c) for c in page.candidates))
                if ((page.offset is not None and page.offset != offset) or
                        (page.count and signature in pages_seen)):
                    diagnostics.append(SearchDiagnostic(source.source.key, SearchError.PAGINATION,
                                       request.query_id, offset))
                    break
                pages_seen.add(signature)
                if page.count > request.limit or (page.count == 0 and page.total is not None and page.total > offset):
                    diagnostics.append(SearchDiagnostic(source.source.key, SearchError.PAGINATION,
                                       request.query_id, offset))
                for c in page.candidates[:request.limit]:
                    # No invented title identity. Identity-less observations remain separate.
                    key = c.candidate_id or f'unidentified-{len(candidates)}'
                    previous = candidates.get(key)
                    if previous is not None and previous != c:
                        diagnostics.append(SearchDiagnostic(source.source.key, SearchError.ITEM,
                                           request.query_id, offset))
                        candidates[key] = min((previous, c), key=repr)
                    elif key not in candidates:
                        candidates[key] = c
                    if len(candidates) >= limits.source_results:
                        diagnostics.append(SearchDiagnostic(source.source.key, SearchError.LIMIT,
                                           request.query_id, offset))
                        return SourceSearchResult(source.source, SearchState.PARTIAL,
                            tuple(candidates.values()), tuple(attempts), tuple(diagnostics))
                offset += page.count
                if page.count == 0 or (page.total is not None and offset >= page.total) or (
                        page.total is None and page.count < request.limit):
                    break
                if page_number == limits.pages - 1:
                    diagnostics.append(SearchDiagnostic(source.source.key, SearchError.LIMIT,
                                       request.query_id, offset))
    except SourceFailure as error:
        diagnostics.append(SearchDiagnostic(source.source.key, error.code, retry_after=error.retry_after))
    state = SearchState.PARTIAL if successful and diagnostics else SearchState.COMPLETE if successful else SearchState.FAILED
    return SourceSearchResult(source.source, state, tuple(candidates.values()), tuple(attempts), tuple(diagnostics))


@dataclass(frozen=True)
class ReleaseSearchBatch:
    correlation: str
    state: SearchState
    sources: Tuple[SourceSearchResult, ...]
    evaluations: Tuple[ReleaseEvaluation, ...]
    diagnostics: Tuple[SearchDiagnostic, ...] = ()
    query_policy: str = QUERY_POLICY
    configuration_fingerprint: str = ''


def evaluate_search(target, sources: Iterable[ReleaseSearchSource], *, policy=ScoringPolicy(),
                    limits=SearchLimits(), correlation=None, initial=(), diagnostics=()):
    """Source-neutral seam, one supplied target, one shared bulk scorer. No close.

Caller owns source/resolver lifetime; use configured_search for an automatically
closed preview operation. Ranking never selects or filters rejected candidates.
"""
    ordered = sorted(sources, key=lambda s: s.source.key)
    if len({s.source.key for s in ordered}) != len(ordered):
        raise SourceFailure(SearchError.CONFIGURATION)
    reports, candidates, notes = list(initial), [], list(diagnostics)
    priorities = { (p.kind, p.key): p for p in policy.source_priorities }
    for source in ordered[:limits.sources]:
        if len(candidates) >= limits.total_results:
            notes.append(SearchDiagnostic(source.source.key, SearchError.LIMIT))
            break
        result = search_source(target, source, replace(limits,
            source_results=min(limits.source_results, limits.total_results - len(candidates))))
        reports.append(result)
        candidates.extend(result.candidates)
        priorities[(source.source.kind, source.source.key)] = SourcePriority(
            source.source.kind, source.source.key, source.priority)
    if len(ordered) > limits.sources:
        notes.append(SearchDiagnostic('search', SearchError.LIMIT))
    active = [r for r in reports if r.state != SearchState.DISABLED]
    state = (SearchState.COMPLETE if active and all(r.state == SearchState.COMPLETE for r in active) and not notes
             else SearchState.PARTIAL if any(r.state in (SearchState.COMPLETE, SearchState.PARTIAL) for r in active)
             else SearchState.FAILED)
    policy = replace(policy, source_priorities=tuple(sorted(priorities.values(), key=lambda p: (p.kind.value, p.key))))
    evaluations = rank_evaluations(evaluate_releases(target, candidates, policy))
    return ReleaseSearchBatch(correlation or uuid4().hex, state, tuple(reports), evaluations, tuple(notes))


@contextmanager
def retained_search_sources(target, configs: Iterable[SourceConfig], *,
                            limits=SearchLimits(), transport=None, cancelled=lambda: False):
    """Caller-owned live source scope; closing always expires private references."""
    configs = tuple(sorted(configs, key=lambda c: c.key))
    if not configs or len(configs) > limits.sources or len({c.key for c in configs}) != len(configs):
        raise SourceFailure(SearchError.CONFIGURATION)
    # Validate the intent before even discovery/capability traffic.
    plan_queries(target, limits=limits)
    transport = transport or BoundedHTTP(SearchBudget(limits, cancelled))
    sources, reports, diagnostics = [], [], []
    try:
        for config in configs:
            if not config.enabled:
                reports.append(SourceSearchResult(config.source(), SearchState.DISABLED))
                continue
            if len(sources) >= limits.sources:
                diagnostics.append(SearchDiagnostic(config.namespace, SearchError.LIMIT))
                continue
            try:
                if config.mode == 'prowlarr':
                    discovered, excluded, truncated = discover_prowlarr(config, transport, limits.sources - len(sources))
                    sources.extend(discovered)
                    if not discovered:
                        raise SourceFailure(SearchError.UNSUPPORTED)
                    if truncated:
                        diagnostics.append(SearchDiagnostic(config.namespace, SearchError.LIMIT))
                    # Excluded disabled/torrent indexers are status, not failed Usenet searches.
                    reports.extend(SourceSearchResult(config.source(i), SearchState.DISABLED) for i in excluded)
                else:
                    from backend.implementations.torznab import TorznabSource
                    sources.append((TorznabSource if config.mode == 'torznab' else NewznabSource)(config, transport))
            except SourceFailure as error:
                reports.append(SourceSearchResult(config.source(), SearchState.FAILED, diagnostics=(
                    SearchDiagnostic(config.namespace, error.code, retry_after=error.retry_after),)))
        fingerprint = sha256(dumps((QUERY_POLICY, repr(limits), [
            (c.namespace, c.mode, c.enabled, c.priority, sorted(set(c.categories))) for c in configs
        ]), sort_keys=True).encode()).hexdigest()
        yield sources, reports, diagnostics, fingerprint
    finally:
        for source in sources:
            source.close()


def configured_search(target, configs: Iterable[SourceConfig], *, policy=ScoringPolicy(),
                      limits=SearchLimits(), transport=None, cancelled=lambda: False):
    """Preview operation: configuration supplied, no DB access, always expires URLs."""
    with retained_search_sources(target, configs, limits=limits, transport=transport,
                                 cancelled=cancelled) as (sources, reports, diagnostics, fingerprint):
        batch = evaluate_search(target, sources, policy=policy, limits=limits,
                                initial=reports, diagnostics=diagnostics)
        return replace(batch, configuration_fingerprint=fingerprint)


def preview_search(batch: ReleaseSearchBatch) -> dict:
    """Allowlisted transport. Never config, result ID, resolver context or URL."""
    def diagnostic(d):
        return {'source': d.source, 'code': d.code.value, 'query_id': d.query_id,
                'offset': d.offset, 'item': d.item, 'retry_after': d.retry_after}
    return {'correlation': batch.correlation, 'state': batch.state.value,
        'query_policy': batch.query_policy, 'configuration_fingerprint': batch.configuration_fingerprint,
        'sources': [{'source': {'key': s.source.key, 'name': s.source.name, 'via': s.source.via},
            'state': s.state.value, 'count': len(s.candidates),
            'attempts': [{'query_id': a.request.query_id, 'query': a.request.query,
                          'offset': a.request.offset, 'count': a.count,
                          'candidate_ids': list(a.candidate_ids),
                          'error': a.error.value if a.error else None} for a in s.attempts],
            'diagnostics': [diagnostic(d) for d in s.diagnostics]} for s in batch.sources],
        'diagnostics': [diagnostic(d) for d in batch.diagnostics],
        'results': [preview_explanation(e) for e in explain_releases(batch.evaluations)]}


def check_source(config: SourceConfig, transport=None):
    """Explicit non-destructive capability/authentication check, never an NZB grab."""
    transport = transport or BoundedHTTP(SearchBudget(SearchLimits()))
    sources = ()
    try:
        if config.mode == 'prowlarr':
            sources, excluded, truncated = discover_prowlarr(config, transport)
        else:
            from backend.implementations.torznab import TorznabSource
            sources, excluded, truncated = ((TorznabSource if config.mode == 'torznab' else NewznabSource)(config, transport),), (), False
        statuses = []
        for source in sources:
            try:
                caps = source.capabilities()
                supported = caps.search and (not source.categories or set(source.categories) <= set(caps.categories))
                statuses.append({'source': source.source.name, 'usable': supported,
                                 'error': None if supported else SearchError.UNSUPPORTED.value})
            except SourceFailure as error:
                statuses.append({'source': source.source.name, 'usable': False, 'error': error.code.value})
        return {'sources': statuses, 'excluded_indexers': list(excluded), 'truncated': truncated,
                'usable': bool(statuses) and all(s['usable'] for s in statuses) and not truncated,
                'authentication_scope': 'capabilities_only'}
    finally:
        for source in sources:
            source.close()

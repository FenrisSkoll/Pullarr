"""Explicit manual DDL search/selection. Automatic/RSS callers stay legacy.

All release decisions are Phase 5B receipts. Page resolution can reveal new
offerings; each is evaluated afresh, never substituted by mirror availability.
"""

from dataclasses import dataclass, field, replace
from hashlib import sha256
from threading import RLock
from time import monotonic
from typing import Optional
from uuid import uuid4

from bs4 import BeautifulSoup

from backend.base.definitions import (BlocklistReason,
                                      DownloadType, SpecialVersion)
from backend.base.release_evaluation import (Compatibility, ReleaseEvaluation,
                                             Rule, ScoringPolicy, TargetKind,
                                             WantedTarget)
from backend.features.library_identification import local_matching_snapshot
from backend.implementations.direct_download_source import (DDLError,
                                                            DDLSourceConfig,
                                                            GetComicsSource,
                                                            offering_candidate)
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.release_candidates import adapt_ddl_result
from backend.implementations.release_explanations import (evaluation_identity,
                                                          explain_release,
                                                          preview_explanation)
from backend.implementations.release_scoring import (build_wanted_target,
                                                     evaluate_releases,
                                                     rank_evaluations)
from backend.implementations.release_search import plan_queries
from backend.internals.db import get_db

MAX_SEARCHES = 32
MAX_RESULTS = 200
LIFETIME = 900


def force_available(evaluation: ReleaseEvaluation) -> bool:
    """Force is bibliographic authority, not an unsupported/empty acquisition."""
    return not set(evaluation.rejections).intersection({
        Rule.ACQUISITION_UNAVAILABLE, Rule.ARCHIVE_WRONG, Rule.SIZE_ZERO})


def load_target(volume_id: int, issue_id: Optional[int] = None):
    """One canonical snapshot and two bounded local reads, no provider traffic."""
    snapshot = local_matching_snapshot(tuple(PROVIDERS), volume_ids=(volume_id,))
    row = get_db().execute('SELECT alt_title FROM volumes WHERE id = ?', (volume_id,)).fetchone()
    if row is None or volume_id not in snapshot.volumes:
        raise DDLError('target_unavailable')
    rows = get_db().execute('''SELECT i.id,i.date,EXISTS(
        SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id),d.year,i.title
        FROM issues i LEFT JOIN issue_number_facts n ON n.issue_id=i.id
        LEFT JOIN issue_date_facts d ON d.issue_id=i.id AND d.source_field=n.selected_date_field
        WHERE i.volume_id=? ORDER BY i.id''', (volume_id,)).fetchall()
    ids = (issue_id,) if issue_id is not None else tuple(r[0] for r in rows)
    if not ids or (issue_id is not None and issue_id not in {r[0] for r in rows}):
        raise DDLError('target_unavailable')
    kind = TargetKind.ISSUES if issue_id is not None else TargetKind.WHOLE_VOLUME
    if len(ids) == 1 and snapshot.volumes[volume_id].special_version not in (
            SpecialVersion.NORMAL, SpecialVersion.VOLUME_AS_ISSUE):
        kind = TargetKind.COLLECTION
    years = {r[0]: int(r[1][:4]) for r in rows if r[1] and str(r[1])[:4].isdigit()}
    years.update({r[0]: r[3] for r in rows if r[3] is not None})
    target = build_wanted_target(snapshot, volume_id, ids, kind=kind,
        issue_years=years, owned_issue_ids=tuple(r[0] for r in rows if r[2]))
    titles = {r[0]: r[4] for r in rows}
    target = replace(target, catalog=tuple(replace(i, title=titles[i.id]) for i in target.catalog))
    if row[0]:
        target = replace(target, publication=replace(target.publication, aliases=(row[0],)))
    return target


def configured_sources():
    from backend.implementations.indexer_client_manager import IndexerClients

    result = {}
    for indexer in IndexerClients.get_all_clients():
        data = indexer.get_indexer_data()
        if data['enabled'] and indexer.download_type == DownloadType.DDL and indexer.client_type == 'GetComics':
            result[data['id']] = DDLSourceConfig(data['id'], data['title'], data['url'],
                tuple(data['gc_service_preference'] or ()), bool(data['gc_avoid_large_downloads']))
    return result


def blocked_links():
    # Exactly the legacy predicate, batched once instead of once per result/link.
    return frozenset(r[0] for r in get_db().execute(
        'SELECT COALESCE(download_link,web_link) FROM blocklist').fetchall() if r[0])


@dataclass
class Selection:
    source: GetComicsSource
    raw: dict = field(repr=False)
    evaluation: ReleaseEvaluation
    identity: str
    offerings: dict = field(default_factory=dict, repr=False)
    force: Optional[bool] = None
    claimed: bool = False
    receipt: Optional[dict] = None
    authorization: dict = field(default_factory=dict)
    before_dispatch: object = field(default=None, repr=False)


@dataclass
class SearchSession:
    target: WantedTarget
    issue_id: Optional[int]
    policy: ScoringPolicy
    expires: float
    selections: dict


class ManualDDL:
    """Bounded process-local contexts. Restart/eviction requires a fresh search.

    The lock serializes selection and dispatch, including double-clicks. Claimed
    dispatch failures stay claimed: legacy workers cannot promise exactly once.
    """

    def __init__(self, *, clock=monotonic, target_loader=load_target,
                 sources_loader=configured_sources, block_loader=blocked_links,
                 source_factory=GetComicsSource, dispatch=None, policy_loader=ScoringPolicy):
        self.clock, self.target_loader, self.sources_loader = clock, target_loader, sources_loader
        self.block_loader, self.source_factory = block_loader, source_factory
        self.dispatch = dispatch or dispatch_offering
        self.policy_loader = policy_loader
        self.sessions = {}
        self.lock = RLock()

    def _prune(self):
        for key in tuple(self.sessions):
            if self.sessions[key].expires <= self.clock():
                del self.sessions[key]

    def search(self, volume_id: int, issue_id: Optional[int] = None, *, target=None, cancelled=lambda: False):
        target = target or self.target_loader(volume_id, issue_id)
        policy = self.policy_loader()
        configs = self.sources_loader()
        if not configs:
            raise DDLError('source_disabled')
        candidates, raw, attempts, errors = {}, {}, [], []
        # Shared literal query plan: bounded, no match-dependent query feedback.
        for config in sorted(configs.values(), key=lambda c: c.id)[:4]:
            source = self.source_factory(config)
            for request in plan_queries(target):
                seen = set()
                for page in range(1, 3):
                    if cancelled():
                        raise DDLError('cancelled')
                    try:
                        values, more, invalid, truncated = source.search(request.query, page)
                    except DDLError as error:
                        errors.append({'source': config.id, 'code': error.code})
                        break
                    attempts.append({'source': config.id, 'query': request.query, 'page': page, 'count': len(values)})
                    signature = tuple(sorted(c.candidate_id for c in values))
                    if signature in seen and values:
                        errors.append({'source': config.id, 'code': 'repeated_page'})
                        break
                    seen.add(signature)
                    if invalid or truncated:
                        errors.append({'source': config.id, 'code': 'invalid_items' if invalid else 'result_limit'})
                    for c in values:
                        candidates[c.candidate_id] = c
                        raw[c.candidate_id] = (source, source.records[c.candidate_id])
                        if len(candidates) >= MAX_RESULTS:
                            break
                    if len(candidates) >= MAX_RESULTS or (more and page == 2):
                        errors.append({'source': config.id, 'code': 'result_limit'})
                        break
                    if not more:
                        break
                if len(candidates) >= MAX_RESULTS:
                    break
            if len(candidates) >= MAX_RESULTS:
                break
        if len(configs) > 4:
            errors.append({'source': None, 'code': 'source_limit'})
        evaluations = rank_evaluations(evaluate_releases(target, candidates.values(), policy))
        selections = {}
        for e in evaluations:
            source, record = raw[e.candidate.candidate_id]
            selections[uuid4().hex] = Selection(source, dict(record), e, evaluation_identity(e))
        search_id = uuid4().hex
        session = SearchSession(target, issue_id, policy, self.clock() + LIFETIME, selections)
        blocked = self.block_loader()
        with self.lock:
            self._prune()
            while len(self.sessions) >= MAX_SEARCHES:
                del self.sessions[next(iter(self.sessions))]
            self.sessions[search_id] = session
        return {'search_id': search_id, 'state': 'partial' if attempts and errors else 'failed' if not attempts else 'complete',
                'attempts': attempts, 'errors': errors, 'expires_in': LIFETIME,
                'results': [self._preview(key, value, value.raw['link'] in blocked) for key, value in selections.items()]}

    @staticmethod
    def _preview(key, selection, blocked=False):
        e = selection.evaluation
        return {'selection_id': key, 'explanation': preview_explanation(explain_release(e)),
                'blocked': blocked, 'resolver_available': True,
                'download_eligible': not blocked and e.state == Compatibility.COMPATIBLE,
                'force_eligible': force_available(e)}

    def _lookup(self, search_id, selection_id):
        self._prune()
        session = self.sessions.get(search_id)
        if session is None or selection_id not in session.selections:
            raise DDLError('selection_expired')
        selection = session.selections[selection_id]
        e = selection.evaluation
        if (e.target != session.target or e.policy_fingerprint != session.policy.fingerprint
                or evaluation_identity(e) != selection.identity
                or adapt_ddl_result(selection.raw) != e.candidate):
            raise DDLError('selection_mismatch')
        return session, selection

    def select(self, search_id, selection_id, *, force=False, offering_id=None):
        if type(force) is not bool:
            raise DDLError('invalid_action')
        with self.lock:
            session, selected = self._lookup(search_id, selection_id)
            if selected.receipt is not None:
                return selected.receipt
            if selected.claimed:
                raise DDLError('dispatch_already_claimed')
            config = self.sources_loader().get(selected.source.config.id)
            if config is None or config.identity != selected.source.config.identity:
                raise DDLError('source_changed')
            if self.target_loader(session.target.publication.id, session.issue_id) != session.target:
                raise DDLError('target_changed')
            if session.policy != self.policy_loader():
                raise DDLError('policy_changed')
            blocked = self.block_loader()
            if not force and (selected.raw['link'] in blocked or selected.evaluation.state != Compatibility.COMPATIBLE):
                raise DDLError('selection_not_eligible')
            if force and not force_available(selected.evaluation):
                raise DDLError('force_unavailable')
            if selected.force is not None and selected.force != force:
                raise DDLError('authorization_changed')
            selected.force = force
            if not selected.offerings:
                groups = resolve_offerings(selected, blocked)
                candidates = [offering_candidate(selected.evaluation.candidate, g['web_sub_title'], g['size'], key, g.get('source_year'))
                              for key, g in groups.items()]
                evaluations = rank_evaluations(evaluate_releases(session.target, candidates, session.policy))
                by_id = {c.candidate_id: key for c, key in zip(candidates, groups)}
                selected.offerings = {uuid4().hex: (e, groups[by_id[e.candidate.candidate_id]]) for e in evaluations}
            options = {key: value for key, value in selected.offerings.items()
                       if (force and force_available(value[0])) or value[0].state == Compatibility.COMPATIBLE}
            if not options:
                raise DDLError('no_compatible_offering')
            if offering_id is not None:
                if offering_id not in options:
                    raise DDLError('offering_mismatch')
                chosen = options[offering_id]
            elif (len(options) == 1 and not force
                  and next(iter(options.values()))[0].band == selected.evaluation.band):
                chosen = next(iter(options.values()))
            else:
                # A force always shows the actual offering before dispatch.
                return {'state': 'offering_selection_required', 'search_id': search_id,
                        'selection_id': selection_id, 'force': force,
                        'offerings': [{'offering_id': key, 'explanation': preview_explanation(explain_release(e))}
                                      for key, (e, _) in options.items()]}
            # No fallback to another semantic offering, even if its mirror works.
            if self.target_loader(session.target.publication.id, session.issue_id) != session.target:
                raise DDLError('target_changed')
            current_config = self.sources_loader().get(selected.source.config.id)
            if current_config is None or current_config.identity != selected.source.config.identity:
                raise DDLError('source_changed')
            blocked = self.block_loader()
            if not force and selected.raw['link'] in blocked:
                raise DDLError('selection_not_eligible')
            selected.claimed = True
            result = self.dispatch(selected, chosen[0], chosen[1], session.issue_id, force, blocked)
            selected.receipt = {'state': 'dispatched', 'downloads': result,
                                'forced': force, 'original_state': selected.evaluation.state.value}
            return selected.receipt

    def block(self, search_id, selection_id):
        from backend.implementations.blocklist import add_to_blocklist

        with self.lock:
            session, selected = self._lookup(search_id, selection_id)
            add_to_blocklist(web_link=selected.raw['link'], web_title=selected.evaluation.candidate.raw_title,
                web_sub_title=None, download_link=None, download_service=None,
                volume_id=session.target.publication.id, issue_id=session.issue_id,
                reason=BlocklistReason.ADDED_BY_USER)
            return {'blocked': True}


def resolve_offerings(selected, blocked):
    from backend.implementations.download_preppers.ddl.GetComics import (
        _extract_button_links, _extract_list_links)
    from backend.implementations.external_client_manager import ExternalClients

    config = selected.source.config
    html = selected.source.http.fetch(config.url, selected.raw['link'])
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.find('section', {'class': 'post-contents'})
    if body is None:
        raise DDLError('invalid_page')
    torrent = (selected.authorization.get('authorization') != 'automatic'
               and bool(ExternalClients.clients[DownloadType.TORRENT]))
    checker = lambda text, link, available: supported_mirror(text, link, available, config, blocked)
    groups = _extract_button_links(body, torrent, checker, False) + _extract_list_links(body, torrent, checker, False)
    if len(groups) > 100:
        raise DDLError('offering_limit')
    if not groups:
        raise DDLError('no_supported_offering')
    result = {}
    for group in groups:
        if not group['web_sub_title'].strip() or len(group['web_sub_title']) > 16384:
            raise DDLError('invalid_offering')
        # Exact group facts + private links: no title-only release deduplication.
        identity = sha256(repr((selected.evaluation.candidate.candidate_id, group['web_sub_title'],
            group['size'], group.get('source_year'), sorted((k.value, sorted(v)) for k, v in group['links'].items()))).encode()).hexdigest()
        result[identity] = group
    return result


def supported_mirror(text, link, torrent, config, blocked):
    from backend.base.definitions import (GC_DOWNLOAD_SERVICE_TERMS,
                                          GCDownloadService)
    from backend.implementations.direct_download_source import public_url

    if link in blocked:
        return None
    entries = list(GC_DOWNLOAD_SERVICE_TERMS.items())
    entries.sort(key=lambda item: item[0].value.lower() != text)
    for service, terms in entries:
        if not any(term in text for term in terms):
            continue
        if service == GCDownloadService.GETCOMICS_TORRENT and torrent and link.startswith('magnet:?xt=urn:btih:'):
            return service if not any(ord(c) < 32 for c in link) else None
        try:
            host = public_url(link, allow_fragment=service == GCDownloadService.MEGA).hostname
        except DDLError:
            return None
        domains = {
            GCDownloadService.MEGA: ('mega.nz', 'mega.co.nz'),
            GCDownloadService.MEDIAFIRE: ('mediafire.com',),
            GCDownloadService.PIXELDRAIN: ('pixeldrain.com',),
            GCDownloadService.WETRANSFER: ('we.tl', 'wetransfer.com'),
            GCDownloadService.GETCOMICS: (public_url(config.url).hostname, 'getcomics.org', 'getcomics.info'),
            GCDownloadService.GETCOMICS_TORRENT: (public_url(config.url).hostname,),
        }
        if service == GCDownloadService.GETCOMICS_TORRENT and not torrent:
            return None
        return service if any(host == d or host.endswith('.' + d) for d in domains[service]) else None
    return None


def dispatch_offering(selected, evaluation, group, issue_id, force, blocked):
    from asyncio import run

    from backend.features.download_queue import DownloadHandler
    from backend.implementations.direct_download_source import resolve_mirror
    from backend.implementations.download_preppers.ddl.GetComics import \
        GetComicsPrepper

    # Recheck at dispatch as offerings can have been resolved during manual search.
    torrent = selected.authorization.get('authorization') != 'automatic'
    group = {**group, 'links': {service: [link for link in links
        if supported_mirror(service.value.lower(), link, torrent, selected.source.config, blocked) == service]
        for service, links in group['links'].items()}}
    if not any(group['links'].values()):
        raise DDLError('mirrors_unavailable')
    prepper = GetComicsPrepper(selected.raw['link'], selected.source.config.id,
        evaluation.target.publication.id, issue_id, force)
    receipt = {'version': 'ddl-selection/v1', 'issue_id': issue_id,
               'target_ids': list(evaluation.target.issue_ids),
               'candidate_id': selected.evaluation.candidate.candidate_id,
               'evaluation_id': selected.identity, 'offering_id': evaluation.candidate.candidate_id,
               'source': evaluation.candidate.source.key, 'forced': force,
               'original_state': selected.evaluation.state.value}
    receipt.update({k: v for k, v in selected.authorization.items()
                    if k in ('automation_decision_id', 'authorization')})
    async def purify(service, link):
        return resolve_mirror(service, link, lambda candidate: supported_mirror(
            service.value.lower(), candidate, torrent, selected.source.config, blocked) == service)

    downloads = run(prepper.prepare_exact_offering(group, selected.evaluation.candidate.raw_title,
        selected.source.config.services, selected.source.config.avoid_large, receipt, purify))
    if selected.before_dispatch is not None:
        selected.before_dispatch()
    return DownloadHandler().add_resolved(downloads, force)


MANUAL_DDL = ManualDDL()

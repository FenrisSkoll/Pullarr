"""Reviewed Discover adapter to existing DDL, quality and Wanted owners."""

import json
from time import time
from uuid import uuid4

from backend.base.discovery import (DiscoveryError, canonical,
                                    digest, source_url)
from backend.base.release_evaluation import Compatibility
from backend.features.direct_downloads import (configured_sources,
                                               resolve_offerings)
from backend.features.discovery_matching import project
from backend.features.quality import search_quality_context
from backend.features.wanted_automation import WantedAutomation
from backend.features.wanted_search import UNIFIED_SEARCH
from backend.implementations.direct_download_source import (DDLError,
                                                            GetComicsSource,
                                                            offering_candidate)
from backend.implementations.release_explanations import (explain_release,
                                                          preview_explanation)
from backend.implementations.release_scoring import evaluate_release
from backend.internals.collections import transaction
from backend.internals.db import DBConnection, get_db
from backend.internals.discovery import DiscoveryStore
from backend.internals.wanted import WantedConflict


def acquisition(owner, post_id, confirmation=None):
    """All authority is reconstructed server-side; confirmation contains IDs only."""
    c = get_db()
    if confirmation is not None:
        previous = owner.reviews.get(confirmation['preview_id'])
        if (previous and previous['post_id'] == post_id and previous['expires'] >= owner.clock()
                and previous.get('receipt') and previous.get('selected_offering') == confirmation['offering_id']):
            return previous['receipt']
    post = project(c, [DiscoveryStore(c).post(post_id)])[0]
    if post['match'] != 'matched':
        raise DiscoveryError('requires_local_match')
    if post['local']['acquisition']:
        raise DiscoveryError('acquisition_active')
    configs = []
    for value in configured_sources().values():
        try:
            if source_url(value.url).rstrip('/') == 'https://getcomics.org':
                configs.append(value)
        except DiscoveryError:
            continue
    if len(configs) != 1:
        raise DiscoveryError('ddl_source_unavailable')
    config = configs[0]
    local = post['local']
    context = search_quality_context(local['volume_id'], local['issue_id'], c)
    reviews = owner.reviews
    for key, value in tuple(reviews.items()):
        if value['expires'] < owner.clock():
            UNIFIED_SEARCH.close(value['search_id'])
            del reviews[key]
    try:
        if confirmation is not None:
            review = reviews.get(confirmation['preview_id'])
            if review is None or review['post_id'] != post_id:
                raise DiscoveryError('preview_expired')
            if review.get('receipt'):
                return review['receipt']
            session = UNIFIED_SEARCH.lookup(review['search_id'])
            selected = session.ddl.sessions[session.ddl_search].selections[next(iter(session.ddl_ids.values()))]
            if (review['revision'] != post['revision'] or context != session.policy.quality_context
                    or session.target.publication.id != local['volume_id'] or session.issue_id != local['issue_id']):
                raise DiscoveryError('stale_preview')
            UNIFIED_SEARCH.revalidate(session)
            groups = resolve_offerings(selected, session.ddl.block_loader())
            if digest(groups_safe(groups)) != review['offering_digest']:
                raise DiscoveryError('stale_preview')
            offering = confirmation['offering_id']
            if offering not in review['allowed']:
                raise DiscoveryError('selection_not_eligible')
            selected.offerings = {key: (evaluate_release(session.target,
                offering_candidate(session.evaluations[0].candidate, group['web_sub_title'],group['size'],key,group.get('source_year')),
                session.policy), group) for key,group in groups.items()}
            automation = WantedAutomation(DBConnection.default_file)
            try:
                unavailable, _, _ = automation.unavailable(session)
                evaluation = session.evaluations[0]
                if evaluation.candidate.candidate_id in unavailable:
                    raise DiscoveryError('candidate_unavailable')
                if session.run_id is None:
                    session.run_id = automation.store.begin_search(session.target, 'manual')
                    automation.store.finish_search(session.run_id, 'manual_results', sources=session.source_receipts,
                        counts=automation.counts(session), cooldown=False)
                result = automation.grab(session, evaluation, automatic=False, offering_id=offering)
                review['selected_offering'] = offering
                review['receipt'] = result
                return result
            finally:
                automation.close()
        if len(reviews) >= 16:
            raise DiscoveryError('capacity')
        source = GetComicsSource(config, http=owner.transport)
        raw = dict(link=post['url'],display_title=post['title'],size=post['size_bytes'] if post['size_bytes'] is not None else -1,
            indexer_id=config.id,indexer_title=config.name)
        evidence = dict(source='getcomics',post_id=post_id,guid=post['guid'],url=post['url'],title=post['title'],
            revision=post['revision'],year_text=post['year_text'],size_text=post['size_text'])
        search_id, session, selected = UNIFIED_SEARCH.observe_ddl(local['volume_id'],local['issue_id'],raw,source,
            quality_context=context,evidence=evidence)
        try:
            groups = resolve_offerings(selected, session.ddl.block_loader())
            options, allowed = [], []
            for key, group in groups.items():
                candidate = offering_candidate(session.evaluations[0].candidate,group['web_sub_title'],group['size'],key,group.get('source_year'))
                evaluation = evaluate_release(session.target,candidate,session.policy)
                quality = json.loads(evaluation.quality_receipt) if evaluation.quality_receipt else None
                admitted = evaluation.state == Compatibility.COMPATIBLE and (not quality or quality['result'] not in ('not_allowed','equal','downgrade'))
                if admitted:
                    allowed.append(key)
                options.append(dict(offering_id=key,title=candidate.raw_title,quality=quality,allowed=admitted,
                    explanation=preview_explanation(explain_release(evaluation))))
            automation = WantedAutomation(DBConnection.default_file)
            try:
                unavailable, _, _ = automation.unavailable(session)
            finally:
                automation.close()
            listing = session.evaluations[0]
            if listing.state != Compatibility.COMPATIBLE or listing.candidate.candidate_id in unavailable:
                allowed = []
                for option in options:
                    option['allowed'] = False
            identifier = uuid4().hex
            reviews[identifier] = dict(post_id=post_id,revision=post['revision'],search_id=search_id,
                offering_digest=digest(groups_safe(groups)),allowed=allowed,expires=owner.clock()+600)
            safe = dict(offerings=options,reason='candidate_unavailable' if not allowed else None)
            if len(canonical(safe)) > 65536:
                del reviews[identifier]
                raise DiscoveryError('bounded')
            with transaction(c, write=True):
                c.execute('''INSERT INTO discovery_details VALUES(?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET
                    revision=excluded.revision,fetched_at=excluded.fetched_at,digest=excluded.digest,facts=excluded.facts''',
                    (post_id,post['revision'],time(),digest(safe),canonical(safe)))
            return dict(preview_id=identifier,post_id=post_id,revision=post['revision'],**safe)
        except BaseException:
            UNIFIED_SEARCH.close(search_id)
            raise
    except (DDLError, WantedConflict) as error:
        allowed_errors = {'quality_not_allowed','operationally_unavailable','target_changed','source_changed',
            'policy_changed','selection_expired','no_supported_offering','invalid_page','coverage_reserved','reservation_conflict'}
        raise DiscoveryError(str(error) if str(error) in allowed_errors else 'acquisition_unavailable') from None


def groups_safe(groups):
    """Only used as a digest input; links are never durable/API fields."""
    return {key: dict(title=g['web_sub_title'],size=g['size'],year=g.get('source_year'),
        links={k.value: sorted(v) for k,v in g['links'].items()}) for key,g in groups.items()}

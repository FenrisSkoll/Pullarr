"""Read-time Discover projection using existing title/number/evaluator contracts."""

from dataclasses import replace

from backend.base.discovery import DiscoveryError, canonical
from backend.base.issue_facts import NumberCatalog
from backend.base.quality import classify, compare
from backend.base.release_candidate import CoverageKind
from backend.base.release_evaluation import Compatibility, ScoringPolicy
from backend.implementations.identification import MatchingSnapshot, title_key
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.release_candidates import (adapt_ddl_result,
                                                        parse_release_title)
from backend.implementations.release_scoring import (build_wanted_target,
                                                     evaluate_release)
from backend.internals.identification import load_matching_records
from backend.internals.quality import QualityStore


def project(cursor, posts):
    if len(posts) > 500:
        raise DiscoveryError('bounded')
    # This title-only index is a prefilter, never matching authority.
    volumes = cursor.execute('SELECT id,title,alt_title FROM volumes ORDER BY id LIMIT 50001').fetchall()
    if len(volumes) > 50000:
        raise DiscoveryError('matching_scope_bounded')
    index = {}
    for row in volumes:
        for title in (row[1], row[2]):
            if title:
                index.setdefault(title_key(title), set()).add(row[0])
    parsed = [parse_release_title(p['title'])[0] for p in posts]
    scopes = [index.get(title_key(o.series), set()) if o.series else set() for o in parsed]
    ids = sorted(set().union(*scopes)) if scopes else []
    if len(ids) > 1000:
        raise DiscoveryError('matching_scope_bounded')
    snapshot, catalogs, targets = None, {}, {}
    if ids:
        count = cursor.execute('SELECT COUNT(*) FROM issues WHERE volume_id IN (SELECT value FROM json_each(?))', (canonical(ids),)).fetchone()[0]
        if count > 20000:
            raise DiscoveryError('matching_scope_bounded')
        snapshot = MatchingSnapshot.build(*load_matching_records(tuple(PROVIDERS), cursor, volume_ids=ids))
        facts = cursor.execute('''SELECT i.id,i.date,d.year FROM issues i
            LEFT JOIN issue_number_facts n ON n.issue_id=i.id
            LEFT JOIN issue_date_facts d ON d.issue_id=i.id AND d.source_field=n.selected_date_field
            WHERE i.volume_id IN (SELECT value FROM json_each(?))''', (canonical(ids),)).fetchall()
        years = {r[0]: r[2] or int(r[1][:4]) for r in facts if r[2] or r[1] and str(r[1])[:4].isdigit()}
        aliases = {r[0]: r[2] for r in volumes if r[0] in ids and r[2]}
        for vid in ids:
            children = snapshot.children[vid]
            catalogs[vid] = NumberCatalog.build((i.id, i.raw_number, i.number_facts) for i in children)
            if children:
                target = build_wanted_target(snapshot, vid, (children[0].id,), issue_years=years)
                if vid in aliases:
                    target = replace(target, publication=replace(target.publication, aliases=(aliases[vid],)))
                targets[vid] = target
    matched = []
    for post, observation, scope in zip(posts, parsed, scopes):
        item = dict(post, claims=classify(post['title']).preview(), match='unmatched', matches=[], interest='external')
        if post['release_kind'] != 'release':
            item['match'] = post['release_kind']
        elif observation.coverage.kind in (CoverageKind.RANGE, CoverageKind.SET, CoverageKind.PACK, CoverageKind.COLLECTION):
            item['match'] = 'bundle'
        elif observation.coverage.kind == CoverageKind.SINGLE and len(observation.coverage.labels) == 1:
            candidate = adapt_ddl_result(dict(indexer_id=1, indexer_title='GetComics', link=post['url'], display_title=post['title'], size=post['size_bytes'] or -1))
            options = []
            for vid in sorted(scope):
                for iid in catalogs[vid].match(observation.coverage.labels[0]).issue_ids:
                    target = replace(targets[vid], issue_ids=(iid,))
                    evaluation = evaluate_release(target, candidate, ScoringPolicy(reject_owned=False))
                    if evaluation.state == Compatibility.COMPATIBLE:
                        options.append(dict(issue_id=iid, volume_id=vid, title=target.publication.title))
            if len(options) > 20:
                raise DiscoveryError('matching_scope_bounded')
            item['matches'] = options
            item['match'] = 'matched' if len(options) == 1 else 'ambiguous' if options else 'unmatched'
        matched.append(item)
    issue_ids = sorted({i['matches'][0]['issue_id'] for i in matched if i['match'] == 'matched'})
    states = {s['id']: s for s in QualityStore(cursor).issue_states(issue_ids)}
    active = {r[0]: r[1] for r in cursor.execute('''SELECT r.issue_id,d.state FROM wanted_reservations r
        JOIN wanted_decisions d ON d.id=r.decision_id WHERE r.active=1 AND r.issue_id IN (SELECT value FROM json_each(?))''', (canonical(issue_ids),))} if issue_ids else {}
    for item in matched:
        if item['match'] != 'matched':
            continue
        state = states[item['matches'][0]['issue_id']]
        item['local'] = dict(issue_id=state['id'], volume_id=state['volume_id'], direct_owned=state['direct_owned'],
            content_elsewhere=state['content_elsewhere'], monitored=bool(state['monitored'] and state['volume_monitored']),
            reason=state['reason'], acquisition=active.get(state['id']), profile_conflict=state['assignment']['conflict'])
        item['interest'] = ('upgrade' if state['upgrade_eligible'] else 'satisfied' if state['direct_owned'] else
            'missing' if not state['content_owned'] and state['monitored'] and state['volume_monitored'] else 'in_library')
        if state['assignment']['conflict']:
            item['interest'] = 'blocked'
        profile = state['assignment']['profile']
        if profile:
            from backend.base.quality import ClaimedQuality
            current = ClaimedQuality(**state['files'][0]['claims']) if state['direct_owned'] else None
            item['quality'] = compare(profile, classify(item['title']), current=current)
    return matched

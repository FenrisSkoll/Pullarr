"""Date-sitemap hints trigger fresh bounded multi-source wanted searches.

No direct feed grabs: the complete current source set must establish a unique
best choice. Cursors acknowledge durable queued searches, not library ownership.
"""

from dataclasses import replace

from backend.base.release_evaluation import Compatibility
from backend.features.direct_downloads import configured_sources, load_target
from backend.implementations.direct_download_source import (DDLError,
                                                            GetComicsSource)
from backend.implementations.identification import title_key
from backend.implementations.release_scoring import evaluate_release


def discovery_key(config):
    return f'{config.id}:{config.identity}'


def discover_wanted(store, *, sources_loader=configured_sources, source_factory=GetComicsSource,
                    target_loader=load_target, cancelled=lambda: False):
    configs = sources_loader()
    if len(configs) > 4:
        return {'state': 'source_limit'}
    # One source per invocation, deterministic due order; bounded 1000 targets.
    cursors = {r['source_key']: dict(r) for r in store.db.execute('SELECT * FROM wanted_discovery')}
    sources = sorted(configs.values(), key=lambda c: (cursors.get(discovery_key(c), {}).get('next_search', 0), c.id))
    source = next((c for c in sources if cursors.get(discovery_key(c), {}).get('next_search', 0) <= store.clock()), None)
    if source is None or cancelled():
        return {'state': 'idle'}
    eligible = store.due(limit=1000, ignore_cooldown=True)
    if not eligible:
        return {'state': 'idle'}
    # Refuse truncated wanted scope rather than advancing past unchecked hints.
    count = store.db.execute('''SELECT COUNT(*) FROM issues i JOIN volumes v ON v.id=i.volume_id
        WHERE i.monitored=1 AND v.monitored=1 AND NOT EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id)
        AND NOT EXISTS(SELECT 1 FROM wanted_reservations r WHERE r.issue_id=i.id AND r.active=1)''').fetchone()[0]
    if count > 1000:
        return {'state': 'wanted_limit_periodic_search_remains_available'}
    key = discovery_key(source)
    old = cursors.get(key, {})
    started = store.clock()
    cursor = old.get('cursor', max(0, started - 172800))
    try:
        candidates = source_factory(source).discover(max(0, cursor - 172800) // 86400 * 86400)
        seen = {r[0] for r in store.db.execute('SELECT candidate_id FROM wanted_discovery_seen WHERE source_key=?', (key,))}
        candidates = tuple(c for c in candidates if c.candidate_id not in seen)
        by_title = {}
        for candidate in candidates:
            for name in {title_key(o.series) for o in candidate.observations if o.series}:
                by_title.setdefault(name, []).append(candidate)
        grouped = {}
        for row in eligible:
            grouped.setdefault(row['volume_id'], []).append(row['id'])
        titles = {r[0]: (r[1], r[2]) for r in store.db.execute('SELECT id,title,alt_title FROM volumes WHERE monitored=1')}
        grouped = {volume: ids for volume, ids in grouped.items()
                   if any(title_key(name) in by_title for name in titles[volume] if name)}
        if len(grouped) > 64:
            raise DDLError('discovery_target_limit')
        queued = set()
        for volume, ids in sorted(grouped.items()):
            if cancelled():
                raise DDLError('cancelled')
            target = target_loader(volume, ids[0])
            plausible = {c.candidate_id: c for name in (target.publication.title, *target.publication.aliases)
                         for c in by_title.get(title_key(name), ())}
            for issue in ids:
                hypothesis = replace(target, issue_ids=(issue,))
                if any(evaluate_release(hypothesis, c).state == Compatibility.COMPATIBLE for c in plausible.values()):
                    queued.add(issue)
        with store.transaction():
            store.db.executemany('INSERT INTO wanted_discovery_seen VALUES(?,?,?) ON CONFLICT DO NOTHING',
                ((key, c.candidate_id, started) for c in candidates))
            store.db.execute('DELETE FROM wanted_discovery_seen WHERE observed_at<?', (started - 7 * 86400,))
            store.db.executemany('''INSERT INTO wanted_schedule(issue_id,next_search,requested) VALUES(?,0,2)
                ON CONFLICT(issue_id) DO UPDATE SET next_search=0,requested=CASE WHEN requested=1 THEN 1 ELSE 2 END''',
                ((issue,) for issue in sorted(queued)))
            store.db.execute('''INSERT INTO wanted_discovery VALUES(?,?,?,NULL)
                ON CONFLICT(source_key) DO UPDATE SET cursor=excluded.cursor,next_search=excluded.next_search,error=NULL''',
                (key, started, started + 1800))
        return {'state': 'complete', 'queued': len(queued)}
    except DDLError as error:
        with store.transaction():
            store.db.execute('''INSERT INTO wanted_discovery VALUES(?,?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET next_search=excluded.next_search,error=excluded.error''',
                (key, cursor, started + 1800, error.code))
        return {'state': 'failed', 'reason': error.code}

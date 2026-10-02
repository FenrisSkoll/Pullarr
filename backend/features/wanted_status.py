"""Read-only lifecycle presentation, derived from authoritative component state."""

import json

from backend.internals.wanted import EXTERNAL_HOLD, WantedConflict


def wanted_rows(store, *, offset=0, state=None):
    if type(offset) is not int or offset < 0 or offset > 1000000:
        raise WantedConflict('invalid_offset')
    has_quality = getattr(store, 'has_quality', None)
    if has_quality is None:
        has_quality = bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='quality_upgrade_issues'").fetchone())
    upgrade_clause = "WHEN i.id IN (SELECT issue_id FROM quality_upgrade_issues) AND d.id IS NULL THEN 'upgrade'" if has_quality else ''
    rows = store.db.execute(f'''SELECT i.id,i.volume_id,i.issue_number,v.title,i.monitored issue_monitored,
        v.monitored volume_monitored,EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id) owned,
        s.last_search,s.next_search,ls.outcome last_search_outcome,ls.error last_search_error,
        d.id decision_id,d.state decision_state,d.error,
        CASE {upgrade_clause}
             WHEN EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id) AND d.id IS NULL THEN 'owned'
             WHEN d.state='review' THEN 'review'
             WHEN d.id IS NOT NULL AND EXISTS(
                 SELECT 1 FROM wanted_acquisitions a JOIN acquisition_intakes t
                 ON t.kind=a.kind AND t.download_id=a.acquisition_id WHERE a.decision_id=d.id) THEN 'importing'
             WHEN d.id IS NOT NULL THEN 'downloading'
             WHEN NOT(1 {EXTERNAL_HOLD}) THEN 'existing_acquisition'
             WHEN EXISTS(SELECT 1 FROM wanted_searches run,json_each(run.issue_ids) member
                 WHERE run.state='searching' AND member.value=i.id) THEN 'searching'
             WHEN COALESCE(s.next_search,0)>? THEN 'cooldown' ELSE 'due' END lifecycle
        FROM issues i JOIN volumes v ON v.id=i.volume_id
        LEFT JOIN wanted_schedule s ON s.issue_id=i.id
        LEFT JOIN wanted_searches ls ON ls.id=s.last_search
        LEFT JOIN wanted_reservations r ON r.issue_id=i.id AND r.active=1
        LEFT JOIN wanted_decisions d ON d.id=r.decision_id
        WHERE ((i.monitored=1 AND v.monitored=1) OR d.id IS NOT NULL)
        AND (? IS NULL OR lifecycle=?)
        ORDER BY v.title,v.id,i.id LIMIT 100 OFFSET ?''', (store.clock(), state, state, offset)).fetchall()
    result = [dict(r) for r in rows]
    quality_states = {}
    if has_quality and result:
        from backend.internals.quality import QualityStore
        quality_states = {r['id']:r for r in QualityStore(store.db.cursor()).issue_states([r['id'] for r in result])}
    # One bounded relation query for visible decisions, not per-issue lookups.
    ids = {r['decision_id'] for r in result if r['decision_id']}
    links = {}
    if ids:
        slots = ','.join('?' for _ in ids)
        for row in store.db.execute(f'''SELECT a.decision_id,a.kind,a.acquisition_id,t.id intake_id,t.state intake_state
            FROM wanted_acquisitions a LEFT JOIN acquisition_intakes t
            ON t.kind=a.kind AND t.download_id=a.acquisition_id WHERE a.decision_id IN ({slots})''', tuple(ids)):
            links.setdefault(row['decision_id'], []).append(dict(row))
    for row in result:
        row['wanted'] = bool(row['issue_monitored'] and row['volume_monitored'] and not row['owned'])
        quality = quality_states.get(row['id'])
        row['acquisition_reason'] = 'upgrade' if quality and quality['upgrade_eligible'] else 'missing' if row['wanted'] else None
        row['quality'] = quality
        if quality and quality['upgrade_eligible']:
            row['wanted'] = True
            if row['lifecycle'] == 'owned':
                row['lifecycle'] = 'upgrade'
        row['acquisitions'] = links.get(row['decision_id'], [])
    return result


def search_history(store, volume_id=None):
    rows = store.db.execute('''SELECT * FROM wanted_searches WHERE (? IS NULL OR volume_id=?)
        ORDER BY started_at DESC,id LIMIT 100''', (volume_id, volume_id)).fetchall()
    result = []
    decisions = {}
    if rows:
        slots = ','.join('?' for _ in rows)
        for decision in store.db.execute(f'SELECT * FROM wanted_decisions WHERE search_id IN ({slots})', tuple(r['id'] for r in rows)):
            value = dict(decision)
            value['issue_ids'], value['quality'] = json.loads(value['issue_ids']), json.loads(value['quality'])
            decisions.setdefault(value['search_id'], []).append(value)
    for row in rows:
        value = dict(row)
        for key in ('issue_ids', 'source_receipt', 'counts'):
            value[key] = json.loads(value[key])
        value['decisions'] = decisions.get(value['id'], [])
        result.append(value)
    return result

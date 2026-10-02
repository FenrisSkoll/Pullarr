"""Local graph persistence/read service. No file ownership or metadata writes."""

import json
from dataclasses import asdict, fields
from functools import wraps
from hashlib import sha256
from uuid import uuid4

from backend.base.reprint_graph import POLICY, GraphConflict

_UNSET = object()


def consistent_read(function):
    """One coherent local graph version even if explicit sync commits mid-read."""
    @wraps(function)
    def read(cursor, *args, **kwargs):
        cursor.execute('SAVEPOINT graph_view')
        try:
            return function(cursor, *args, **kwargs)
        finally:
            cursor.execute('RELEASE graph_view')
    return read


EDGE_PAGE = '''SELECT * FROM bibliographic_reprint_edges
    WHERE provider=? AND origin_issue=?
    UNION ALL SELECT * FROM bibliographic_reprint_edges
    WHERE provider=? AND target_issue=?
    ORDER BY provider_id LIMIT ? OFFSET ?'''


def seeds(cursor, provider='gcd'):
    """Selected authority only, never cross-reference based enrichment."""
    return {r[0]: (r[1], r[2]) for r in cursor.execute('''
        SELECT e.provider_id,vx.provider_id,i.id FROM issues i
        JOIN volumes v ON v.id=i.volume_id
        JOIN issue_external_ids e ON e.issue_id=i.id AND e.provider=v.metadata_provider
        JOIN volume_external_ids vx ON vx.volume_id=v.id AND vx.provider=v.metadata_provider
        WHERE v.metadata_provider=? ORDER BY i.id LIMIT 10001''', (provider,))}


def persist(cursor, snapshot, expected_seeds, *, revision=_UNSET):
    """Acquire externally first; atomically reconcile only incident seed scope."""
    cursor.execute('SAVEPOINT catalog_graph')
    try:
        if revision is not _UNSET and cursor.execute('SELECT MAX(rowid) FROM bibliographic_graph_snapshots').fetchone()[0] != revision:
            raise GraphConflict('Catalog graph changed during acquisition')
        provider = snapshot.provider
        current = seeds(cursor, provider)
        if current != expected_seeds or set(snapshot.seeds) != set(current):
            raise GraphConflict('Catalog seed authority changed')
        hasher = sha256()
        for key in ('issues', 'stories', 'creators', 'names', 'credits', 'edges'):
            hasher.update(key.encode() + b'\n')
            for record in getattr(snapshot, key):
                hasher.update(json.dumps(asdict(record), sort_keys=True, ensure_ascii=True,
                                         separators=(',', ':')).encode() + b'\n')
        digest = hasher.hexdigest()
        sid = str(uuid4())
        cursor.execute('''INSERT INTO bibliographic_graph_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (sid, provider, POLICY, snapshot.source_policy, snapshot.source_fingerprint, snapshot.observed_at,
             len(snapshot.seeds), len(snapshot.edges), len(snapshot.stories), len(snapshot.creators),
             sum(r.provider_id not in current for r in snapshot.issues), digest))
        # Source-neutral normalized tables. Fixed table names and dataclass field
        # names, never remote identifiers as SQL structure.
        for table, records in (
            ('bibliographic_issue_refs', snapshot.issues),
            ('bibliographic_story_entities', snapshot.stories),
            ('bibliographic_creator_entities', snapshot.creators),
            ('bibliographic_creator_names', snapshot.names),
            ('bibliographic_story_credits', snapshot.credits),
            ('bibliographic_reprint_edges', snapshot.edges),
        ):
            if not records:
                continue
            columns = ['provider', *(f.name for f in fields(records[0])), 'snapshot_id']
            statement = f'INSERT INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)})'
            statement += ' ON CONFLICT(provider,provider_id) DO UPDATE SET ' + ','.join(
                f'{c}=excluded.{c}' for c in columns[2:])
            cursor.executemany(statement, ((provider, *asdict(r).values(), sid) for r in records))
        for offset in range(0, len(snapshot.seeds), 400):
            batch = snapshot.seeds[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            cursor.execute(f'''DELETE FROM bibliographic_reprint_edges WHERE provider=? AND snapshot_id!=?
                AND (origin_issue IN ({marks}) OR target_issue IN ({marks}))''',
                (provider, sid, *batch, *batch))
            # Physical absence in the complete supplied scope deactivates stable
            # identities; it never reassigns them or deletes REST observations.
            cursor.execute(f'''UPDATE bibliographic_story_entities SET deleted=1 WHERE provider=?
                AND issue_id IN ({marks}) AND snapshot_id!=?''', (provider, *batch, sid))
        story_ids = tuple(r.provider_id for r in snapshot.stories)
        for offset in range(0, len(story_ids), 400):
            batch = story_ids[offset:offset + 400]
            cursor.execute('''UPDATE bibliographic_story_credits SET deleted=1 WHERE provider=?
                AND snapshot_id!=? AND story_id IN (''' + ','.join('?' for _ in batch) + ')',
                (provider, sid, *batch))
        cursor.executemany('''INSERT INTO bibliographic_graph_scopes VALUES(?,?,?)
            ON CONFLICT(provider,issue_id) DO UPDATE SET snapshot_id=excluded.snapshot_id''',
            ((provider, iid, sid) for iid in snapshot.seeds))
        if cursor.execute('''SELECT 1 FROM bibliographic_reprint_edges e
            LEFT JOIN bibliographic_story_entities o ON o.provider=e.provider AND o.provider_id=e.origin_story
            LEFT JOIN bibliographic_story_entities t ON t.provider=e.provider AND t.provider_id=e.target_story
            WHERE e.provider=? AND (o.issue_id!=e.origin_issue OR t.issue_id!=e.target_issue) LIMIT 1''',
                (provider,)).fetchone():
            raise GraphConflict('Existing graph endpoint contradicts new catalog parent')
        cursor.execute('RELEASE catalog_graph')
        return dict(snapshot_id=sid, digest=digest, seeds=len(snapshot.seeds), edges=len(snapshot.edges),
                    stories=len(snapshot.stories), creators=len(snapshot.creators),
                    catalog_selects=snapshot.select_count)
    except BaseException:
        cursor.execute('ROLLBACK TO catalog_graph')
        cursor.execute('RELEASE catalog_graph')
        raise


@consistent_read
def issue_graph(cursor, local_id, offset=0, limit=100):
    """Paginated direct edges and their identities, bounded queries; no catalog IO."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid graph page')
    owner = cursor.execute('''SELECT e.provider,e.provider_id FROM issues i JOIN volumes v ON v.id=i.volume_id
        JOIN issue_external_ids e ON e.issue_id=i.id AND e.provider=v.metadata_provider WHERE i.id=?''',
        (local_id,)).fetchone()
    result = dict(schema='issue-reprint-graph/v1', issue_id=local_id, available=False,
                  incoming=[], outgoing=[], issues=[], stories=[], credits=[], snapshot=None,
                  offset=offset, next_offset=None, semantics='material_reprint_evidence_only')
    if owner is None:
        return result
    provider, iid = owner
    receipt = cursor.execute('''SELECT s.* FROM bibliographic_graph_scopes g
        JOIN bibliographic_graph_snapshots s ON s.id=g.snapshot_id WHERE g.provider=? AND g.issue_id=?''',
        (provider, iid)).fetchone()
    if receipt is None:
        return result
    result.update(available=True, snapshot=dict(receipt))
    # Same-issue edges are forbidden, so these endpoint sets are disjoint.
    # UNION ALL permits a merge of the two scoped indexes; an OR+ORDER BY
    # otherwise chooses a provider-wide primary-key scan on SQLite.
    edges = [dict(r) for r in cursor.execute(EDGE_PAGE,
        (provider, iid, provider, iid, limit + 1, offset))]
    if len(edges) > limit:
        result['next_offset'] = offset + limit
    edges = edges[:limit]
    issue_ids = {iid}
    story_ids = set()
    for edge in edges:
        edge['shape'] = ('story' if edge['origin_story'] else 'issue') + '_to_' + ('story' if edge['target_story'] else 'issue')
        result['incoming' if edge['target_issue'] == iid else 'outgoing'].append(edge)
        issue_ids.update((edge['origin_issue'], edge['target_issue']))
        story_ids.update(s for s in (edge['origin_story'], edge['target_story']) if s is not None)
    marks = ','.join('?' for _ in issue_ids)
    # Dynamic mapping has no stale FK and never grants authority to xrefs.
    result['issues'] = [dict(r) for r in cursor.execute(f'''SELECT r.*,i.id AS local_issue_id,i.volume_id
        FROM bibliographic_issue_refs r LEFT JOIN issue_external_ids e
        ON e.provider=r.provider AND e.provider_id=r.provider_id
        LEFT JOIN issues i ON i.id=e.issue_id AND EXISTS(SELECT 1 FROM volumes v
            WHERE v.id=i.volume_id AND v.metadata_provider=r.provider)
        WHERE r.provider=? AND r.provider_id IN ({marks}) ORDER BY r.provider_id''', (provider, *issue_ids))]
    if story_ids:
        marks = ','.join('?' for _ in story_ids)
        result['stories'] = [dict(r) for r in cursor.execute(f'''SELECT * FROM bibliographic_story_entities
            WHERE provider=? AND provider_id IN ({marks}) ORDER BY provider_id''', (provider, *story_ids))]
        # Bound independent of malformed/high-fanout credits. Pagination of edges
        # does not authorize arbitrarily large nested arrays.
        result['credits'] = [dict(r) for r in cursor.execute(f'''SELECT c.*,n.creator_id,n.name AS name_text,
            n.official,n.name_type_id,n.deleted AS name_deleted,e.name AS creator_name,e.deleted AS creator_deleted
            FROM bibliographic_story_credits c INDEXED BY graph_credit_story JOIN bibliographic_creator_names n
            ON n.provider=c.provider AND n.provider_id=c.name_id JOIN bibliographic_creator_entities e
            ON e.provider=n.provider AND e.provider_id=n.creator_id
            WHERE c.provider=? AND c.story_id IN ({marks}) ORDER BY c.provider_id LIMIT 1001''', (provider, *story_ids))]
        result['credits_truncated'] = len(result['credits']) > 1000
        result['credits'] = result['credits'][:1000]
    issues = {r['provider_id']: r for r in result['issues']}
    stories = {r['provider_id']: r for r in result['stories']}
    for edge in edges:
        edge['active'] = not any(issues[i]['deleted'] or (s is not None and stories[s]['deleted'])
            for i, s in ((edge['origin_issue'], edge['origin_story']), (edge['target_issue'], edge['target_story'])))
    return result


@consistent_read
def issue_stories(cursor, local_id, offset=0):
    """Stable seed story inventory; separate from C1 observation rows and edges."""
    if type(offset) is not int or offset < 0:
        raise ValueError('Invalid story page')
    owner = cursor.execute('''SELECT e.provider,e.provider_id,g.snapshot_id FROM issues i
        JOIN volumes v ON v.id=i.volume_id JOIN issue_external_ids e ON e.issue_id=i.id AND e.provider=v.metadata_provider
        LEFT JOIN bibliographic_graph_scopes g ON g.provider=e.provider AND g.issue_id=e.provider_id
        WHERE i.id=?''', (local_id,)).fetchone()
    result = dict(schema='issue-catalog-stories/v1', available=bool(owner and owner[2]), stories=[],
                  credits=[], offset=offset, next_offset=None)
    if not result['available']:
        return result
    provider, iid, _ = owner
    rows = [dict(r) for r in cursor.execute('''SELECT * FROM bibliographic_story_entities
        WHERE provider=? AND issue_id=? ORDER BY sequence,provider_id LIMIT 101 OFFSET ?''', (provider, iid, offset))]
    if len(rows) > 100:
        result['next_offset'] = offset + 100
    result['stories'] = rows[:100]
    ids = [r['provider_id'] for r in result['stories']]
    if ids:
        result['credits'] = [dict(r) for r in cursor.execute('''SELECT c.*,n.creator_id,n.name AS name_text,
            n.official,n.name_type_id,n.deleted AS name_deleted,e.name AS creator_name,e.deleted AS creator_deleted
            FROM bibliographic_story_credits c INDEXED BY graph_credit_story JOIN bibliographic_creator_names n
            ON n.provider=c.provider AND n.provider_id=c.name_id JOIN bibliographic_creator_entities e
            ON e.provider=n.provider AND e.provider_id=n.creator_id
            WHERE c.provider=? AND c.story_id IN (''' + ','.join('?' for _ in ids)
            + ') ORDER BY c.provider_id LIMIT 1001', (provider, *ids))]
    result['credits_truncated'] = len(result['credits']) > 1000
    result['credits'] = result['credits'][:1000]
    return result

"""Local-only content review and DB-only, explicit operator mutations.

No filesystem, catalog, provider or organization imports belong in this module.
Savepoints preserve caller-owned transactions and roll back whole mutations.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
from time import time
from uuid import uuid4

from backend.base.content_claims import (POLICY, ClaimKind, ContentConflict,
                                         ContentEvidenceEvaluation,
                                         EvidenceReceipt, PublicationRef)

PAGE_SIZE = 100
EDGE_LIMIT = 1000


@contextmanager
def transaction(cursor):
    cursor.execute('SAVEPOINT content_operation')
    try:
        yield
        cursor.execute('RELEASE content_operation')
    except BaseException as error:
        cursor.execute('ROLLBACK TO content_operation')
        cursor.execute('RELEASE content_operation')
        if isinstance(error, sqlite3.OperationalError) and (getattr(error, 'sqlite_errorcode', 0) & 255) in (5, 6):
            raise ContentConflict('Concurrent database change; reload the preview and retry') from None
        raise


def rows(cursor, sql, params=()):
    cursor.execute(sql, params)
    names = tuple(column[0] for column in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True).encode()).hexdigest()


def exact_local(cursor, ref):
    row = cursor.execute('''SELECT i.id FROM issues i JOIN volumes v ON v.id=i.volume_id
        JOIN issue_external_ids x ON x.issue_id=i.id AND x.provider=v.metadata_provider
        WHERE x.provider=? AND x.provider_id=?''', (ref.provider, ref.provider_id)).fetchone()
    return row[0] if row else None


def local_ref(cursor, issue_id):
    row = cursor.execute('''SELECT x.provider,x.provider_id FROM issues i
        JOIN volumes v ON v.id=i.volume_id
        JOIN issue_external_ids x ON x.issue_id=i.id AND x.provider=v.metadata_provider
        WHERE i.id=?''', (issue_id,)).fetchone()
    if row is None:
        raise ContentConflict('Exact selected-authority issue identity unavailable')
    return PublicationRef(*row)


def descriptor(cursor, ref):
    local = rows(cursor, '''SELECT i.id AS local_issue_id,i.volume_id,v.title AS series_name,
        i.issue_number AS number,i.title,vx.provider_id AS series_provider_id
        FROM issues i JOIN volumes v ON v.id=i.volume_id
        JOIN issue_external_ids x ON x.issue_id=i.id AND x.provider=v.metadata_provider
        LEFT JOIN volume_external_ids vx ON vx.volume_id=v.id AND vx.provider=v.metadata_provider
        WHERE x.provider=? AND x.provider_id=?''', (ref.provider, ref.provider_id))
    graph = rows(cursor, '''SELECT series_id,series_name,number,title,deleted,snapshot_id
        FROM bibliographic_issue_refs WHERE provider=? AND provider_id=?''',
        (ref.provider, ref.provider_id))
    if not local and not graph:
        raise ContentConflict('Source must have an exact local identity or persisted graph reference')
    if local and graph and local[0]['series_provider_id'] != graph[0]['series_id']:
        raise ContentConflict('Catalog and selected local parent identities disagree; review metadata first')
    result = dict(local[0] if local else graph[0])
    result.update(asdict(ref))
    result.setdefault('local_issue_id', None)
    result.setdefault('volume_id', None)
    result['graph_deleted'] = bool(graph[0]['deleted']) if graph else None
    return result


def evaluate(cursor, target, source):
    """Pure domain evaluation over persisted evidence; bounded, never complete."""
    if target.provider != source.provider:
        return ContentEvidenceEvaluation(source, target, ())
    values = rows(cursor, '''SELECT e.provider,e.provider_id AS edge_id,e.snapshot_id,
        e.origin_issue,e.target_issue,e.origin_story,e.target_story,
        NOT (a.deleted OR b.deleted OR COALESCE(os.deleted,0) OR COALESCE(ts.deleted,0)) AS active
        FROM bibliographic_reprint_edges e
        JOIN bibliographic_issue_refs a ON a.provider=e.provider AND a.provider_id=e.origin_issue
        JOIN bibliographic_issue_refs b ON b.provider=e.provider AND b.provider_id=e.target_issue
        LEFT JOIN bibliographic_story_entities os ON os.provider=e.provider AND os.provider_id=e.origin_story
        LEFT JOIN bibliographic_story_entities ts ON ts.provider=e.provider AND ts.provider_id=e.target_story
        WHERE e.provider=? AND e.target_issue=? AND e.origin_issue=?
        ORDER BY e.provider_id LIMIT ?''',
        (target.provider, target.provider_id, source.provider_id, EDGE_LIMIT + 1))
    if len(values) > EDGE_LIMIT:
        raise ContentConflict('Evidence exceeds review limit; use explicit no-evidence confirmation')
    return ContentEvidenceEvaluation(source, target, tuple(
        EvidenceReceipt(**dict(value, active=bool(value['active']))) for value in values))


def _claim_preview(cursor, target_id, source, kind, manual):
    kind = ClaimKind(kind)
    target = local_ref(cursor, target_id)
    if target == source:
        raise ContentConflict('A publication cannot assert coverage of itself')
    target_data, source_data = descriptor(cursor, target), descriptor(cursor, source)
    evaluation = ContentEvidenceEvaluation(source, target, ()) if manual else evaluate(cursor, target, source)
    old = cursor.execute('''SELECT id FROM bibliographic_content_claims
        WHERE target_provider=? AND target_provider_id=? AND source_provider=? AND source_provider_id=?
        AND retired_at IS NULL''', (target.provider, target.provider_id, source.provider, source.provider_id)).fetchone()
    affected = cursor.execute('SELECT COUNT(*) FROM valid_file_content_coverage WHERE claim_id=?',
                              (old[0] if old else None,)).fetchone()[0]
    value = {'schema': 'content-claim-preview/v1', 'target': target_data, 'source': source_data,
             'kind': kind.value, 'authority': 'operator_confirmed', 'policy': POLICY,
             'evidence_mode': 'manual_without_graph' if manual else 'reviewed_graph',
             'evidence_outcome': evaluation.outcome.value,
             'evidence': [dict(asdict(edge), shape=edge.shape) for edge in evaluation.edges],
             'supersedes': old[0] if old else None, 'prior_coverage_retired': affected,
             'ownership_effect': 'retires_prior_claim_coverage' if affected else 'none_until_explicit_file_application',
             'warning': 'GCD reprint evidence does not prove completeness. Complete is your explicit ownership-policy assertion.'}
    value['preview_token'] = digest(value)
    return value


def claim_preview(cursor, target_id, source, kind, *, manual=False):
    with transaction(cursor):
        return _claim_preview(cursor, target_id, source, kind, manual)


def confirm_claim(cursor, target_id, source, kind, token, *, manual=False, now=None):
    """Explicit confirmation only. Does not apply or reactivate file coverage."""
    with transaction(cursor):
        value = _claim_preview(cursor, target_id, source, kind, manual)
        if token != value['preview_token']:
            raise ContentConflict('Claim preview changed; review the exact publications and evidence again')
        stamp = time() if now is None else now
        old = value['supersedes']
        if old:
            _retire_claim(cursor, old, stamp)
        claim_id = str(uuid4())
        target = local_ref(cursor, target_id)
        cursor.execute('''INSERT INTO bibliographic_content_claims VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (claim_id, target.provider, target.provider_id, source.provider, source.provider_id,
             ClaimKind(kind).value, 'operator_confirmed', POLICY, stamp, None, old))
        cursor.executemany('''INSERT INTO bibliographic_content_claim_evidence
            VALUES(?,?,?,?,?,?,?,?,?)''', ((claim_id, edge['provider'], edge['edge_id'], edge['snapshot_id'],
                edge['origin_issue'], edge['target_issue'], edge['origin_story'], edge['target_story'], edge['active'])
                for edge in value['evidence']))
        return claim_id


def _retire_claim(cursor, claim_id, stamp):
    cursor.execute('UPDATE bibliographic_content_claims SET retired_at=? WHERE id=? AND retired_at IS NULL',
                   (stamp, claim_id))
    cursor.execute('UPDATE file_content_coverage SET retired_at=? WHERE claim_id=? AND retired_at IS NULL',
                   (stamp, claim_id))


def _coverage_preview(cursor, target_id, file_id, claim_ids):
    if not claim_ids or len(claim_ids) > PAGE_SIZE or len(set(claim_ids)) != len(claim_ids):
        raise ContentConflict('Select 1–100 distinct exact claims')
    target = local_ref(cursor, target_id)
    file = rows(cursor, '''SELECT f.id,f.filepath FROM active_files f JOIN issues_files d ON d.file_id=f.id
        WHERE f.id=? AND d.issue_id=?''', (file_id, target_id))
    if not file:
        raise ContentConflict('File must directly represent the selected target publication')
    claims = rows(cursor, '''SELECT k.*,i.id AS local_source_id,i.volume_id AS source_volume_id,
        i.issue_number AS source_number,i.title AS source_title,v.title AS source_series,
        g.deleted AS graph_deleted,g.series_id AS graph_series_id,vx.provider_id AS source_series_id,
        c.id AS existing_coverage_id,c.claim_id AS existing_claim_id
        FROM bibliographic_content_claims k
        LEFT JOIN issue_external_ids sx ON sx.provider=k.source_provider AND sx.provider_id=k.source_provider_id
        LEFT JOIN issues i ON i.id=sx.issue_id AND EXISTS(
            SELECT 1 FROM volumes authority WHERE authority.id=i.volume_id AND authority.metadata_provider=k.source_provider)
        LEFT JOIN volumes v ON v.id=i.volume_id
        LEFT JOIN volume_external_ids vx ON vx.volume_id=v.id AND vx.provider=k.source_provider
        LEFT JOIN bibliographic_issue_refs g ON g.provider=k.source_provider AND g.provider_id=k.source_provider_id
        LEFT JOIN file_content_coverage c ON c.file_id=? AND c.source_issue_id=i.id AND c.retired_at IS NULL
        WHERE k.id IN (''' + ','.join('?' for _ in claim_ids) + ') ORDER BY k.id', (file_id, *claim_ids))
    if len(claims) != len(claim_ids):
        raise ContentConflict('Claim is unavailable')
    sources = []
    for claim in claims:
        if (claim['retired_at'] is not None or claim['kind'] != ClaimKind.COMPLETE.value
                or claim['authority'] != 'operator_confirmed' or claim['policy'] != POLICY
                or PublicationRef(claim['target_provider'], claim['target_provider_id']) != target):
            raise ContentConflict('Only current complete claims for this exact target are eligible')
        if claim['local_source_id'] is None:
            raise ContentConflict('External source has no selected-authority local mapping; apply after explicit local add')
        if claim['graph_series_id'] is not None and claim['graph_series_id'] != claim['source_series_id']:
            raise ContentConflict('Source catalog and local parent identities disagree')
        if claim['existing_coverage_id'] and claim['existing_claim_id'] != claim['id']:
            raise ContentConflict('File already has active coverage for this source; retire it before changing authority')
        sources.append({'provider': claim['source_provider'], 'provider_id': claim['source_provider_id'],
            'local_issue_id': claim['local_source_id'], 'volume_id': claim['source_volume_id'],
            'series_name': claim['source_series'], 'number': claim['source_number'], 'title': claim['source_title'],
            'graph_deleted': claim['graph_deleted'], 'claim_id': claim['id'], 'coverage_id': claim['existing_coverage_id']})
    value = {'schema': 'content-coverage-preview/v1', 'file': file[0],
             'target': descriptor(cursor, target), 'sources': sources,
             'effects': {'move': False, 'rename': False, 'comicinfo_write': False,
                         'direct_association_change': False, 'source_counts_as_owned': True}}
    value['preview_token'] = digest(value)
    return value


def coverage_preview(cursor, target_id, file_id, claim_ids):
    with transaction(cursor):
        return _coverage_preview(cursor, target_id, file_id, claim_ids)


def apply_coverage(cursor, target_id, file_id, claim_ids, token, *, now=None):
    with transaction(cursor):
        value = _coverage_preview(cursor, target_id, file_id, claim_ids)
        if token != value['preview_token']:
            raise ContentConflict('Coverage preview changed; review again')
        stamp = time() if now is None else now
        result = []
        for source in value['sources']:
            cid = source['coverage_id'] or str(uuid4())
            if source['coverage_id'] is None:
                cursor.execute('INSERT INTO file_content_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (cid, file_id, target_id, source['local_issue_id'], source['claim_id'], file_id,
                     target_id, source['local_issue_id'], POLICY, stamp, None))
            result.append(cid)
        return result


def retirement_preview(cursor, identifier, *, coverage=False):
    # Identifiers below are internal fixed choices, never request SQL.
    with transaction(cursor):
        table = 'file_content_coverage' if coverage else 'bibliographic_content_claims'
        current = rows(cursor, f'SELECT * FROM {table} WHERE id=?', (identifier,))
        if not current:
            raise ContentConflict('Record unavailable')
        affected = rows(cursor, '''SELECT source_issue_id,target_issue_id,COUNT(DISTINCT file_id) AS files
            FROM valid_file_content_coverage WHERE ''' +
                        ('id=?' if coverage else 'claim_id=?') +
                        ' GROUP BY source_issue_id,target_issue_id ORDER BY source_issue_id,target_issue_id', (identifier,))
        value = {'schema': 'content-retirement-preview/v1', 'record': current[0], 'coverage': coverage,
                 'affected': affected, 'warning': 'Source issues may become Wanted again. No file or direct association is removed.'}
        value['preview_token'] = digest(value)
        return value


def retire(cursor, identifier, token, *, coverage=False, now=None):
    with transaction(cursor):
        preview = retirement_preview(cursor, identifier, coverage=coverage)
        if preview['preview_token'] != token:
            raise ContentConflict('Retirement preview changed; review ownership impact again')
        stamp = time() if now is None else now
        if coverage:
            cursor.execute('UPDATE file_content_coverage SET retired_at=? WHERE id=? AND retired_at IS NULL',
                           (stamp, identifier))
        else:
            _retire_claim(cursor, identifier, stamp)


def contents(cursor, target_id, *, offset=0):
    """One opt-in page, bounded independently of graph/story/library size."""
    if type(offset) is not int or offset < 0 or offset > 1000000:
        raise ContentConflict('Invalid content page')
    with transaction(cursor):
        target = local_ref(cursor, target_id)
        candidates = rows(cursor, '''SELECT g.provider,g.provider_id,g.series_name,g.number,g.title,g.deleted,
            COUNT(e.provider_id) AS edge_count,i.id AS local_issue_id,i.volume_id
            FROM bibliographic_reprint_edges e JOIN bibliographic_issue_refs g
            ON g.provider=e.provider AND g.provider_id=e.origin_issue
            LEFT JOIN issue_external_ids x ON x.provider=g.provider AND x.provider_id=g.provider_id
            LEFT JOIN issues i ON i.id=x.issue_id AND EXISTS(SELECT 1 FROM volumes v
                WHERE v.id=i.volume_id AND v.metadata_provider=g.provider)
            WHERE e.provider=? AND e.target_issue=? GROUP BY g.provider,g.provider_id
            ORDER BY g.provider,g.provider_id LIMIT ? OFFSET ?''',
            (target.provider, target.provider_id, PAGE_SIZE + 1, offset))
        claims = rows(cursor, '''SELECT k.*,i.id AS source_local_id,i.volume_id AS source_volume_id,
            COALESCE(v.title,g.series_name) AS source_series,COALESCE(i.issue_number,g.number) AS source_number,
            g.deleted AS graph_deleted,
            (SELECT COUNT(*) FROM bibliographic_content_claim_evidence h WHERE h.claim_id=k.id) AS evidence_count,
            (SELECT COUNT(*) FROM bibliographic_content_claim_evidence h WHERE h.claim_id=k.id AND NOT EXISTS(
                SELECT 1 FROM bibliographic_reprint_edges e WHERE e.provider=h.provider AND e.provider_id=h.edge_id
                AND e.origin_issue=h.origin_issue AND e.target_issue=h.target_issue
                AND e.origin_story IS h.origin_story AND e.target_story IS h.target_story)) AS evidence_no_longer_current,
            (SELECT COUNT(*) FROM valid_file_content_coverage c WHERE c.claim_id=k.id) AS applied_files
            FROM bibliographic_content_claims k
            LEFT JOIN bibliographic_issue_refs g ON g.provider=k.source_provider AND g.provider_id=k.source_provider_id
            LEFT JOIN issue_external_ids x ON x.provider=k.source_provider AND x.provider_id=k.source_provider_id
            LEFT JOIN issues i ON i.id=x.issue_id AND EXISTS(SELECT 1 FROM volumes a
                WHERE a.id=i.volume_id AND a.metadata_provider=k.source_provider)
            LEFT JOIN volumes v ON v.id=i.volume_id
            WHERE k.target_provider=? AND k.target_provider_id=?
            ORDER BY k.created_at DESC,k.id LIMIT ? OFFSET ?''',
            (target.provider, target.provider_id, PAGE_SIZE + 1, offset))
        coverage = rows(cursor, '''SELECT c.*,f.filepath,valid.id IS NOT NULL AS valid
            FROM file_content_coverage c JOIN bibliographic_content_claims k ON k.id=c.claim_id
            LEFT JOIN active_files f ON f.id=c.file_id
            LEFT JOIN valid_file_content_coverage valid ON valid.id=c.id
            WHERE k.target_provider=? AND k.target_provider_id=?
            ORDER BY c.created_at DESC,c.id LIMIT ? OFFSET ?''',
            (target.provider, target.provider_id, PAGE_SIZE + 1, offset))
        files = rows(cursor, '''SELECT f.id,f.filepath FROM issues_files d JOIN active_files f ON f.id=d.file_id
            WHERE d.issue_id=? ORDER BY f.id LIMIT ?''', (target_id, PAGE_SIZE + 1))
        return {'schema': 'collected-contents/v1', 'target': descriptor(cursor, target),
                'candidates': candidates[:PAGE_SIZE], 'claims': claims[:PAGE_SIZE], 'coverage': coverage[:PAGE_SIZE],
                'files': files[:PAGE_SIZE], 'files_truncated': len(files) > PAGE_SIZE,
                'offset': offset, 'next_offset': offset + PAGE_SIZE if any(len(values) > PAGE_SIZE
                    for values in (candidates, claims, coverage)) else None,
                'semantics': 'graph_evidence_is_not_complete_containment'}


def claim_history(cursor, claim_id):
    """Original immutable receipts survive current graph reconciliation."""
    with transaction(cursor):
        claims = rows(cursor, 'SELECT * FROM bibliographic_content_claims WHERE id=?', (claim_id,))
        if not claims:
            raise ContentConflict('Claim unavailable')
        evidence = rows(cursor, '''SELECT h.* FROM bibliographic_content_claim_evidence h
            WHERE claim_id=? ORDER BY provider,edge_id LIMIT ?''', (claim_id, EDGE_LIMIT + 1))
        if len(evidence) > EDGE_LIMIT:
            raise ContentConflict('Historical evidence exceeds supported receipt limit')
        return {'schema': 'content-claim/v1', 'claim': claims[0], 'evidence': evidence}

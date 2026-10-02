"""Bounded, consistent read snapshots for review and future apply revalidation.

Only SELECTs and read savepoints. No settings constructors, filesystem IO or
provider calls. Caller may recompute within a future serialized write transaction.
"""

from hashlib import sha256

from backend.base.switch_review import FrozenReviewData, SwitchReviewError
from backend.internals.classification_provenance import details
from backend.internals.issue_facts import load_records

ROW_LIMIT = 20000


def rows(cursor, sql, params=(), limit=ROW_LIMIT):
    cursor.execute(sql, params)
    names = [column[0] for column in cursor.description]
    values = cursor.fetchmany(limit + 1)
    if len(values) > limit:
        raise SwitchReviewError('local_review_row_limit')
    return [dict(zip(names, row)) for row in values]


def identity_owners(cursor, table, key, references):
    """Fixed caller-selected tables, batched exact namespace/ID lookups."""
    if (table, key) not in (('volume_external_ids', 'volume_id'), ('issue_external_ids', 'issue_id')):
        raise ValueError('Unsupported identity table')
    grouped = {}
    for provider, identity in references:
        grouped.setdefault(provider, set()).add(identity)
    result = []
    for provider, ids in sorted(grouped.items()):
        ordered = sorted(ids)
        for start in range(0, len(ordered), 350):
            batch = ordered[start:start + 350]
            result.extend(rows(cursor, f'SELECT * FROM {table} WHERE provider=? AND provider_id IN (' +
                ','.join('?' for _ in batch) + f') ORDER BY {key},provider', (provider, *batch)))
            if len(result) > 40000:
                raise SwitchReviewError('identity_review_limit')
    return sorted(result, key=lambda row: (row[key], row['provider']))


def load_local(cursor, volume_id, target):
    """Deterministic domain fingerprint, not a whole-DB or filesystem hash."""
    if type(volume_id) is not int or volume_id <= 0:
        raise SwitchReviewError('invalid_volume_id')
    cursor.execute('SAVEPOINT provider_switch_read')
    try:
        volume = rows(cursor, 'SELECT * FROM volumes WHERE id=?', (volume_id,))
        if not volume:
            raise SwitchReviewError('volume_unavailable')
        volume = volume[0]
        issues = rows(cursor, 'SELECT * FROM issues WHERE volume_id=? ORDER BY id', (volume_id,), 10000)
        external = rows(cursor, 'SELECT * FROM volume_external_ids WHERE volume_id=? ORDER BY provider', (volume_id,))
        selected = [row for row in external if row['provider'] == volume['metadata_provider']]
        if len(selected) != 1:
            raise SwitchReviewError('selected_volume_identity_unavailable')
        refs = rows(cursor, '''SELECT e.* FROM issue_external_ids e JOIN issues i ON i.id=e.issue_id
            WHERE i.volume_id=? ORDER BY e.issue_id,e.provider''', (volume_id,), 40000)
        facts = load_records(cursor, volume_id=volume_id)
        direct = rows(cursor, '''SELECT d.*,f.filepath,f.size FROM issues_files d
            JOIN issues i ON i.id=d.issue_id JOIN active_files f ON f.id=d.file_id
            WHERE i.volume_id=? ORDER BY d.issue_id,d.file_id''', (volume_id,))
        general = rows(cursor, '''SELECT g.*,f.filepath,f.size FROM volume_files g JOIN active_files f ON f.id=g.file_id
            WHERE g.volume_id=? ORDER BY g.file_id''', (volume_id,))
        artwork = cursor.execute('SELECT cover FROM volumes_covers WHERE volume_id=?', (volume_id,)).fetchone()
        cover = artwork[0] if artwork else None
        data = dict(volume=volume, selected=selected[0], issues=issues, external=external, issue_refs=refs,
            facts=facts, direct=direct, general=general,
            artwork=dict(available=cover is not None, digest=sha256(cover).hexdigest() if cover else None),
            root=rows(cursor, 'SELECT * FROM root_folders WHERE id=?', (volume['root_folder'],)),
            classification=details(cursor, volume_id),
            classification_revision=rows(cursor, 'SELECT * FROM classification_state WHERE volume_id=?', (volume_id,)))
        payload = target.data.view()
        issue_references = {(target.reference.provider, row['provider_id']) for row in payload['issues']}
        volume_references = {(target.reference.provider, target.reference.provider_id)}
        for assertion in payload['assertions']:
            (volume_references if assertion['entity'] == 'volume' else issue_references).add(
                (assertion['provider'], assertion['provider_id']))
        data['volume_owners'] = identity_owners(cursor, 'volume_external_ids', 'volume_id', volume_references)
        data['issue_owners'] = identity_owners(cursor, 'issue_external_ids', 'issue_id', issue_references)
        data['bibliography'] = dict(
            volume=rows(cursor, 'SELECT * FROM volume_bibliography WHERE volume_id=?', (volume_id,)),
            issues=rows(cursor, '''SELECT b.* FROM issue_bibliography b JOIN issues i ON i.id=b.issue_id
                WHERE i.volume_id=? ORDER BY b.issue_id''', (volume_id,)),
            observation_sets=rows(cursor, '''SELECT s.* FROM story_observation_sets s JOIN issues i ON i.id=s.issue_id
                WHERE i.volume_id=? ORDER BY s.id''', (volume_id,)))
        # All established local references are candidates for *inspection*, not
        # permission to translate a claim endpoint. Include target refs as well
        # so a future rebound-pair collision is visible before application.
        claim_refs = {(row['provider'], row['provider_id']) for row in refs} | issue_references
        claims = {}
        graph = {}
        for provider in sorted({p for p, _ in claim_refs}):
            ids = sorted(identity for p, identity in claim_refs if p == provider)
            for start in range(0, len(ids), 350):
                batch = ids[start:start + 350]
                marks = ','.join('?' for _ in batch)
                for row in rows(cursor, '''SELECT * FROM bibliographic_content_claims WHERE
                    (target_provider=? AND target_provider_id IN (''' + marks + ''')) OR
                    (source_provider=? AND source_provider_id IN (''' + marks + ')) ORDER BY id',
                    (provider, *batch, provider, *batch)):
                    claims[row['id']] = row
                for row in rows(cursor, 'SELECT * FROM bibliographic_issue_refs WHERE provider=? AND provider_id IN (' +
                                marks + ') ORDER BY provider_id', (provider, *batch)):
                    graph[(row['provider'], row['provider_id'])] = row
                if max(len(claims), len(graph)) > ROW_LIMIT:
                    raise SwitchReviewError('claim_review_limit')
        # Include stale active coverage whose claim no longer matches selected
        # identity; never claim that rebinding would automatically resurrect it.
        local_coverage = rows(cursor, '''SELECT c.* FROM file_content_coverage c WHERE
            c.target_issue_id IN (SELECT id FROM issues WHERE volume_id=?) OR
            c.source_issue_id IN (SELECT id FROM issues WHERE volume_id=?) ORDER BY c.id''', (volume_id, volume_id))
        for start in range(0, len(local_coverage), 350):
            ids = sorted({row['claim_id'] for row in local_coverage[start:start + 350]})
            if ids:
                for row in rows(cursor, 'SELECT * FROM bibliographic_content_claims WHERE id IN (' +
                                ','.join('?' for _ in ids) + ')', ids):
                    claims[row['id']] = row
        if len(claims) > ROW_LIMIT:
            raise SwitchReviewError('claim_review_limit')
        coverage, evidence, valid = {}, [], {}
        ordered_claims = sorted(claims)
        for start in range(0, len(ordered_claims), 350):
            ids = ordered_claims[start:start + 350]
            marks = ','.join('?' for _ in ids)
            for row in rows(cursor, 'SELECT * FROM file_content_coverage WHERE claim_id IN (' + marks + ') ORDER BY id', ids):
                coverage[row['id']] = row
            evidence.extend(rows(cursor, 'SELECT * FROM bibliographic_content_claim_evidence WHERE claim_id IN (' +
                marks + ') ORDER BY claim_id,provider,edge_id', ids))
            for row in rows(cursor, 'SELECT * FROM valid_file_content_coverage WHERE claim_id IN (' + marks + ') ORDER BY id', ids):
                valid[row['id']] = row
            if max(len(coverage), len(evidence)) > ROW_LIMIT:
                raise SwitchReviewError('coverage_review_limit')
        if max(len(coverage), len(evidence)) > ROW_LIMIT:
            raise SwitchReviewError('coverage_review_limit')
        data.update(claims=[claims[key] for key in sorted(claims)], coverage=[coverage[key] for key in sorted(coverage)],
                    claim_evidence=evidence, valid_coverage=[valid[key] for key in sorted(valid)],
                    graph=[graph[key] for key in sorted(graph)])
        # Ownership of outside-volume sources may depend on a collected target
        # in this volume. Include their exact live selected identities/monitoring.
        related = sorted({row[key] for row in coverage.values() for key in ('source_issue_id', 'target_issue_id')
                          if row[key] is not None} | {row['id'] for row in issues})
        ownership, endpoints = [], []
        for start in range(0, len(related), 350):
            ids = related[start:start + 350]
            marks = ','.join('?' for _ in ids)
            ownership.extend(rows(cursor, 'SELECT * FROM canonical_issue_files WHERE issue_id IN (' + marks +
                                  ') ORDER BY issue_id,file_id,role,coverage_id', ids))
            endpoints.extend(rows(cursor, '''SELECT i.id,i.volume_id,i.monitored,v.monitored AS volume_monitored,
                v.metadata_provider,x.provider_id FROM issues i JOIN volumes v ON v.id=i.volume_id
                LEFT JOIN issue_external_ids x ON x.issue_id=i.id AND x.provider=v.metadata_provider
                WHERE i.id IN (''' + marks + ') ORDER BY i.id', ids))
            if len(ownership) > 40000:
                raise SwitchReviewError('ownership_review_limit')
        data.update(ownership=ownership, endpoints=endpoints, dependencies=dependencies(cursor, volume_id),
            monitor=dict(configuration=rows(cursor, "SELECT key,value FROM config WHERE key='folder_monitoring'"),
                root=rows(cursor, 'SELECT * FROM monitor_roots WHERE root_id=?', (volume['root_folder'],))))
        # Root polling times are operational diagnostics, not metadata authority.
        for row in data['monitor']['root']:
            for name in ('generation', 'requested', 'completed_at', 'attempted_at'):
                row.pop(name, None)
        return FrozenReviewData.create(data, 32 * 1024 * 1024)
    finally:
        cursor.execute('RELEASE provider_switch_read')


def dependencies(cursor, volume_id):
    output = []
    # Do not retain raw JSON intent/completion, paths or downloader resolver data.
    for table, payload, terminal, category, digest_field in (
        ('organization_jobs', 'intent', "'completed','failed'", 'organization', 'intent_digest'),
        ('acquisition_downloads', 'intent', "'completed','failed'", 'download', 'intent_digest'),
        ('acquisition_intakes', 'completion', "'completed'", 'intake', 'completion_digest')):
        extra = ''
        parameters = [volume_id, volume_id]
        if category == 'organization':
            # Recovery/undo can retain links from a volume other than the
            # destination. Inspect exact journaled local IDs, not path guesses.
            for field in ('links_after', 'database_before.file.links', 'database_restore.file.links'):
                extra += f''' OR EXISTS(SELECT 1 FROM json_each(
                    CASE WHEN json_valid({payload}) THEN {payload} ELSE '{{}}' END,'$.{field}') l
                    WHERE json_extract(l.value,'$[0]')=?)'''
                parameters.append(volume_id)
        sql = f'''SELECT id,state,{digest_field} AS intent_digest,updated_at FROM {table}
            WHERE state NOT IN ({terminal}) AND (
                NOT json_valid({payload}) OR json_extract(CASE WHEN json_valid({payload}) THEN {payload} ELSE '{{}}' END,'$.volume_id')=? OR
                EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid({payload}) THEN {payload} ELSE '{{}}' END,
                    '$.issue_ids') m JOIN issues i ON i.id=m.value WHERE i.volume_id=?){extra}) ORDER BY id'''
        output.extend(dict(row, category=category) for row in rows(cursor, sql, parameters, 2000))
    output.extend(dict(row, category='wanted_reservation') for row in rows(cursor, '''
        SELECT r.* FROM wanted_reservations r JOIN issues i ON i.id=r.issue_id
        WHERE i.volume_id=? AND r.active=1 ORDER BY r.issue_id''', (volume_id,)))
    output.extend(dict(row, category='wanted_search') for row in rows(cursor, '''
        SELECT id,state,target_fingerprint FROM wanted_searches WHERE volume_id=? AND state='searching' ORDER BY id''', (volume_id,)))
    output.extend(dict(row, category='wanted_decision') for row in rows(cursor, '''
        SELECT d.id,d.state,d.updated_at,d.acquisition_id FROM wanted_decisions d
        JOIN wanted_searches s ON s.id=d.search_id WHERE s.volume_id=?
        AND d.state IN ('selected','grabbing','tracking','review') ORDER BY d.id''', (volume_id,)))
    output.extend(dict(row, category='download_queue') for row in rows(cursor,
        'SELECT id,volume_id FROM download_queue WHERE volume_id=? ORDER BY id', (volume_id,)))
    return output

"""Read-only, bounded projections of authoritative receipts; no artifact IO.

JSON extraction happens in SQLite. Large XML, tree/domain snapshots and event
payloads never enter overview Python objects. No schema or cache is installed.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from backend.base.maintenance_history import (DOMAINS, POLICY,
                                              HistoryCursor, HistoryError,
                                              HistoryFilter, page_limit)
from backend.base.organization_job import EXECUTOR_POLICY, ExecutionCode

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_DETAIL_BYTES = 8 * 1024 * 1024


def bounded(value, maximum=MAX_PAGE_BYTES):
    if len(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)) > maximum:
        raise HistoryError('history_projection_too_large')
    return value


@contextmanager
def connection(database):
    db = sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.rollback()
        db.close()


# All SQL below is repository-owned. User values are parameters only.
# Only bounded summary fragments are transferred; arbitrary stored JSON is not
# returned. Future/invalid effect versions are history-only, never guessed.
_JOB = f"""
WITH safe_jobs AS (
 SELECT *, CASE WHEN length(intent)<=6291456 AND json_valid(intent)
                 THEN intent ELSE '{{}}' END AS payload FROM organization_jobs /* history_candidates */
), classified AS (
 SELECT *, CASE
  WHEN executor_version!='{EXECUTOR_POLICY}' OR json_extract(payload,'$.version') IS NOT executor_version
       THEN 'unsupported_history'
  WHEN json_type(payload,'$.archive_effect') IS NOT NULL THEN
   CASE WHEN json_extract(payload,'$.archive_effect')='archive-normalization/v1' THEN 'archive_normalization'
        ELSE 'unsupported_history' END
  WHEN json_type(payload,'$.quarantine_effect') IS NOT NULL THEN
   CASE WHEN json_extract(payload,'$.quarantine_effect')!='retained-artifact/v1' THEN 'unsupported_history'
        WHEN json_extract(payload,'$.inverse')=1 THEN 'duplicate_restore' ELSE 'duplicate_quarantine' END
  WHEN json_type(payload,'$.directory_effect') IS NOT NULL THEN
   CASE WHEN json_extract(payload,'$.directory_effect')='volume-tree/v1' THEN 'folder_organization'
        ELSE 'unsupported_history' END
  WHEN json_type(payload,'$.rename_origin')='object' THEN 'rename'
  WHEN json_type(payload,'$.repair_origin')='object' OR json_type(payload,'$.xml')='text' THEN 'comicinfo_repair'
  ELSE 'local_organization' END AS operation,
  CASE state WHEN 'completed' THEN 'complete' WHEN 'running' THEN 'active'
   WHEN 'pending' THEN 'pending' WHEN 'failed' THEN 'failed'
   WHEN 'recovery_required' THEN 'recovery_required' ELSE 'unknown' END AS normalized,
  CASE WHEN json_type(payload,'$.archive_effect') IS NOT NULL THEN json_extract(payload,'$.original')
       WHEN json_type(payload,'$.quarantine_effect') IS NOT NULL
       THEN json_extract(payload,'$.original') ELSE json_extract(payload,'$.source') END AS shown_source,
  CASE WHEN json_type(payload,'$.quarantine_effect') IS NOT NULL THEN NULL
       ELSE json_extract(payload,'$.target') END AS shown_target
 FROM safe_jobs
)
SELECT id, 'organization' AS domain, operation, normalized,
 state AS domain_state, strftime('%Y-%m-%dT%H:%M:%fZ',created_at) AS time,
 created_at,updated_at, batch_id,inverse_of,
 json_extract(payload,'$.volume_id') AS volume_id,
 COALESCE(json_extract(payload,'$.file_id'),json_extract(payload,'$.database_before.file.id'),
   (SELECT json_extract(s.evidence,'$.database_after.file.id') FROM organization_steps s
     WHERE s.job_id=classified.id AND json_valid(s.evidence)
       AND json_extract(s.evidence,'$.database_after.file.id') IS NOT NULL ORDER BY s.ordinal DESC LIMIT 1)) AS file_id,
 shown_source AS source,shown_target AS target,
 CASE WHEN operation IN ('archive_normalization','comicinfo_repair','duplicate_restore','unsupported_history')
       OR inverse_of IS NOT NULL THEN 'unsupported' ELSE 'unchecked' END AS inverse,
 executor_version AS source_version,error,
 json_object('worklist',COALESCE(json_extract(payload,'$.rename_origin.worklist'),
  json_extract(payload,'$.folder_origin.worklist'),json_extract(payload,'$.duplicate_origin.worklist'),
  json_extract(payload,'$.repair_origin')),
  'review_digest',COALESCE(json_extract(payload,'$.rename_origin.review_digest'),
   json_extract(payload,'$.folder_origin.review_digest'),json_extract(payload,'$.duplicate_origin.review_digest')),
  'finding_id',COALESCE(json_extract(payload,'$.rename_origin.finding_id'),json_extract(payload,'$.folder_origin.finding_id')),
  'group_id',json_extract(payload,'$.group_id'),
  'effect_count',json_array_length(payload,'$.effects'),
  'issue_count',COALESCE(json_array_length(payload,'$.issue_ids'),json_array_length(payload,'$.links_after')),
  'affected_ids_detail_required',1) AS summary
FROM classified
"""

_REPAIR = """SELECT id,'metadata_repair' AS domain,'metadata_repair' AS operation,
 'complete' AS normalized,'applied' AS domain_state,
 strftime('%Y-%m-%dT%H:%M:%fZ',applied_at) AS time,applied_at AS created_at,applied_at AS updated_at,
 NULL AS batch_id,NULL AS inverse_of,volume_id,NULL AS file_id,NULL AS source,NULL AS target,
 'unsupported' AS inverse,policy AS source_version,NULL AS error,
 json_object('worklist_id',worklist_id,'worklist_digest',worklist_digest,'session_id',session_id,
 'revision',revision,'review_digest',review_digest,'field_count',field_count,'provider',provider,
 'provider_id',provider_id,'authority_generation',authority_generation,
 'classification_action',classification_action,'bibliography_action',bibliography_action) AS summary
 FROM metadata_repair_receipts"""

_SWITCH = """SELECT id,'provider_switch' AS domain,'provider_switch' AS operation,
 'complete' AS normalized,'applied' AS domain_state,
 strftime('%Y-%m-%dT%H:%M:%fZ',applied_at) AS time,applied_at AS created_at,applied_at AS updated_at,
 NULL AS batch_id,NULL AS inverse_of,volume_id,NULL AS file_id,NULL AS source,NULL AS target,
 'fresh_operation_required' AS inverse,schema_version AS source_version,NULL AS error,
 json_object('session_id',session_id,'revision',revision,'source_provider',source_provider,
 'source_provider_id',source_provider_id,'source_generation',source_generation,
 'target_provider',target_provider,'target_provider_id',target_provider_id,
 'target_generation',target_generation,'mapped_count',mapped_count,'added_count',added_count,
 'claim_count',claim_count,'coverage_count',coverage_count) AS summary FROM provider_switch_receipts"""

_CLAIM = """SELECT id,'content_claim' AS domain,'content_claim' AS operation,
 'complete' AS normalized,CASE WHEN retired_at IS NULL THEN 'active_claim' ELSE 'retired_claim' END AS domain_state,
 strftime('%Y-%m-%dT%H:%M:%fZ',created_at,'unixepoch') AS time,created_at,retired_at AS updated_at,
 NULL AS batch_id,NULL AS inverse_of,NULL AS volume_id,NULL AS file_id,NULL AS source,NULL AS target,
 'unsupported' AS inverse,policy AS source_version,NULL AS error,
 json_object('kind',kind,'source_provider',source_provider,'source_provider_id',source_provider_id,
 'target_provider',target_provider,'target_provider_id',target_provider_id,
 'supersedes',supersedes,'retired_at',retired_at) AS summary FROM bibliographic_content_claims"""

_COVERAGE = """SELECT id,'content_coverage' AS domain,'content_coverage' AS operation,
 'complete' AS normalized,CASE WHEN retired_at IS NULL THEN 'recorded_coverage' ELSE 'retired_coverage' END AS domain_state,
 strftime('%Y-%m-%dT%H:%M:%fZ',created_at,'unixepoch') AS time,created_at,retired_at AS updated_at,
 NULL AS batch_id,NULL AS inverse_of,NULL AS volume_id,original_file_id AS file_id,NULL AS source,NULL AS target,
 'unsupported' AS inverse,policy AS source_version,NULL AS error,
 json_object('claim_id',claim_id,'original_target_id',original_target_id,'original_source_id',original_source_id,
 'retired_at',retired_at) AS summary FROM file_content_coverage"""

_INTAKE = """SELECT id,'intake' AS domain,'intake' AS operation,
 CASE state WHEN 'completed' THEN 'complete' WHEN 'pending' THEN 'pending'
 WHEN 'failed' THEN 'failed' ELSE 'unknown' END AS normalized,state AS domain_state,
 strftime('%Y-%m-%dT%H:%M:%fZ',created_at) AS time,created_at,updated_at,
 NULL AS batch_id,NULL AS inverse_of,
 json_extract(CASE WHEN json_valid(completion) THEN completion ELSE '{}' END,'$.volume_id') AS volume_id,
 NULL AS file_id,NULL AS source,NULL AS target,'unsupported' AS inverse,
 'acquisition-intake' AS source_version,NULL AS error,
 json_object('kind',kind,'download_id',download_id,'operational_only',1) AS summary FROM acquisition_intakes"""

SOURCES = dict(zip(DOMAINS, (_JOB, _REPAIR, _SWITCH, _CLAIM, _COVERAGE, _INTAKE)))

_JOB_KEYS = """SELECT id,'organization' AS domain,batch_id,
 strftime('%Y-%m-%dT%H:%M:%fZ',created_at) AS time,
 CASE state WHEN 'completed' THEN 'complete' WHEN 'running' THEN 'active'
 WHEN 'pending' THEN 'pending' WHEN 'failed' THEN 'failed'
 WHEN 'recovery_required' THEN 'recovery_required' ELSE 'unknown' END AS normalized
 FROM organization_jobs"""


def projection(row):
    result = dict(row)
    result['summary'] = json.loads(result['summary'])
    result['entry_id'] = result['domain'] + ':' + result['id']
    result['detail_key'] = [result['domain'], result['id']]
    result['state'] = result.pop('normalized')
    result['inverse_capability'] = result.pop('inverse')
    result['history_only'] = (result['domain'] != 'organization' or result['state'] == 'complete'
                             and result['inverse_capability'] == 'unsupported')
    result['recovery_capability'] = ('unchecked' if result['domain'] == 'organization'
        and result['state'] in ('pending', 'active', 'failed', 'recovery_required') else 'unsupported')
    result['internal_storage_hidden'] = result['operation'] in ('duplicate_quarantine', 'duplicate_restore')
    result['volume_filter_scope'] = ('qualified_identity_mapping' if result['domain'] == 'content_claim'
                                    else 'current_issue_parent' if result['domain'] == 'content_coverage' else 'recorded')
    result['eligibility_checked'] = False
    if result['error'] not in {code.value for code in ExecutionCode}:
        result['error'] = 'domain_error' if result['error'] else None
    return result


def correlations(db, items):
    jobs = {item['id']: item for item in items if item['domain'] == 'organization'}
    if not jobs:
        return
    placeholders = ','.join('?' for _ in jobs)
    for row in db.execute(f'''SELECT id,inverse_of,state FROM organization_jobs
            WHERE inverse_of IN ({placeholders})''', tuple(jobs)):
        item = jobs[row['inverse_of']]
        item['inverse_job'] = dict(id=row['id'], state=row['state'])
        item['relationship_state'] = ('reverted' if row['state'] == 'completed' else 'inverse_incomplete')
    for row in db.execute(f'''SELECT id,intake_id,organization_job_id FROM acquisition_artifacts
            WHERE organization_job_id IN ({placeholders})''', tuple(jobs)):
        jobs[row['organization_job_id']]['intake_link'] = dict(intake_id=row['intake_id'], artifact_id=row['id'])
    for row in db.execute(f'''SELECT job_id,file_id FROM quarantined_files
            WHERE job_id IN ({placeholders})''', tuple(jobs)):
        jobs[row['job_id']]['retained_marker'] = dict(file_id=row['file_id'], owned_by_job=True)


def _conditions(filters, cursor):
    terms, params = ['time IS NOT NULL'], []
    for key, value in (('operation', filters.operation), ('normalized', filters.state),
                       ('inverse', filters.inverse),
                       ('batch_id', filters.batch_id)):
        if value is not None:
            terms.append(key + '=?')
            params.append(value)
    if filters.volume_id is not None:
        terms.append('''(volume_id=? OR (domain='content_coverage' AND id IN (
            SELECT c.id FROM file_content_coverage c JOIN issues i ON i.id IN (c.source_issue_id,c.target_issue_id)
            WHERE i.volume_id=?)) OR (domain='content_claim' AND id IN (
            SELECT c.id FROM bibliographic_content_claims c JOIN issue_external_ids x
              ON (x.provider=c.target_provider AND x.provider_id=c.target_provider_id)
                 OR (x.provider=c.source_provider AND x.provider_id=c.source_provider_id)
            JOIN issues i ON i.id=x.issue_id WHERE i.volume_id=?)))''')
        params.extend((filters.volume_id,) * 3)
    if filters.file_id is not None:
        terms.append('''(file_id=? OR (domain='organization' AND id IN (
            SELECT j.id FROM organization_jobs j,json_each(
              CASE WHEN json_valid(j.intent) THEN
                CASE WHEN json_valid(json_extract(j.intent,'$.tree_database_before')) THEN
                  json_extract(json_extract(j.intent,'$.tree_database_before'),'$.files') ELSE '[]' END
              ELSE '[]' END) f WHERE json_extract(f.value,'$.id')=?)))''')
        params.extend((filters.file_id, filters.file_id))
    if filters.since:
        terms.append('time>=?')
        params.append(filters.since)
    if filters.until:
        terms.append('time<=?')
        params.append(filters.until)
    if cursor:
        if not isinstance(cursor, HistoryCursor) or cursor.filters != filters:
            raise HistoryError('history_cursor_filter_mismatch')
        terms.append('(time,domain,id)<(?,?,?)')
        params.extend((cursor.time, cursor.domain, cursor.identifier))
    return ' AND '.join(terms), params


def page(db, filters=HistoryFilter(), *, before=None, limit=50):
    page_limit(limit)
    if not isinstance(filters, HistoryFilter):
        raise HistoryError('invalid_history_filter')
    condition, params = _conditions(filters, before)
    rows = []
    for domain, source in SOURCES.items():
        if filters.domain is None or filters.domain == domain:
            source_params = []
            if domain == 'organization' and all(value is None for value in (
                    filters.operation, filters.inverse, filters.volume_id, filters.file_id)):
                # Global time ordering may scan scalar keys (schema66 has no
                # global time index), but it need not parse every large intent.
                keys = (f'SELECT id FROM ({_JOB_KEYS}) WHERE {condition} '
                        'ORDER BY time DESC,domain DESC,id DESC LIMIT ?')
                source = source.replace('/* history_candidates */', f'WHERE id IN ({keys})')
                source_params = [*params, limit + 1]
            rows.extend(db.execute(f'SELECT * FROM ({source}) WHERE {condition} '
                'ORDER BY time DESC,domain DESC,id DESC LIMIT ?', (*source_params, *params, limit + 1)).fetchall())
    rows.sort(key=lambda row: (row['time'], row['domain'], row['id']), reverse=True)
    selected = rows[:limit]
    next_page = None
    if len(rows) > limit:
        last = selected[-1]
        next_page = HistoryCursor(last['time'], last['domain'], last['id'], filters)
    items = [projection(row) for row in selected]
    correlations(db, items)
    bounded(items)
    return dict(version=POLICY, items=items, next_cursor=next_page)


def entry(db, domain, identifier):
    if domain not in SOURCES or not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
        raise HistoryError('invalid_history_identity')
    row = db.execute(f'SELECT * FROM ({SOURCES[domain]}) WHERE id=?', (identifier,)).fetchone()
    if row is None:
        raise HistoryError('history_entry_unavailable')
    item = projection(row)
    correlations(db, [item])
    return bounded(item)


def batch(db, identifier, *, offset=0, limit=50):
    """Real mutation jobs plus bounded recorded no-op/group provenance.

    Batches are intentionally not synthesized for transient all-no-op reviews.
    Independent current job states, not a cached completion flag, own outcome.
    """
    page_limit(limit)
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 512:
        raise HistoryError('invalid_history_batch')
    if type(offset) is not int or not 0 <= offset <= 2000:
        raise HistoryError('invalid_history_page')
    rows = db.execute(f'SELECT * FROM ({_JOB}) WHERE batch_id=? ORDER BY time,id LIMIT 1001', (identifier,)).fetchall()
    if not rows:
        raise HistoryError('history_batch_unavailable')
    if len(rows) > 1000:
        raise HistoryError('history_batch_too_large')
    # Extract correlation only; never transfer a complete journal intent.
    origin = db.execute('''SELECT COALESCE(json_extract(intent,'$.rename_origin'),
        json_extract(intent,'$.folder_origin'),json_extract(intent,'$.duplicate_origin'))
        FROM organization_jobs WHERE id=?''', (rows[0]['id'],)).fetchone()[0]
    if origin is not None and len(origin) > MAX_PAGE_BYTES:
        raise HistoryError('history_batch_too_large')
    provenance = json.loads(origin) if origin else {}
    counts = {}
    for row in rows:
        counts[row['normalized']] = counts.get(row['normalized'], 0) + 1
    extras = provenance.get('no_changes', [])
    groups = provenance.get('groups', [])
    if not isinstance(extras, list) or not isinstance(groups, list) or len(extras) + len(groups) > 1000:
        raise HistoryError('history_batch_too_large')
    nonmutating = [dict(state='no_changes', provenance=item, job_id=None) for item in extras]
    nonmutating.extend(dict(state=g['action'], group_id=g['id'], job_id=None)
                       for g in groups if g['action'] in ('keep_all', 'acknowledge', 'review_later'))
    counts['nonmutating'] = len(nonmutating)
    file_count = db.execute('''SELECT COUNT(DISTINCT file_id) FROM (
        SELECT COALESCE(json_extract(intent,'$.file_id'),json_extract(intent,'$.database_before.file.id')) AS file_id
        FROM organization_jobs WHERE batch_id=?
        UNION ALL
        SELECT json_extract(f.value,'$.id') FROM organization_jobs j,json_each(
            CASE WHEN json_valid(json_extract(j.intent,'$.tree_database_before'))
            THEN json_extract(json_extract(j.intent,'$.tree_database_before'),'$.files') ELSE '[]' END) f
        WHERE j.batch_id=?)''', (identifier, identifier)).fetchone()[0]
    jobs = [projection(row) for row in rows[offset:offset + limit]]
    correlations(db, jobs)
    if len(jobs) < limit:
        start = max(0, offset - len(rows))
        jobs.extend(nonmutating[start:start + limit - len(jobs)])
    state = ('complete' if counts.get('complete') == len(rows) else
             'partially_completed_batch' if counts.get('complete') else 'pending_or_stopped')
    return bounded(dict(version=POLICY, batch_id=identifier, state=state,
        operations=sorted({row['operation'] for row in rows}), created_at=rows[0]['created_at'],
        counts=counts, mutation_job_count=len(rows), nonmutating_count=len(nonmutating),
        selected_count=len(provenance.get('selection', [])),
        selected_unit='group' if groups else 'finding', group_count=len(groups),
        volume_count=len({row['volume_id'] for row in rows if row['volume_id'] is not None}),
        file_count=file_count,
        inverse_unchecked=sum(row['inverse'] == 'unchecked' for row in rows),
        inverse_unsupported=sum(row['inverse'] != 'unchecked' for row in rows),
        provenance=provenance, items=jobs,
        next_offset=offset + limit if offset + limit < len(rows) + len(nonmutating) else None))

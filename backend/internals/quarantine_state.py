"""Bounded domain evidence for the versioned quarantine journal.

No lifecycle writes live here. Callers own the read/write transaction. Reads are
batched by table, including canonical ownership outside the artifact's volume.
"""

from dataclasses import asdict

from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.internals.organization_jobs import canonical
from backend.internals.provider_authority import capture

MAX_ROWS = 20000
MAX_BYTES = 4 * 1024 * 1024


def snapshot(cursor, file_ids, *, historical_issues=(), historical_volumes=()):
    ids = set(file_ids)
    if not ids or len(ids) > 256 or any(type(i) is not int or i <= 0 for i in ids):
        raise OrganizationError(ExecutionCode.UNSUPPORTED, 'quarantine_domain_bound')
    data, size = {}, 0
    for key, sql in (
        ('files', 'SELECT * FROM files ORDER BY id'),
        ('markers', 'SELECT * FROM quarantined_files ORDER BY file_id'),
        ('direct', 'SELECT * FROM issues_files ORDER BY file_id,issue_id'),
        ('general', 'SELECT * FROM volume_files ORDER BY file_id,volume_id'),
        ('issues', 'SELECT * FROM issues ORDER BY id'),
        ('volumes', 'SELECT * FROM volumes ORDER BY id'),
        ('roots', 'SELECT * FROM root_folders ORDER BY id'),
        ('volume_refs', 'SELECT * FROM volume_external_ids ORDER BY volume_id,provider'),
        ('issue_refs', 'SELECT * FROM issue_external_ids ORDER BY issue_id,provider'),
        ('numbers', 'SELECT * FROM issue_number_facts ORDER BY issue_id'),
        ('dates', 'SELECT * FROM issue_date_facts ORDER BY issue_id,source_field'),
        ('variants', 'SELECT * FROM issue_variant_of ORDER BY issue_id'),
        ('coverage', 'SELECT * FROM file_content_coverage ORDER BY id'),
        ('valid_coverage', 'SELECT * FROM valid_file_content_coverage ORDER BY id'),
        ('claims', 'SELECT * FROM bibliographic_content_claims ORDER BY id'),
        ('claim_evidence', 'SELECT * FROM bibliographic_content_claim_evidence ORDER BY claim_id,provider,edge_id'),
        ('classification', 'SELECT * FROM classification_provenance ORDER BY volume_id'),
        ('classification_control', 'SELECT * FROM classification_control_state ORDER BY volume_id'),
        ('ownership', 'SELECT * FROM canonical_issue_files ORDER BY issue_id,file_id,role,coverage_id'),
    ):
        result = cursor.execute(sql + ' LIMIT ?', (MAX_ROWS + 1,))
        names = [c[0] for c in result.description]
        # App cursors enable BOOL conversion; JobStore intentionally does not.
        # SQL-domain evidence must not depend on which connection acquired it.
        values = [dict(zip(names, (int(v) if type(v) is bool else v for v in r))) for r in result]
        size += len(canonical(values))
        if len(values) > MAX_ROWS or size > MAX_BYTES:
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'quarantine_domain_bound')
        data[key] = values
    return project(cursor, data, ids, historical_issues=historical_issues, historical_volumes=historical_volumes)


def project(cursor, source, file_ids, *, historical_issues=(), historical_volumes=()):
    """Reuse one acquired batch snapshot when constructing per-file intents."""
    ids, data = set(file_ids), dict(source)
    data['files'] = [r for r in data['files'] if r['id'] in ids]
    if {r['id'] for r in data['files']} != ids:
        raise OrganizationError(ExecutionCode.STALE, 'quarantine_file_identity_missing')
    for key in ('markers', 'direct', 'general', 'coverage', 'valid_coverage'):
        data[key] = [r for r in data[key] if r['file_id'] in ids]
    issue_ids = set(historical_issues) | {r['issue_id'] for r in data['direct']}
    issue_ids.update(r['issue_id'] for r in data['ownership'] if r['file_id'] in ids)
    issue_ids.update(r[k] for r in data['coverage'] for k in ('source_issue_id', 'target_issue_id') if r[k] is not None)
    data['issues'] = [r for r in data['issues'] if r['id'] in issue_ids]
    volume_ids = set(historical_volumes) | {r['volume_id'] for r in data['issues']} | {r['volume_id'] for r in data['general']}
    data['volumes'] = [r for r in data['volumes'] if r['id'] in volume_ids]
    data['volume_refs'] = [r for r in data['volume_refs'] if r['volume_id'] in volume_ids]
    for key in ('classification', 'classification_control'):
        data[key] = [r for r in data[key] if r['volume_id'] in volume_ids]
    data['issue_refs'] = [r for r in data['issue_refs'] if r['issue_id'] in issue_ids]
    for key in ('numbers', 'dates', 'variants'):
        data[key] = [r for r in data[key] if r['issue_id'] in issue_ids]
    claim_ids = {r['claim_id'] for r in data['coverage']}
    data['claims'] = [r for r in data['claims'] if r['id'] in claim_ids]
    data['claim_evidence'] = [r for r in data['claim_evidence'] if r['claim_id'] in claim_ids]
    data['ownership'] = [r for r in data['ownership'] if r['issue_id'] in issue_ids]
    if 'authority' in source:
        data['authority'] = [r for r in source['authority'] if r['volume_id'] in volume_ids]
        return data
    authorities = capture(cursor, sorted(volume_ids))
    if set(authorities) != volume_ids:
        raise OrganizationError(ExecutionCode.STALE, 'quarantine_authority_missing')
    data['authority'] = [asdict(authorities[i]) for i in sorted(authorities)]
    return data


def identity(data):
    """Post-namespace proof, not a new naming/metadata-policy evaluation.

    Keep stable IDs, selected authorities, folder/root identity and all historical
    associations/coverage. Unrelated title/description/settings changes need not
    strand an already-moved artifact. Ownership is checked separately.
    """
    value = dict(data)
    value.pop('ownership')
    value.pop('valid_coverage')
    value.pop('classification')
    value.pop('classification_control')
    value['volumes'] = [{k: r[k] for k in ('id', 'root_folder', 'folder', 'custom_folder',
                                         'metadata_provider', 'authority_generation')} for r in data['volumes']]
    value['issues'] = [{k: r[k] for k in ('id', 'volume_id')} for r in data['issues']]
    value['volume_refs'] = [{k: r[k] for k in ('volume_id', 'provider', 'provider_id')} for r in data['volume_refs']]
    return value


def require_owned(data, removed):
    affected = {r['issue_id'] for r in data['ownership'] if r['file_id'] in removed}
    retained = {r['issue_id'] for r in data['ownership'] if r['file_id'] not in removed}
    if affected - retained:
        raise OrganizationError(ExecutionCode.NOT_AUTHORIZED, 'quarantine_ownership_loss')

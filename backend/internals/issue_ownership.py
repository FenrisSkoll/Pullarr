"""Canonical owned-state projection, never organizational file identity.

SQL consumers use canonical_issue_files, whose coverage branch checks all live
predicates. File-list, matching and naming APIs continue using issues_files.
"""

from backend.internals.content_claims import rows, transaction


def owned_ids(cursor, issue_ids):
    """Bounded lightweight predicate acquisition for scan/reconciliation callers."""
    ids = tuple(dict.fromkeys(issue_ids))
    result = set()
    with transaction(cursor):
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            result.update(row[0] for row in cursor.execute(
                'SELECT DISTINCT issue_id FROM canonical_issue_files WHERE issue_id IN (' +
                ','.join('?' for _ in batch) + ')', batch))
    return result


def load_ownership(cursor, *, issue_ids=None, volume_id=None):
    """One bounded/batched acquisition, no query per issue or release candidate."""
    if (issue_ids is None) == (volume_id is None):
        raise ValueError('Select a volume or exact issue IDs')
    result = {}
    if issue_ids is not None:
        ids = tuple(dict.fromkeys(issue_ids))
        batches = [ids[start:start + 400] for start in range(0, len(ids), 400)]
    else:
        batches = [(volume_id,)]
    with transaction(cursor):
        for batch in batches:
            predicate = 'i.volume_id=?' if volume_id is not None else 'i.id IN (' + ','.join('?' for _ in batch) + ')'
            values = rows(cursor, '''SELECT i.id AS issue_id,o.file_id,o.role,o.coverage_id,
                o.target_issue_id,o.claim_id,f.filepath,f.size,t.volume_id AS target_volume_id,
                t.issue_number AS target_number,v.title AS target_title
                FROM issues i LEFT JOIN canonical_issue_files o ON o.issue_id=i.id
                LEFT JOIN files f ON f.id=o.file_id
                LEFT JOIN issues t ON t.id=o.target_issue_id
                LEFT JOIN volumes v ON v.id=t.volume_id WHERE ''' + predicate +
                ' ORDER BY i.id,o.role,o.file_id,o.coverage_id', batch)
            for row in values:
                entry = result.setdefault(row['issue_id'], {'issue_id': row['issue_id'],
                    'direct_files': [], 'collected_coverage': [], 'owned': False, 'state': 'none'})
                if row['role'] == 'direct':
                    entry['direct_files'].append({'id': row['file_id'], 'filepath': row['filepath'], 'size': row['size']})
                elif row['role'] == 'collected':
                    entry['collected_coverage'].append({key: value for key, value in row.items() if key != 'issue_id'})
        for entry in result.values():
            direct, collected = bool(entry['direct_files']), bool(entry['collected_coverage'])
            entry['owned'] = direct or collected
            entry['state'] = 'direct_and_collected' if direct and collected else (
                'direct' if direct else 'collected' if collected else 'none')
    return result

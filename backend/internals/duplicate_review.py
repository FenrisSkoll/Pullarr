"""Read-only, bounded ownership evidence for duplicate review.

Historical coverage remains tied to stable file identities. The current health
snapshot excludes schema-66 inactive artifacts; coverage history remains visible
as evidence. This reader changes neither validity nor ownership and does not
authorize quarantine execution.
"""

import sqlite3
from pathlib import Path

from backend.base.duplicate_review import DuplicateReviewError
from backend.base.library_health import canonical, fingerprint
from backend.internals.library_health import read_snapshot
from backend.internals.switch_review import dependencies

MAX_ROWS = 20000
MAX_BYTES = 16 * 1024 * 1024


def read_duplicate_state(database, scope):
    health = read_snapshot(database, scope, MAX_ROWS)
    db = sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        extra = {}
        size = 0
        for name, sql in (
            ('claims', 'SELECT * FROM bibliographic_content_claims ORDER BY id'),
            ('coverage_history', 'SELECT * FROM file_content_coverage ORDER BY id'),
            ('canonical', 'SELECT * FROM canonical_issue_files ORDER BY issue_id,file_id,role,coverage_id'),
            ('reservations', 'SELECT path_key,job_id FROM organization_reservations ORDER BY path_key'),
            ('jobs', '''SELECT id,state,plan_digest,json_extract(intent,'$.volume_id') volume_id,
                json_extract(intent,'$.source') source,json_extract(intent,'$.target') target
                FROM organization_jobs ORDER BY id'''),
        ):
            values = []
            for row in db.execute(sql):
                value = dict(row)
                size += len(canonical(value))
                if len(values) >= MAX_ROWS or size > MAX_BYTES:
                    raise DuplicateReviewError('duplicate_ownership_snapshot_limit')
                values.append(value)
            extra[name] = values
        # Existing bounded dependency observations include intake, acquisition
        # and Wanted. This is per volume, never per file/issue/finding.
        extra['dependencies'] = []
        for volume_id in health['selected']:
            values = [dict(value, owner_volume_id=volume_id)
                      for value in dependencies(db.cursor(), volume_id)]
            size += len(canonical(values))
            if len(extra['dependencies']) + len(values) > MAX_ROWS or size > MAX_BYTES:
                raise DuplicateReviewError('duplicate_dependency_limit')
            extra['dependencies'].extend(values)
    finally:
        db.rollback()
        db.close()
    # No read transaction spans file IO. The service also repeats this complete
    # acquisition after hashing; a mixed DB/FS snapshot is never an apply token.
    current = read_snapshot(database, scope, MAX_ROWS)
    if current['digest'] != health['digest']:
        raise DuplicateReviewError('duplicate_state_changed_during_read')
    return health, extra, fingerprint((health['digest'], extra))

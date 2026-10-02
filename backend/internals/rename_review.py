"""Additional persisted naming evidence absent from legacy plan repr digests."""

from backend.base.library_health import fingerprint


def naming_evidence(cursor, volume_id):
    # Rich number facts intentionally have repr=False in the legacy matching
    # model. Do not mistake its old compatibility fingerprint for this guard.
    data = {}
    for table, query in (
        ('controls', 'SELECT special_version,special_version_locked FROM volumes WHERE id=?'),
        ('provenance', 'SELECT * FROM classification_provenance WHERE volume_id=?'),
        ('control_history', 'SELECT * FROM classification_control_state WHERE volume_id=?'),
        ('numbers', 'SELECT n.* FROM issue_number_facts n JOIN issues i ON i.id=n.issue_id WHERE i.volume_id=? ORDER BY n.issue_id'),
        ('dates', 'SELECT d.* FROM issue_date_facts d JOIN issues i ON i.id=d.issue_id WHERE i.volume_id=? ORDER BY d.issue_id,d.source_field'),
        ('variants', 'SELECT v.* FROM issue_variant_of v JOIN issues i ON i.id=v.issue_id WHERE i.volume_id=? ORDER BY v.issue_id')):
        rows = cursor.execute(query, (volume_id,)).fetchmany(20001)
        if len(rows) > 20000:
            raise ValueError('rename_evidence_limit')
        data[table] = [tuple(row) for row in rows]
    return fingerprint(data)

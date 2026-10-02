"""Provider-neutral opt-in issue-facts/v1 transport. Local reads only."""

import json
from dataclasses import asdict
from typing import Any, Dict, List

from backend.base.issue_facts import DatePrecision, presentation_key
from backend.internals.db import get_db
from backend.internals.issue_facts import load_records


def snapshot_status(volume_id: int):
    row = get_db().execute('SELECT value FROM config WHERE key=?',
                           ('provider_snapshot:' + str(volume_id),)).fetchone()
    if row is None:
        return None
    data = json.loads(row[0])
    return {key: data[key] for key in ('policy', 'membership_digest', 'acquired_at',
            'expected_count', 'retained_missing_issue_ids')}


def rich_issue_rows(volume_id: int) -> List[Dict[str, Any]]:
    cursor = get_db()
    records = load_records(cursor, volume_id=volume_id)
    rows = {row['id']: dict(row) for row in cursor.execute('''SELECT id,volume_id,comicvine_id,
        issue_number,calculated_issue_number,title,date,description,monitored FROM issues WHERE volume_id=?''',
        (volume_id,))}
    files: Dict[int, list] = {}
    for iid, fid, path, size in cursor.execute('''SELECT i.id,f.id,f.filepath,f.size FROM issues i
        JOIN issues_files x ON x.issue_id=i.id JOIN active_files f ON f.id=x.file_id
        WHERE i.volume_id=? ORDER BY f.filepath,f.id''', (volume_id,)):
        files.setdefault(iid, []).append({'id': fid, 'filepath': path, 'size': size})
    # Existing projected rows retain their established visible ordering. Rich
    # records use the canonical presentation policy, never float fiction.
    legacy = all(r.legacy_number is not None for r in records)
    result = []
    for position, record in enumerate(sorted(records, key=lambda r: presentation_key(r, legacy_order=legacy))):
        row = rows[record.id]
        row.update(schema='issue-facts/v1', files=files.get(record.id, []),
                   display_order=position, legacy_projection_available=record.legacy_number is not None,
                   number=None, bibliographic_dates=[], operational_date=None,
                   variant_of=asdict(record.variant_of) if record.variant_of else None,
                   date_display=row['date'])
        if record.facts is not None:
            facts = record.facts
            row['issue_number'] = facts.number.raw_label
            row['number'] = dict(asdict(facts.number), interpretation=facts.number.interpretation.value)
            row['bibliographic_dates'] = [dict(asdict(d), kind=d.kind.value, precision=d.precision.value)
                                          for d in facts.dates]
            selected = facts.operational_date
            if selected is not None:
                row['operational_date'] = selected.source_field
                row['date_display'] = (f'{selected.year:04}' if selected.year else '')
                if selected.month is not None:
                    row['date_display'] += f'-{selected.month:02}'
                if selected.day is not None:
                    row['date_display'] += f'-{selected.day:02}'
                if selected.precision is DatePrecision.UNSUPPORTED_TEXT:
                    row['date_display'] = selected.raw_value
        result.append(row)
    return result

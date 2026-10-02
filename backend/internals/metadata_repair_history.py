"""Bounded successful repair history; original local IDs are historical values."""

import json

from backend.base.metadata_repair import RepairError


def _rows(cursor, sql, params):
    cursor.execute(sql, params)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def by_session(cursor, session):
    rows = _rows(cursor, 'SELECT * FROM metadata_repair_receipts WHERE session_id=?', (session,))
    return rows[0] if rows else None


def get_receipt(cursor, identifier):
    rows = _rows(cursor, 'SELECT * FROM metadata_repair_receipts WHERE id=?', (identifier,))
    if not rows:
        raise RepairError('repair_receipt_unavailable')
    return rows[0]


def page(cursor, volume_id, *, offset=0, limit=50):
    _page(offset, limit)
    if type(volume_id) is not int or volume_id <= 0:
        raise RepairError('invalid_repair_volume')
    return _rows(cursor, '''SELECT * FROM metadata_repair_receipts WHERE volume_id=?
        ORDER BY applied_at DESC,id DESC LIMIT ? OFFSET ?''', (volume_id, limit, offset))


def field_page(cursor, identifier, *, offset=0, limit=50):
    _page(offset, limit)
    get_receipt(cursor, identifier)
    rows = _rows(cursor, '''SELECT * FROM metadata_repair_fields WHERE repair_id=?
        ORDER BY ordinal LIMIT ? OFFSET ?''', (identifier, limit, offset))
    for row in rows:
        row['before_value'] = json.loads(row['before_value'])
        row['after_value'] = json.loads(row['after_value'])
    return rows


def _page(offset, limit):
    if type(offset) is not int or not 0 <= offset <= 40000 or type(limit) is not int or not 1 <= limit <= 100:
        raise RepairError('invalid_repair_page')

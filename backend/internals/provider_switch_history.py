"""Bounded successful transition receipts; never a metadata undo journal."""

from backend.base.switch_review import SwitchReviewError
from backend.internals.switch_review import rows


def by_session(cursor, identifier):
    result = rows(cursor, 'SELECT * FROM provider_switch_receipts WHERE session_id=?', (identifier,), 1)
    return result[0] if result else None


def history(cursor, volume_id, *, before_generation=None, limit=25):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise SwitchReviewError('invalid_history_limit')
    return rows(cursor, '''SELECT * FROM provider_switch_receipts WHERE volume_id=?
        AND (? IS NULL OR target_generation<?) ORDER BY target_generation DESC LIMIT ?''',
        (volume_id, before_generation, before_generation, limit), limit)


def receipt(cursor, identifier, *, detail=False, offset=0, limit=100):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise SwitchReviewError('invalid_receipt_page')
    cursor.execute('SAVEPOINT switch_history_read')
    try:
        found = rows(cursor, 'SELECT * FROM provider_switch_receipts WHERE id=?', (identifier,), 1)
        if not found:
            raise SwitchReviewError('switch_receipt_unavailable')
        result = found[0]
        if detail:
            for key, table, order in (
                ('issues', 'provider_switch_issue_receipts', 'local_issue_id'),
                ('claims', 'provider_switch_claim_receipts', 'old_claim_id'),
                ('coverage', 'provider_switch_coverage_receipts', 'old_coverage_id')):
                result[key] = rows(cursor, f'SELECT * FROM {table} WHERE switch_id=? ORDER BY {order} LIMIT ? OFFSET ?',
                                   (identifier, limit, offset), limit)
            result['page'] = dict(offset=offset, limit=limit)
        return result
    finally:
        cursor.execute('RELEASE switch_history_read')

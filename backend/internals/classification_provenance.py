"""Application receipts share the successful value write's transaction.

No classifier, remote acquisition or filesystem operation belongs here.
Supported writes replace the invalidation triggered by the SQL assignment.
Savepoints never commit a caller's existing transaction.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class ApplicationKind(Enum):
    AUTOMATIC = 'automatic_decision'
    EXPLICIT = 'explicit_selection'
    LEGACY_DEFAULT = 'legacy_default_application'


class InputScope(Enum):
    DECISION_TIME = 'decision_time_inputs'
    DURABLE = 'durable_fields_only'
    EXPLICIT = 'explicit_selection'
    LEGACY_DEFAULT = 'legacy_default_application'


@dataclass(frozen=True)
class ClassificationApplicationReceipt:
    """Successful in-transaction application; durable only with caller commit."""
    volume_id: int
    applied_value: object
    application_kind: ApplicationKind
    input_scope: str
    recorded_at: str
    schema_version: str = 'classification-provenance/v1'


@contextmanager
def transaction(cursor):
    cursor.execute('SAVEPOINT classification_application')
    try:
        yield
        cursor.execute('RELEASE classification_application')
    except BaseException:
        cursor.execute('ROLLBACK TO classification_application')
        cursor.execute('RELEASE classification_application')
        raise


def rows(cursor, sql, params=()):
    cursor.execute(sql, params)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def revisions(cursor, volume_ids):
    """Bounded pre-acquisition guard; catches same-value manual writes too."""
    ids = tuple(dict.fromkeys(volume_ids))
    result = {i: 0 for i in ids}
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        result.update(cursor.execute('SELECT volume_id,revision FROM classification_state WHERE volume_id IN (' +
                                     ','.join('?' for _ in batch) + ')', batch))
    return result


def decision_payload(decision, scope):
    """Serialize the evaluator's answer, never reconstruct its policy."""
    facts = decision.facts
    age = facts.issue_age
    evidence = []
    for axis, assessment in (('physical', decision.physical_evidence),
                             ('publication', decision.publication_evidence)):
        item = assessment.evidence
        if item and any(len(value) > 512 for value in (item.provider, item.provider_id, item.source_field)):
            raise ValueError('Classification evidence identity exceeds receipt bound')
        raw = item.raw_value if item else None
        normalized = (getattr(item, 'physical_format', None) if axis == 'physical'
                      else getattr(item, 'publication_kind', None))
        evidence.append(dict(axis=axis,
            availability=('available_with_value' if item else 'available_but_absent')
                if scope == InputScope.DECISION_TIME else 'not_available_in_scope',
            disposition=assessment.disposition.value,
            provider=item.provider if item else None,
            provider_id=item.provider_id if item else None,
            source_field=item.source_field if item else None,
            raw_value=raw[:512] if raw is not None else None,
            raw_truncated=raw is not None and len(raw) > 512,
            normalized_value=normalized.value if normalized else None))
    return dict(value=decision.value.value, source=decision.source.value, reason=decision.reason.value,
        policy_id=decision.policy_id, evaluated_at=decision.evaluated_at.isoformat(),
        input_scope=scope.value, replay_status='explanation_complete_replay_incomplete',
        lock_input=decision.locked, issue_count=facts.issue_count,
        volume_numbered_count=facts.volume_numbered_count,
        issue_date=(decision.evaluated_at - age).date().isoformat() if age is not None else None,
        age_seconds=age.total_seconds() if age is not None else None, evidence=evidence)


def apply(cursor, volume_id, value, *, decision=None, scope=InputScope.DECISION_TIME,
          kind=ApplicationKind.EXPLICIT, expected_revision=None):
    """True only for an actual application. A losing guard writes no receipt."""
    if decision is not None:
        if decision.value != value or expected_revision is None:
            raise ValueError('Automatic application requires exact decision and revision')
        kind = ApplicationKind.AUTOMATIC
        payload = decision_payload(decision, scope)
    else:
        if kind not in (ApplicationKind.EXPLICIT, ApplicationKind.LEGACY_DEFAULT):
            raise ValueError('Missing automatic decision')
        payload = dict(value=value.value, source=None, reason=None, policy_id=None, evaluated_at=None,
            input_scope=kind.value, replay_status='not_applicable', lock_input=False,
            issue_count=None, volume_numbered_count=None, issue_date=None, age_seconds=None, evidence=[])
    with transaction(cursor):
        if decision is None:
            lock = cursor.execute('SELECT special_version_locked FROM volumes WHERE id=?', (volume_id,)).fetchone()
            if lock is None:
                return False
            payload['lock_input'] = bool(lock[0])
            cursor.execute('UPDATE volumes SET special_version=? WHERE id=?', (value.value, volume_id))
        else:
            cursor.execute('''UPDATE volumes SET special_version=? WHERE id=? AND special_version_locked=0
                AND COALESCE((SELECT revision FROM classification_state WHERE volume_id=volumes.id),0)=?''',
                (value.value, volume_id, expected_revision))
        if cursor.rowcount != 1:
            return False
        receipt = ClassificationApplicationReceipt(volume_id, payload['value'], kind, payload['input_scope'],
                                                   datetime.now(timezone.utc).isoformat())
        cursor.execute('''INSERT INTO classification_provenance VALUES(
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            receipt.volume_id, receipt.schema_version, receipt.applied_value, receipt.application_kind.value,
            payload['source'], payload['reason'], payload['policy_id'], payload['evaluated_at'],
            receipt.recorded_at, receipt.input_scope, payload['replay_status'],
            payload['lock_input'], payload['issue_count'], payload['volume_numbered_count'],
            payload['issue_date'], payload['age_seconds']))
        cursor.executemany('''INSERT INTO classification_evidence_receipts VALUES(
            :volume_id,:axis,:availability,:disposition,:provider,:provider_id,:source_field,
            :raw_value,:raw_truncated,:normalized_value)''',
            (dict(item, volume_id=volume_id) for item in payload['evidence']))
        cursor.execute('UPDATE classification_state SET invalidated=0 WHERE volume_id=?', (volume_id,))
    return True


def control(cursor, volume_id, locked, context='operator'):
    with transaction(cursor):
        cursor.execute('UPDATE volumes SET special_version_locked=? WHERE id=?', (locked, volume_id))
        if cursor.rowcount:
            cursor.execute('''INSERT INTO classification_control_state VALUES(?,?,?,?)
                ON CONFLICT(volume_id) DO UPDATE SET action=excluded.action,
                occurred_at=excluded.occurred_at,context=excluded.context''',
                (volume_id, 'lock' if locked else 'unlock', datetime.now(timezone.utc).isoformat(), context))


def summaries(cursor, volume_ids):
    ids = tuple(dict.fromkeys(volume_ids))
    result = {}
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        for row in rows(cursor, '''SELECT v.id,v.special_version,p.applied_value,p.application_kind,p.source,p.reason,
                p.policy_id,s.invalidated FROM volumes v
                LEFT JOIN classification_provenance p ON p.volume_id=v.id
                LEFT JOIN classification_state s ON s.volume_id=v.id WHERE v.id IN (''' +
                ','.join('?' for _ in batch) + ')', batch):
            status = ('recorded' if row['application_kind'] and row['applied_value'] == row['special_version']
                      and not row['invalidated'] else 'invalidated' if row['invalidated'] else 'unavailable')
            result[row['id']] = dict(status=status, source=row['source'] if status == 'recorded' else None,
                reason=row['reason'] if status == 'recorded' else None)
    return result


def details(cursor, volume_id):
    with transaction(cursor):
        volume = cursor.execute('SELECT special_version,special_version_locked FROM volumes WHERE id=?', (volume_id,)).fetchone()
        if volume is None:
            raise ValueError('Volume unavailable')
        status = summaries(cursor, [volume_id])[volume_id]['status']
        receipt = rows(cursor, 'SELECT * FROM classification_provenance WHERE volume_id=?', (volume_id,))
        provenance = dict(status=status, source=None, reason=None)
        if status == 'recorded':
            provenance.update(receipt[0])
            provenance['evidence'] = rows(cursor, 'SELECT * FROM classification_evidence_receipts WHERE volume_id=? ORDER BY axis', (volume_id,))
        action = rows(cursor, 'SELECT action,occurred_at,context FROM classification_control_state WHERE volume_id=?', (volume_id,))
        return dict(schema='classification-details/v1', stored=dict(value=volume[0], locked=bool(volume[1])),
                    provenance=provenance, last_control_action=action[0] if action else None,
                    current_evaluation=None, evaluation_clock_convention='naive_local_datetime')

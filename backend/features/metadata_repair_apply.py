"""One same-authority DB repair transaction. No provider or filesystem calls."""

from datetime import datetime, timezone
from uuid import uuid4

from backend.base.library_health import canonical
from backend.base.metadata_repair import POLICY, Field, RepairError
from backend.implementations.metadata.repair_fields import (ISSUE_FIELDS,
                                                            VOLUME_FIELDS,
                                                            field_review)
from backend.implementations.metadata.switch_values import (
    bibliography_snapshot, facts)
from backend.internals.bibliography import persist_bibliography
from backend.internals.classification_provenance import (InputScope,
                                                         apply as classify,
                                                         decision_payload)
from backend.internals.issue_facts import write_facts
from backend.internals.metadata_repair_history import by_session
from backend.internals.provider_authority import (AuthorityToken,
                                                  require_current, serialized)
from backend.internals.switch_review import load_local


def apply_review(service, cursor, identifier, revision, digest, *, confirmed, expected_authority):
    if (confirmed is not True or type(revision) is not int or revision < 0
            or not isinstance(identifier, str) or not 1 <= len(identifier) <= 128
            or not isinstance(digest, str) or len(digest) != 64 or not isinstance(expected_authority, AuthorityToken)):
        raise RepairError('explicit_repair_confirmation_required')
    with service._lock:
        with serialized(cursor):
            previous = by_session(cursor, identifier)
            if previous is not None:
                if (previous['revision'] != revision or previous['review_digest'] != digest or
                    (previous['volume_id'], previous['provider'], previous['provider_id'], previous['authority_generation']) !=
                    (expected_authority.volume_id, expected_authority.provider, expected_authority.provider_id, expected_authority.generation)):
                    raise RepairError('repair_retry_identity_mismatch')
                return dict(previous, state='already_applied')
            session = service.get(cursor, identifier)
            if session.revision != revision or session.digest != digest or session.authority != expected_authority:
                raise RepairError('stale_repair_confirmation')
            require_current(cursor, (expected_authority,))
            preview = session.preview.view()
            if preview['blockers']:
                raise RepairError('repair_blocked')
            if service.task_observer(expected_authority.volume_id):
                raise RepairError('repair_active_process_task')
            if not preview['changes']:
                return dict(state='no_changes', receipt=None)
            service.fault_hook('before_mutation')
            local = session.local.view()
            _, owners = field_review(session.local, session.target)
            for field in session.fields:
                if field.selection.key not in session.selected or field.change == 'unchanged':
                    continue
                key, iid = field.selection.field, field.selection.local_id
                after = field.values.view()['after']
                if key == Field.BIBLIOGRAPHY:
                    remote = session.target.data.view()
                    remote['issues'] = [r for r in remote['issues'] if r['provider_id'] in owners]
                    persist_bibliography(cursor, bibliography_snapshot(remote, session.authority.provider), iid, owners)
                    service.fault_hook('bibliography')
                elif key == Field.FACTS and field.selection.scope == 'issue':
                    cursor.execute('''UPDATE issues SET issue_number=?,calculated_issue_number=?,date=? WHERE id=?''',
                        (after['issue_number'], after['calculated_issue_number'], after['date'], iid))
                    write_facts(cursor, iid, facts(after['facts']))
                    cursor.execute('DELETE FROM issue_variant_of WHERE issue_id=?', (iid,))
                    relation = after['variant_of']
                    if relation:
                        cursor.execute('INSERT INTO issue_variant_of VALUES(?,?,?,?)',
                            (iid, relation['provider'], relation['provider_id'], relation['provenance']))
                    service.fault_hook('canonical_facts')
                else:
                    allowed = VOLUME_FIELDS if field.selection.scope == 'volume' else ISSUE_FIELDS
                    if key not in allowed or key == Field.FACTS:
                        raise RepairError('unsupported_repair_field')
                    table = 'volumes' if field.selection.scope == 'volume' else 'issues'
                    # Enum allowlist only. No caller-supplied SQL identifiers.
                    cursor.execute(f'UPDATE {table} SET {key.value}=? WHERE id=?', (after, iid))
                    if cursor.rowcount != 1:
                        raise RepairError('repair_owner_disappeared')
                    service.fault_hook('volume_fields' if table == 'volumes' else 'issue_fields')
            classification = preview['classification']
            if classification['action'] == 'apply_reviewed_candidate':
                expected = local['classification_revision'][0]['revision'] if local['classification_revision'] else 0
                if decision_payload(session.candidate, InputScope.DECISION_TIME) != classification['candidate']:
                    raise RepairError('classification_candidate_mismatch')
                if not classify(cursor, session.authority.volume_id, session.candidate.value,
                                decision=session.candidate, expected_revision=expected):
                    raise RepairError('classification_control_changed')
            service.fault_hook('classification')
            after = load_local(cursor, session.authority.volume_id, session.target).view()
            for name in ('selected', 'external', 'issue_refs', 'direct', 'general', 'ownership',
                         'claims', 'coverage', 'valid_coverage', 'claim_evidence', 'graph', 'artwork'):
                if after[name] != local[name]:
                    raise RepairError('repair_preservation_failed')
            require_current(cursor, (expected_authority,))
            identifier = uuid4().hex
            origin = session.origin.view()
            bibliography_action = ('persist_supplied_observations' if any(
                r['field'] == Field.BIBLIOGRAPHY.value for r in preview['changes']) else 'preserve')
            receipt = dict(id=identifier, session_id=session.id, volume_id=session.authority.volume_id,
                provider=session.authority.provider, provider_id=session.authority.provider_id,
                authority_generation=session.authority.generation, revision=revision, review_digest=digest,
                local_digest=session.local.digest, target_digest=session.target.data.digest,
                worklist_id=origin['worklist_id'], worklist_digest=origin['manifest_digest'], policy=POLICY,
                applied_at=datetime.now(timezone.utc).isoformat(), actor='operator',
                field_count=len(preview['changes']), classification_action=classification['action'],
                classification_summary=canonical(classification), bibliography_action=bibliography_action)
            cursor.execute('INSERT INTO metadata_repair_receipts(' + ','.join(receipt) + ') VALUES(' +
                           ','.join('?' for _ in receipt) + ')', tuple(receipt.values()))
            cursor.executemany('INSERT INTO metadata_repair_fields VALUES(?,?,?,?,?,?,?)',
                ((identifier, n, r['scope'], r['local_id'], r['field'], canonical(r['before']), canonical(r['after']))
                 for n, r in enumerate(preview['changes'])))
            service.fault_hook('receipt_details')
            if cursor.execute('SELECT COUNT(*) FROM metadata_repair_fields WHERE repair_id=?', (identifier,)).fetchone()[0] != receipt['field_count']:
                raise RepairError('repair_receipt_count_mismatch')
            service.fault_hook('before_commit')
        service.delete(session.id)
        return dict(receipt, state='applied')

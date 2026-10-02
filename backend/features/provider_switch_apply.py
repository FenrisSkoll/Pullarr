"""Explicit reviewed authority transition. No provider IO or filesystem effects.

All callees below are cursor-based non-committing persistence primitives. The
review service lock protects local revisions; SQLite serialization and durable
session identity protect other connections/processes and post-commit retries.
"""

import json
from datetime import datetime, timezone
from uuid import uuid4

from backend.base.provider_switch import POLICY
from backend.base.switch_review import SwitchReviewError
from backend.implementations.metadata.switch_values import (
    bibliography_snapshot, facts)
from backend.internals.bibliography import persist_bibliography
from backend.internals.classification_provenance import (InputScope,
                                                         apply as
                                                         apply_classification,
                                                         decision_payload)
from backend.internals.issue_facts import write_facts
from backend.internals.provider_authority import (AuthorityToken,
                                                  require_current, serialized)
from backend.internals.provider_switch_history import by_session


def _identity(cursor, table, key, local, provider, identity, provenance, existing):
    """Conflict validation uses the reviewed batched identity set, not N+1 reads."""
    old = existing.get((local, provider))
    if old is not None:
        if old != identity:
            raise SwitchReviewError('established_reference_conflict')
        return  # Retain exact pre-existing provenance and fetch timestamp.
    if provider == 'comicvine':
        numeric = int(identity)
        if str(numeric) != identity or not -(2**63) <= numeric < 2**63:
            raise SwitchReviewError('invalid_comicvine_projection')
        owner_table = 'volumes' if key == 'volume_id' else 'issues'
        # Existing compatibility triggers create the genuine qualified reference.
        cursor.execute(f'UPDATE {owner_table} SET comicvine_id=? WHERE id=?', (numeric, local))
        cursor.execute(f'UPDATE {table} SET provenance=? WHERE {key}=? AND provider=?', (provenance, local, provider))
    else:
        cursor.execute(f'INSERT INTO {table}({key},provider,provider_id,provenance) VALUES(?,?,?,?)',
                       (local, provider, identity, provenance))
    existing[(local, provider)] = identity


def _coverage(cursor, session, switch_id, now, fault):
    local, preview = session.local.view(), session.preview.view()
    claims = {row['id']: row for row in local['claims']}
    coverage = {row['id']: row for row in local['coverage']}
    changes, links = [], []
    for action in preview['content']['claims']:
        original = claims[action['claim_id']]
        successor = dict(original, id=uuid4().hex, created_at=now, retired_at=None, supersedes=original['id'])
        for endpoint, change in action['endpoints'].items():
            successor[endpoint + '_provider'] = change['new']['provider']
            successor[endpoint + '_provider_id'] = change['new']['provider_id']
        cursor.execute('UPDATE bibliographic_content_claims SET retired_at=? WHERE id=? AND retired_at IS NULL',
                       (now, original['id']))
        if cursor.rowcount != 1:
            raise SwitchReviewError('stale_content_claim')
        cursor.execute('''INSERT INTO bibliographic_content_claims VALUES(:id,:target_provider,:target_provider_id,
            :source_provider,:source_provider_id,:kind,:authority,:policy,:created_at,:retired_at,:supersedes)''', successor)
        cursor.execute('''INSERT INTO bibliographic_content_claim_evidence
            SELECT ?,provider,edge_id,snapshot_id,origin_issue,target_issue,origin_story,target_story,active
            FROM bibliographic_content_claim_evidence WHERE claim_id=?''', (successor['id'], original['id']))
        changes.append((switch_id, original['id'], successor['id']))
        fault('claim_supersession')
        for identity in action['valid_coverage']:
            old = coverage[identity]
            new = dict(old, id=uuid4().hex, claim_id=successor['id'], created_at=now, retired_at=None)
            cursor.execute('UPDATE file_content_coverage SET retired_at=? WHERE id=? AND retired_at IS NULL', (now, identity))
            if cursor.rowcount != 1:
                raise SwitchReviewError('stale_content_coverage')
            cursor.execute('''INSERT INTO file_content_coverage VALUES(:id,:file_id,:target_issue_id,:source_issue_id,
                :claim_id,:original_file_id,:original_target_id,:original_source_id,:policy,:created_at,:retired_at)''', new)
            links.append((switch_id, identity, new['id']))
            fault('coverage_rebinding')
    return changes, links


def _apply_values(cursor, session, switch_id, fault):
    local, preview, remote = session.local.view(), session.preview.view(), session.target.data.view()
    volume_id, provider = session.volume_id, session.target.reference.provider
    now = datetime.now(timezone.utc)
    issue_refs = {(r['issue_id'], r['provider']): r['provider_id'] for r in local['issue_refs']}
    volume_refs = {(volume_id, r['provider']): r['provider_id'] for r in local['external']}
    owners, details = {}, []
    _identity(cursor, 'volume_external_ids', 'volume_id', volume_id, provider,
              session.target.reference.provider_id, 'operator_provider_switch', volume_refs)
    for row in preview['issues']:
        mapping = row['correspondence']
        identity, iid = mapping['target']['provider_id'], row['local']['id']
        owners[identity] = iid
        _identity(cursor, 'issue_external_ids', 'issue_id', iid, provider, identity, 'operator_provider_switch', issue_refs)
        details.append((switch_id, iid, mapping['source']['provider'], mapping['source']['provider_id'],
            provider, identity, mapping['kind'], json.dumps(mapping['evidence'], ensure_ascii=True), 0))
    fault('identities')
    for row in preview['target_only']:
        iid = cursor.execute('INSERT INTO issues(volume_id,issue_number,monitored) VALUES(?,?,?)',
                             (volume_id, row['issue_number'], row['monitored'])).lastrowid
        owners[row['provider_id']] = iid
        _identity(cursor, 'issue_external_ids', 'issue_id', iid, provider, row['provider_id'], 'provider', issue_refs)
        details.append((switch_id, iid, None, None, provider, row['provider_id'], 'target_only_new', '[]', 1))
        fault('target_only')
    for assertion in remote['assertions']:
        is_volume = assertion['entity'] == 'volume'
        _identity(cursor, 'volume_external_ids' if is_volume else 'issue_external_ids',
            'volume_id' if is_volume else 'issue_id', volume_id if is_volume else owners[assertion['owner_id']],
            assertion['provider'], assertion['provider_id'], assertion['provenance'], volume_refs if is_volume else issue_refs)
    values = dict(remote['volume'], alt_title=(remote['volume']['aliases'] or [None])[0])
    fields = remote['application_fields']['volume']
    if not set(fields) <= {'title', 'year', 'publisher', 'description', 'site_url', 'volume_number', 'alt_title'}:
        raise SwitchReviewError('unsupported_volume_application_field')
    cursor.execute('UPDATE volumes SET ' + ','.join(field + '=?' for field in fields) + ' WHERE id=?',
                   (*(values[field] for field in fields), volume_id))
    fault('volume_metadata')
    fields = remote['application_fields']['issue']
    if not set(fields) <= {'issue_number', 'calculated_issue_number', 'title', 'date', 'description'}:
        raise SwitchReviewError('unsupported_issue_application_field')
    cursor.executemany('UPDATE issues SET ' + ','.join(field + '=?' for field in fields) + ' WHERE id=?',
        ((*(row[field] for field in fields), owners[row['provider_id']]) for row in remote['issues']))
    for row in remote['issues']:
        iid = owners[row['provider_id']]
        write_facts(cursor, iid, facts(row['facts']))
        cursor.execute('DELETE FROM issue_variant_of WHERE issue_id=?', (iid,))
        relation = row['variant_of']
        if relation:
            cursor.execute('INSERT INTO issue_variant_of VALUES(?,?,?,?)',
                (iid, relation['provider'], relation['provider_id'], relation['provenance']))
        fault('canonical_facts')
    if remote['bibliography'] is not None or any(r['bibliography'] is not None for r in remote['issues']):
        persist_bibliography(cursor, bibliography_snapshot(remote, provider), volume_id, owners)
    fault('bibliography')
    candidate = session.candidate
    if decision_payload(candidate, InputScope.DECISION_TIME) != preview['classification']['target_unlocked_evaluation']:
        raise SwitchReviewError('reviewed_classification_mismatch')
    if not local['volume']['special_version_locked']:
        revision = local['classification_revision'][0]['revision'] if local['classification_revision'] else 0
        if not apply_classification(cursor, volume_id, candidate.value, decision=candidate, expected_revision=revision):
            raise SwitchReviewError('stale_classification_control')
    fault('classification')
    changes, links = _coverage(cursor, session, switch_id, now.timestamp(), fault)
    source = preview['source']
    cursor.execute('''UPDATE volumes SET metadata_provider=?,authority_generation=authority_generation+1
        WHERE id=? AND metadata_provider=? AND authority_generation=?''',
        (provider, volume_id, source['provider'], preview['source_generation']))
    if cursor.rowcount != 1:
        raise SwitchReviewError('stale_metadata_authority')
    acquired = remote['receipt']['acquired_at']
    if 'membership_digest' in remote['receipt']:
        snapshot_receipt = dict(remote['receipt'], provider=provider,
            provider_id=session.target.reference.provider_id, retained_missing_issue_ids=[])
        cursor.execute('''INSERT INTO config(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
            ('provider_snapshot:' + str(volume_id), json.dumps(snapshot_receipt, separators=(',', ':'))))
    if provider == 'comicvine':
        cursor.execute('UPDATE volumes SET last_cv_fetch=? WHERE id=?', (acquired, volume_id))
    else:
        cursor.execute('UPDATE volume_external_ids SET last_fetch=? WHERE volume_id=? AND provider=?', (acquired, volume_id, provider))
    fault('authority_generation')
    # Compare exact local issue/file ownership pairs, not coverage receipt IDs.
    related = sorted({r['id'] for r in local['endpoints']})
    after = set()
    for start in range(0, len(related), 350):
        batch = related[start:start + 350]
        after.update(tuple(row) for row in cursor.execute(
            'SELECT DISTINCT issue_id,file_id FROM canonical_issue_files WHERE issue_id IN (' +
            ','.join('?' for _ in batch) + ')', batch))
    if after != {(r['issue_id'], r['file_id']) for r in local['ownership']}:
        raise SwitchReviewError('ownership_preservation_failed')
    valid_new = set()
    for start in range(0, len(links), 350):
        ids = [r[2] for r in links[start:start + 350]]
        valid_new.update(r[0] for r in cursor.execute('SELECT id FROM valid_file_content_coverage WHERE id IN (' +
                                                    ','.join('?' for _ in ids) + ')', ids))
    if valid_new != {r[2] for r in links}:
        raise SwitchReviewError('coverage_preservation_failed')
    locked = local['volume']['special_version_locked']
    summary = dict(id=switch_id, volume_id=volume_id, session_id=session.id, revision=session.revision,
        source_provider=source['provider'], source_provider_id=source['provider_id'], source_generation=preview['source_generation'],
        target_provider=provider, target_provider_id=session.target.reference.provider_id, target_generation=preview['source_generation'] + 1,
        policy=POLICY, schema_version='provider-switch-receipt/v1', mapping_digest=preview['mapping_digest'],
        local_digest=session.local.digest, target_digest=session.target.data.digest, mapped_count=len(preview['issues']),
        added_count=len(preview['target_only']), unresolved_count=0, claim_count=len(changes), coverage_count=len(links),
        classification_action='preserved_locked' if locked else 'automatic_application',
        classification_value=local['volume']['special_version'] if locked else candidate.value.value,
        classification_policy=None if locked else candidate.policy_id, classification_source=None if locked else candidate.source.value,
        classification_reason=None if locked else candidate.reason.value, applied_at=now.isoformat(), actor='operator')
    cursor.execute('INSERT INTO provider_switch_receipts(' + ','.join(summary) + ') VALUES(' +
                   ','.join('?' for _ in summary) + ')', tuple(summary.values()))
    cursor.executemany('INSERT INTO provider_switch_issue_receipts VALUES(?,?,?,?,?,?,?,?,?)', details)
    cursor.executemany('INSERT INTO provider_switch_claim_receipts VALUES(?,?,?)', changes)
    cursor.executemany('INSERT INTO provider_switch_coverage_receipts VALUES(?,?,?)', links)
    fault('receipt_details')
    for table, expected in (('provider_switch_issue_receipts', summary['mapped_count'] + summary['added_count']),
                            ('provider_switch_claim_receipts', len(changes)), ('provider_switch_coverage_receipts', len(links))):
        if cursor.execute(f'SELECT COUNT(*) FROM {table} WHERE switch_id=?', (switch_id,)).fetchone()[0] != expected:
            raise SwitchReviewError('switch_receipt_count_mismatch')
    fault('before_commit')
    return summary


def apply_review(service, cursor, identifier, revision, mapping_digest, *, confirmed, expected_authority):
    if confirmed is not True or type(revision) is not int or revision < 1:
        raise SwitchReviewError('explicit_switch_confirmation_required')
    if (not isinstance(expected_authority, AuthorityToken) or not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 128 or not isinstance(mapping_digest, str)
            or len(mapping_digest) != 64):
        raise SwitchReviewError('invalid_switch_apply_identity')
    with service._lock:
        with serialized(cursor):
            previous = by_session(cursor, identifier)
            if previous is not None:
                if (previous['revision'] != revision or previous['mapping_digest'] != mapping_digest or
                    (previous['volume_id'], previous['source_provider'], previous['source_provider_id'], previous['source_generation']) !=
                    (expected_authority.volume_id, expected_authority.provider, expected_authority.provider_id, expected_authority.generation)):
                    raise SwitchReviewError('switch_retry_identity_mismatch')
                return dict(previous, already_applied=True)
            session = service.get(cursor, identifier, revision=revision)
            preview = session.preview.view()
            if (session.volume_id != expected_authority.volume_id or preview['source_generation'] != expected_authority.generation
                    or preview['source'] != dict(provider=expected_authority.provider, provider_id=expected_authority.provider_id)):
                raise SwitchReviewError('switch_source_mismatch')
            require_current(cursor, (expected_authority,))
            if mapping_digest != preview['mapping_digest']:
                raise SwitchReviewError('stale_mapping_digest')
            if not preview['apply_available'] or preview['blockers'] or not preview['correspondence_complete']:
                raise SwitchReviewError('switch_review_blocked')
            if preview['target_digest'] != session.target.data.digest:
                raise SwitchReviewError('reviewed_target_mismatch')
            service.fault_hook('before_mutation')
            result = _apply_values(cursor, session, uuid4().hex, service.fault_hook)
        service.delete(identifier)  # Commit first. Durable retry works even after restart.
        return dict(result, already_applied=False)

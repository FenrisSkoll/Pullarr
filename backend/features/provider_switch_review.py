"""Bounded reviews and explicit transactional application; no HTTP/UI surface."""

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from secrets import token_urlsafe
from threading import BoundedSemaphore, RLock
from time import monotonic

from backend.base.definitions import SpecialVersion
from backend.base.provider_switch import (POLICY, ProviderReference,
                                          StoredIssueIdentity,
                                          TargetIssueIdentity, correspondence)
from backend.base.switch_review import FrozenReviewData, SwitchReviewError
from backend.implementations.classification import (ClassificationIssue,
                                                    evaluate_special_version)
from backend.implementations.metadata.switch_target import (
    AdmittedSwitchTarget, acquire_target)
from backend.internals.classification_provenance import (InputScope,
                                                         decision_payload)
from backend.internals.provider_authority import capture
from backend.internals.switch_review import load_local


def observed_tasks(volume_id):
    """Best-effort process-local observation, never a cross-process exclusion lock."""
    from backend.features.tasks import TaskHandler
    queue = tuple(TaskHandler.queue)
    if len(queue) > 2000:
        raise SwitchReviewError('task_observation_limit')
    return tuple(sorted((row['id'], row['task'].action, row['task'].volume_id)
                        for row in queue if row['task'].volume_id in (None, volume_id)))


def source_issues(local):
    references = {}
    provider = local['selected']['provider']
    for row in local['issue_refs']:
        references.setdefault(row['issue_id'], []).append((ProviderReference(row['provider'], row['provider_id']), row['provenance']))
    output = []
    for row in local['issues']:
        refs = tuple(references.get(row['id'], ()))
        selected = [ref for ref, _ in refs if ref.provider == provider]
        if len(selected) != 1:
            raise SwitchReviewError('selected_issue_identity_unavailable')
        output.append(StoredIssueIdentity(row['id'], selected[0], refs))
    return tuple(output)


def target_issues(target):
    data = target.data.view()
    assertions = {}
    for row in data['assertions']:
        if row['entity'] == 'issue':
            assertions.setdefault(row['owner_id'], []).append((ProviderReference(row['provider'], row['provider_id']), row['provenance']))
    return tuple(TargetIssueIdentity(ProviderReference(target.reference.provider, row['provider_id']),
        target.reference, tuple(assertions.get(row['provider_id'], ()))) for row in data['issues'])


def claim_effects(local, plan):
    selected = {(row.source.provider, row.source.provider_id): row for row in plan.issues}
    active = [row for row in local['claims'] if row['retired_at'] is None]
    pairs = {(row['target_provider'], row['target_provider_id'], row['source_provider'], row['source_provider_id']): row['id']
             for row in active}
    result, affected, unsafe = [], set(), set()
    valid = {row['id'] for row in local['valid_coverage']}
    coverage_by_claim = {}
    for row in local['coverage']:
        if row['retired_at'] is None:
            coverage_by_claim.setdefault(row['claim_id'], []).append(row)
    for claim in active:
        replacements = {}
        errors = []
        for endpoint in ('target', 'source'):
            key = (claim[endpoint + '_provider'], claim[endpoint + '_provider_id'])
            mapping = selected.get(key)
            if mapping is not None:
                replacements[endpoint] = dict(local_issue_id=mapping.local_id,
                    old=dict(provider=key[0], provider_id=key[1]), new=mapping.target)
                if mapping.target is None or mapping.blockers:
                    errors.append('unresolved_claim_endpoint')
        if not replacements:
            continue
        affected.add(claim['id'])
        if claim['policy'] != 'kapowarr-collected-content/v1' or claim['authority'] != 'operator_confirmed':
            errors.append('unsupported_claim_authority')
        new_pair = []
        for endpoint in ('target', 'source'):
            change = replacements.get(endpoint)
            ref = change['new'] if change else None
            new_pair.extend((ref.provider, ref.provider_id) if ref else
                            (claim[endpoint + '_provider'], claim[endpoint + '_provider_id']))
        owner = pairs.get(tuple(new_pair))
        if owner is not None and owner != claim['id']:
            errors.append('rebound_claim_pair_conflict')
        if new_pair[:2] == new_pair[2:]:
            errors.append('rebound_claim_self_reference')
        coverage = coverage_by_claim.get(claim['id'], ())
        if errors:
            unsafe.add(claim['id'])
        result.append(dict(claim_id=claim['id'], kind=claim['kind'], authority=claim['authority'],
            endpoints=replacements, blockers=errors, future_action='supersede_preserving_evidence' if not errors else 'blocked',
            valid_coverage=[row['id'] for row in coverage if row['id'] in valid],
            stale_coverage=[row['id'] for row in coverage if row['id'] not in valid],
            stale_coverage_action='do_not_reactivate'))
    ownership = {}
    for row in local['ownership']:
        ownership.setdefault(row['issue_id'], []).append(row)
    consequences = []
    for endpoint in local['endpoints']:
        links = ownership.get(endpoint['id'], ())
        impacted = [row for row in links if row['claim_id'] in affected]
        if not impacted:
            continue
        without = any(row['claim_id'] not in affected for row in links)
        after = any(row['claim_id'] not in unsafe for row in links)
        consequences.append(dict(issue_id=endpoint['id'], currently_owned=True,
            owned_without_rebinding=without, owned_after_exact_rebinding=after,
            wanted_without_rebinding=bool(endpoint['monitored'] and endpoint['volume_monitored'] and not without)))
    # A stale association involving this volume but not a translatable current
    # claim stays stale. Its existence is visible and never auto-recovered.
    return dict(claims=result, ownership=consequences,
        retired_claims_preserved=sum(row['retired_at'] is not None for row in local['claims']),
        coverage_reactivation=False, evidence_rewrite=False)


def evaluate_target(local_data, target, evaluated_at):
    local, remote = local_data.view(), target.data.view()
    return evaluate_special_version(title=remote['volume']['title'], description=remote['volume']['description'],
        issues=tuple(ClassificationIssue(row['title'], row['date']) for row in remote['issues']),
        stored_value=SpecialVersion(local['volume']['special_version']), locked=False, evaluated_at=evaluated_at,
        format_evidence=target.physical, publication_evidence=target.publication)


def build_preview(local_data, target, overrides, evaluated_at, tasks=(), candidate=None):
    local, remote = local_data.view(), target.data.view()
    volume, selected = local['volume'], local['selected']
    source = ProviderReference(selected['provider'], selected['provider_id'])
    plan = correspondence(source, target.reference, source_issues(local), target_issues(target), dict(overrides))
    blockers = []
    for row in local['external']:
        if row['provider'] == target.reference.provider and row['provider_id'] != target.reference.provider_id:
            blockers.append('established_target_volume_conflict')
    if any(row['volume_id'] != volume['id'] for row in local['volume_owners']):
        blockers.append('target_volume_reference_owned_elsewhere')
    mapping_by_target = {row.target.provider_id: row.local_id for row in plan.issues if row.target}
    for row in local['issue_owners']:
        if row['provider'] == target.reference.provider and mapping_by_target.get(row['provider_id']) != row['issue_id']:
            blockers.append('target_issue_identity_conflict')
    refs = {(row['issue_id'], row['provider']): row['provider_id'] for row in local['issue_refs']}
    volrefs = {row['provider']: row['provider_id'] for row in local['external']}
    owners = {(row['provider'], row['provider_id']): row['issue_id'] for row in local['issue_owners']}
    for assertion in remote['assertions']:
        expected = mapping_by_target.get(assertion['owner_id'])
        if assertion['entity'] == 'volume':
            existing = volrefs.get(assertion['provider'])
        else:
            existing = refs.get((expected, assertion['provider']))
            if owners.get((assertion['provider'], assertion['provider_id']), expected) != expected:
                blockers.append('asserted_issue_identity_conflict')
        if existing is not None and existing != assertion['provider_id']:
            blockers.append('established_reference_conflict')
    if not plan.ready:
        blockers.append('incomplete_correspondence')
    blockers.extend('active_' + row['category'] for row in local['dependencies'])
    if tasks:
        blockers.append('observed_process_task')
    claims = claim_effects(local, plan)
    if any(row['blockers'] for row in claims['claims']):
        blockers.append('content_claim_rebind_blocked')
    if candidate is None:
        candidate = evaluate_target(local_data, target, evaluated_at)
    classification = dict(current=local['classification'],
        target_unlocked_evaluation=decision_payload(candidate, InputScope.DECISION_TIME),
        future_action='preserve_locked_value_and_receipt' if volume['special_version_locked'] else 'apply_candidate_and_replace_receipt',
        evaluation_is_application=False)
    local_by_id = {row['id']: row for row in local['issues']}
    remote_by_id = {row['provider_id']: row for row in remote['issues']}
    facts_by_id = {row['id']: row for row in local['facts']}
    direct = {}
    for row in local['direct']:
        direct.setdefault(row['issue_id'], []).append(row['file_id'])
    refs_by_id = {}
    for row in local['issue_refs']:
        refs_by_id.setdefault(row['issue_id'], []).append(row)
    manual_ids = dict(overrides)
    issue_rows = [dict(local=local_by_id[row.local_id], canonical=facts_by_id[row.local_id],
        external_ids=refs_by_id.get(row.local_id, ()), correspondence=row,
        manually_confirmed=row.local_id in manual_ids, direct_files=direct.get(row.local_id, ()),
        target=remote_by_id.get(row.target.provider_id) if row.target else None) for row in plan.issues]
    target_fields = {key: remote['volume'][key] for key in
                     ('title', 'year', 'publisher', 'volume_number', 'description', 'site_url')}
    target_fields['alt_title'] = (remote['volume']['aliases'] or [None])[0]
    deltas = {key: dict(before=volume[key], after=value) for key, value in target_fields.items()
              if key in remote['application_fields']['volume'] and volume[key] != value}
    return FrozenReviewData.create(dict(schema='provider-switch-preview/v1', policy=POLICY,
        volume_id=volume['id'], source=source, target=target.reference, target_volume=remote['volume'],
        source_generation=volume['authority_generation'],
        volume_deltas=deltas, application_fields=remote['application_fields'],
        existing_external_ids=local['external'], issues=issue_rows,
        target_only=[dict(remote_by_id[ref.provider_id], monitored=bool(volume['monitor_new_issues'])) for ref in plan.target_only],
        blockers=sorted(set(blockers)), correspondence_complete=plan.ready, apply_available=not blockers,
        mapping_digest=FrozenReviewData.create(plan).digest, local_digest=local_data.digest,
        target_digest=target.data.digest, classification=classification, content=claims,
        bibliography=dict(retained=local['bibliography'], target=remote['bibliography'],
            selected_before=source.provider, selected_after=target.reference.provider,
            old_evidence_becomes_historical=any(row['provider'] == source.provider
                for row in local['bibliography']['volume'] + local['bibliography']['issues'])),
        graph=dict(retained_refs=local['graph'], gcd_selected_mapping_removed=source.provider == 'gcd',
            gcd_selected_mapping_enabled=target.reference.provider == 'gcd', cross_provider_remap=False, sync=False),
        dependencies=local['dependencies'], observed_tasks=tasks, monitor=local['monitor'],
        refresh_exclusion='authority_generation_write_serialization',
        preserved=dict(existing_local_ids=True, files=True, monitoring=True, artwork=True),
        effects=dict(moves=0, renames=0, comicinfo_writes=0, library_writes=0)), 32 * 1024 * 1024)


@dataclass(frozen=True)
class ReviewedSwitchSession:
    id: str
    volume_id: int
    target: AdmittedSwitchTarget
    local: FrozenReviewData
    preview: FrozenReviewData
    overrides: tuple
    revision: int
    created_at: str
    expires_at: float
    evaluated_at: datetime
    tasks: tuple
    candidate: object

    @property
    def byte_size(self):
        return len(self.target.data.payload) + len(self.local.payload) + len(self.preview.payload)


class ProviderSwitchReviews:
    """Explicitly owned process-local service; no singleton or persistent sessions.

    FIFO eviction, fixed non-sliding TTL, bounded in-flight acquisition, count and
    serialized retained bytes. RLock serializes revisions; no HTTP under that lock.
    Restart requires a new review unless a durable successful receipt exists.
    """
    def __init__(self, *, clock=monotonic, acquire=acquire_target, task_observer=observed_tasks,
                 max_sessions=8, max_bytes=96 * 1024 * 1024, session_bytes=48 * 1024 * 1024, ttl=900,
                 fault_hook=None):
        if not (1 <= max_sessions <= 16 and 0 < session_bytes <= max_bytes <= 128 * 1024 * 1024 and 0 < ttl <= 900):
            raise ValueError('Invalid review bounds')
        self.clock, self.acquire, self.task_observer = clock, acquire, task_observer
        self.max_sessions, self.max_bytes, self.session_bytes, self.ttl = max_sessions, max_bytes, session_bytes, ttl
        self._sessions = OrderedDict()
        self._lock = RLock()
        self._inflight = BoundedSemaphore(2)
        self.fault_hook = fault_hook or (lambda stage: None)

    def apply(self, cursor, identifier, revision, mapping_digest, *, confirmed, expected_authority):
        from backend.features.provider_switch_apply import apply_review
        return apply_review(self, cursor, identifier, revision, mapping_digest,
                            confirmed=confirmed, expected_authority=expected_authority)

    def history(self, cursor, volume_id, **options):
        from backend.internals.provider_switch_history import history
        return history(cursor, volume_id, **options)

    def receipt(self, cursor, identifier, **options):
        from backend.internals.provider_switch_history import receipt
        return receipt(cursor, identifier, **options)

    def expire(self):
        with self._lock:
            now = self.clock()
            for key in tuple(self._sessions):
                if self._sessions[key].expires_at <= now:
                    del self._sessions[key]

    def delete(self, identifier):
        with self._lock:
            self._sessions.pop(identifier, None)

    def _store(self, session):
        if session.byte_size > self.session_bytes:
            raise SwitchReviewError('session_size_limit')
        self.expire()
        while self._sessions and (len(self._sessions) >= self.max_sessions or
                sum(row.byte_size for row in self._sessions.values()) + session.byte_size > self.max_bytes):
            self._sessions.popitem(last=False)
        self._sessions[session.id] = session

    async def create(self, cursor, volume_id, provider, provider_id):
        reference = ProviderReference(provider, provider_id)
        authority = capture(cursor, (volume_id,)).get(volume_id)
        source = cursor.execute('''SELECT v.metadata_provider,x.provider_id FROM volumes v
            JOIN volume_external_ids x ON x.volume_id=v.id AND x.provider=v.metadata_provider
            WHERE v.id=?''', (volume_id,)).fetchone()
        if source is None or source[0] == provider:
            raise SwitchReviewError('unavailable_or_same_provider_volume')
        if cursor.connection.in_transaction:
            raise SwitchReviewError('review_fetch_requires_no_pending_transaction')
        if not self._inflight.acquire(blocking=False):
            raise SwitchReviewError('review_acquisition_busy')
        try:
            target = await self.acquire(reference)
            if target.reference != reference:
                raise SwitchReviewError('target_identity_mismatch')
            local = load_local(cursor, volume_id, target)
            selected = local.view()['selected']
            if ((selected['provider'], selected['provider_id']) != tuple(source)
                    or capture(cursor, (volume_id,)).get(volume_id) != authority):
                raise SwitchReviewError('source_changed_during_fetch')
            evaluated = datetime.now()
            tasks = self.task_observer(volume_id)
            candidate = evaluate_target(local, target, evaluated)
            preview = build_preview(local, target, (), evaluated, tasks, candidate)
            session = ReviewedSwitchSession(token_urlsafe(32), volume_id, target, local, preview, (), 1,
                datetime.now(timezone.utc).isoformat(), self.clock() + self.ttl, evaluated, tasks, candidate)
            with self._lock:
                self._store(session)
            return session
        finally:
            self._inflight.release()

    def get(self, cursor, identifier, *, revision=None):
        with self._lock:
            self.expire()
            session = self._sessions.get(identifier)
            if session is None:
                raise SwitchReviewError('review_expired_or_unavailable')
            if revision is not None and revision != session.revision:
                raise SwitchReviewError('stale_review_revision')
            if (load_local(cursor, session.volume_id, session.target).digest != session.local.digest
                    or self.task_observer(session.volume_id) != session.tasks):
                raise SwitchReviewError('stale_local_state')
            if session.expires_at <= self.clock():
                del self._sessions[identifier]
                raise SwitchReviewError('review_expired_or_unavailable')
            return session

    def revise(self, cursor, identifier, revision, mappings):
        """Full exact override set replaces the previous set; optimistic revision."""
        if not isinstance(mappings, dict) or len(mappings) > 10000:
            raise SwitchReviewError('invalid_manual_mapping')
        if any(type(key) is not int or not isinstance(value, str) for key, value in mappings.items()):
            raise SwitchReviewError('invalid_manual_mapping')
        overrides = tuple(sorted(mappings.items()))
        with self._lock:
            session = self.get(cursor, identifier, revision=revision)
            preview = build_preview(session.local, session.target, overrides, session.evaluated_at, session.tasks, session.candidate)
            revised = replace(session, overrides=overrides, preview=preview, revision=session.revision + 1)
            if revised.expires_at <= self.clock():
                del self._sessions[identifier]
                raise SwitchReviewError('review_expired_or_unavailable')
            if revised.byte_size > self.session_bytes or (sum(row.byte_size for row in self._sessions.values()) -
                    session.byte_size + revised.byte_size > self.max_bytes):
                raise SwitchReviewError('session_size_limit')
            self._sessions[identifier] = revised
            return revised

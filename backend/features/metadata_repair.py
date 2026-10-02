"""Fresh same-authority repair reviews from owned maintenance intent.

Acquisition and review are read-only. Application requires a separate explicit
confirmation and transaction; an 8C digest or provider snapshot is insufficient.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from secrets import token_urlsafe
from threading import BoundedSemaphore, RLock
from time import monotonic

from backend.base.definitions import SpecialVersion
from backend.base.maintenance_review import Action, Capability
from backend.base.metadata_repair import (POLICY, Field, RepairError,
                                          validate_selection)
from backend.base.provider_switch import ProviderReference
from backend.base.switch_review import FrozenReviewData
from backend.implementations.classification import (ClassificationIssue,
                                                    evaluate_special_version)
from backend.implementations.maintenance_review import file_state
from backend.implementations.metadata.repair_fields import field_review
from backend.implementations.metadata.switch_target import acquire_target
from backend.internals.classification_provenance import (InputScope,
                                                         decision_payload)
from backend.internals.library_health import read_snapshot
from backend.internals.provider_authority import capture, require_current
from backend.internals.switch_review import load_local


@dataclass(frozen=True)
class RepairReview:
    id: str
    authority: object
    origin: FrozenReviewData
    target: object
    local: FrozenReviewData
    fields: tuple
    selected: tuple
    revision: int
    created_at: str
    expires_at: float
    evaluated_at: datetime
    candidate: object
    preview: FrozenReviewData

    @property
    def digest(self):
        return FrozenReviewData.create(dict(policy=POLICY, id=self.id, revision=self.revision,
            authority=self.authority, origin=self.origin.digest, target=self.target.data.digest,
            local=self.local.digest, selected=self.selected, preview=self.preview.digest)).digest

    def page(self, offset=0, limit=50):
        if type(offset) is not int or not 0 <= offset <= 40000 or type(limit) is not int or not 1 <= limit <= 100:
            raise RepairError('invalid_repair_page')
        return dict(total=len(self.fields), revision=self.revision, digest=self.digest,
                    items=[dict(f.view(), selected=f.selection.key in self.selected)
                           for f in self.fields[offset:offset + limit]])


def review_effects(local_data, target, fields, selected, evaluated_at):
    """Classify the *selected* effective state, never excluded remote fields."""
    local = local_data.view()
    volume = dict(local['volume'])
    issues = {i['id']: dict(i) for i in local['issues']}
    changes = []
    classification_input = False
    for field in fields:
        if field.selection.key not in selected or field.change == 'unchanged':
            continue
        value = field.values.view()
        changes.append(field.view())
        classification_input |= field.classification_input
        owner = volume if field.selection.scope == 'volume' else issues[field.selection.local_id]
        if field.selection.field == Field.FACTS:
            owner.update({k: value['after'][k] for k in ('issue_number', 'calculated_issue_number', 'date')})
        else:
            owner[field.selection.field.value] = value['after']
    candidate = evaluate_special_version(title=volume['title'], description=volume['description'],
        issues=tuple(ClassificationIssue(i['title'], i['date']) for i in issues.values()),
        stored_value=SpecialVersion(volume['special_version']), locked=False, evaluated_at=evaluated_at,
        format_evidence=target.physical, publication_evidence=target.publication)
    action = ('preserve_locked_value_and_receipt' if volume['special_version_locked'] else
              'apply_reviewed_candidate' if classification_input else 'preserve_no_classification_input_change')
    blockers = sorted({'active_' + row['category'] for row in local['dependencies']})
    return candidate, FrozenReviewData.create(dict(policy=POLICY, changes=changes,
        blockers=blockers, apply_available=bool(changes) and not blockers, application_boundary='same_authority_db_transaction',
        classification=dict(current=local['classification'], action=action,
            candidate=decision_payload(candidate, InputScope.DECISION_TIME)),
        unchanged_identity=True, authority_generation_delta=0, new_issues=0, deleted_issues=0,
        bibliography_action=('persist_supplied_observations' if any(
            r['field'] == Field.BIBLIOGRAPHY.value for r in changes) else 'preserve_not_selected'),
        enrichment_action='preserve_not_selected',
        c2_action='preserve_no_rebinding', artwork_action='preserve_local',
        files=dict(moves=0, renames=0, comicinfo_writes=0),
        receipt='no_success_receipt_from_review', undo='no_snapshot_undo'))


class MetadataRepairReviews:
    """Application-owned transient child reviews; trusted internal/task callers.

    No Flask routes, separate queue or durable pending state. Acquisition
    is async so a TaskHandler integration can own long-running provider work.
    """
    TTL = 900
    MAX_SESSIONS = 4
    MAX_BYTES = 64 * 1024 * 1024
    MAX_REVISIONS = 1000

    def __init__(self, maintenance, *, acquire=acquire_target, clock=monotonic, task_observer=None, fault_hook=None):
        self.maintenance, self.acquire, self.clock = maintenance, acquire, clock
        self._lock = RLock()
        self._acquisition = BoundedSemaphore(1)
        self._sessions: dict[str, RepairReview] = {}
        self._stale: set[str] = set()
        if task_observer is None:
            from backend.features.provider_switch_review import observed_tasks
            task_observer = lambda volume_id: tuple(r for r in observed_tasks(volume_id) if r[1] != 'metadata_repair')
        self.task_observer = task_observer
        self.fault_hook = fault_hook or (lambda stage: None)

    def _expire(self):
        for identity in tuple(self._sessions):
            if self._sessions[identity].expires_at <= self.clock():
                del self._sessions[identity]
                self._stale.discard(identity)

    def _admit(self, session):
        size = len(session.target.data.payload) + len(session.local.payload) + len(session.preview.payload)
        size += sum(len(f.values.payload) + 512 for f in session.fields)
        if size > self.MAX_BYTES or len(session.fields) > 40000:
            raise RepairError('repair_review_size_limit')

    async def create(self, cursor, worklist_id, worklist_revision, manifest_digest, finding_ids):
        if not self._acquisition.acquire(blocking=False):
            raise RepairError('repair_acquisition_busy')
        try:
            with self._lock:
                self._expire()
                if len(self._sessions) >= self.MAX_SESSIONS:
                    raise RepairError('repair_review_capacity')
            worklist = self.maintenance.get(worklist_id)
            if (type(worklist_revision) is not int or worklist.revision != worklist_revision
                    or worklist.manifest_digest != manifest_digest):
                raise RepairError('stale_worklist_handoff')
            if (type(finding_ids) is not tuple or not finding_ids or len(finding_ids) > 2000
                    or any(not isinstance(i, str) for i in finding_ids) or len(set(finding_ids)) != len(finding_ids)):
                raise RepairError('invalid_repair_findings')
            items = {i.finding.id: i for i in worklist.items}
            selected = []
            for identity in finding_ids:
                item = items.get(identity)
                if (item is None or not item.selected or item.excluded or item.action != Action.METADATA
                        or item.capability == Capability.STALE or item.finding.volume_id is None):
                    raise RepairError('unsupported_worklist_handoff')
                selected.append(item)
            volumes = {i.finding.volume_id for i in selected}
            if len(volumes) != 1:
                raise RepairError('one_volume_per_repair_review')
            volume_id = volumes.pop()
            snapshot = read_snapshot(self.maintenance.database, worklist.report.scope, 20000)
            if snapshot['digest'] != worklist.report.state_digest:
                raise RepairError('stale_worklist_evidence')
            for item in selected:
                import json
                for path, expected in json.loads(item.freshness_json).get('paths', {}).items():
                    if file_state(path) != expected:
                        raise RepairError('stale_worklist_file')
            token = capture(cursor, (volume_id,)).get(volume_id)
            if token is None:
                raise RepairError('selected_authority_unavailable')
            if cursor.connection.in_transaction:
                raise RepairError('repair_acquisition_requires_transaction_boundary')
            target = await self.acquire(ProviderReference(token.provider, token.provider_id))
            require_current(cursor, (token,))
            current = self.maintenance.get(worklist_id)
            if current is not worklist:
                raise RepairError('stale_worklist_handoff')
            after = read_snapshot(self.maintenance.database, worklist.report.scope, 20000)
            if after['digest'] != snapshot['digest']:
                raise RepairError('source_changed_during_repair_acquisition')
            local = load_local(cursor, volume_id, target)
            fields, _ = field_review(local, target)
            now = datetime.now(timezone.utc)
            evaluated_at = now.replace(tzinfo=None)  # Existing classifier uses naive UTC date arithmetic.
            candidate, preview = review_effects(local, target, fields, (), evaluated_at)
            origin = FrozenReviewData.create(dict(worklist_id=worklist.id, revision=worklist.revision,
                manifest_digest=worklist.manifest_digest, findings=sorted(finding_ids),
                source_completeness=worklist.report.state.value))
            session = RepairReview(token_urlsafe(24), token, origin, target, local, fields, (), 0,
                                   now.isoformat(), self.clock() + self.TTL, evaluated_at, candidate, preview)
            self._admit(session)
            with self._lock:
                self._sessions[session.id] = session
            return session
        finally:
            self._acquisition.release()

    def get(self, cursor, identifier):
        with self._lock:
            self._expire()
            session = self._sessions.get(identifier)
            if session is None:
                raise RepairError('repair_review_expired_or_unavailable')
            if identifier in self._stale:
                raise RepairError('stale_repair_review')
            try:
                require_current(cursor, (session.authority,))
                if load_local(cursor, session.authority.volume_id, session.target).digest != session.local.digest:
                    raise RepairError('stale_repair_review')
            except Exception:
                self._stale.add(identifier)
                raise RepairError('stale_repair_review') from None
            return session

    def revise(self, cursor, identifier, revision, selected):
        with self._lock:
            session = self.get(cursor, identifier)
            if type(revision) is not int or session.revision != revision:
                raise RepairError('stale_repair_revision')
            if revision >= self.MAX_REVISIONS:
                raise RepairError('repair_revision_limit')
            keys = validate_selection(session.fields, selected)
            candidate, preview = review_effects(session.local, session.target, session.fields, keys, session.evaluated_at)
            revised = replace(session, selected=keys, revision=revision + 1, candidate=candidate, preview=preview)
            self._admit(revised)
            self._sessions[identifier] = revised
            return revised

    def delete(self, identifier):
        with self._lock:
            self._sessions.pop(identifier, None)
            self._stale.discard(identifier)

    def apply(self, cursor, identifier, revision, digest, *, confirmed, expected_authority):
        from backend.features.metadata_repair_apply import apply_review
        return apply_review(self, cursor, identifier, revision, digest,
                            confirmed=confirmed, expected_authority=expected_authority)

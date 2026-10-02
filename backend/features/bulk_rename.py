"""Internal reviewed canonical rename. No provider, archive inspection or UI.

Registration is one bounded DB transaction; execution is independent journaled
jobs. A registration rejection creates no jobs. A later sibling failure does not
undo successful jobs. Unapplied reviews are process-local and non-durable.
"""

import json
import os
from dataclasses import replace
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from backend.base.bulk_rename import (RenameItem, RenameReview,
                                      RenameReviewError)
from backend.base.library_health import canonical, fingerprint
from backend.base.maintenance_review import Action, Capability
from backend.features.comicinfo_repair import scoped_digest
from backend.features.organization_execution import OrganizationExecutor, _key
from backend.implementations.maintenance_review import (batch_collisions,
                                                        build_previews,
                                                        file_state)
from backend.implementations.organization_filesystem import execution_gate
from backend.internals.library_health import read_snapshot
from backend.internals.organization_jobs import JobStore
from backend.internals.provider_authority import capture, require_current
from backend.internals.rename_review import naming_evidence
from backend.internals.switch_review import dependencies


def collisions(items, selected):
    # Reservations already compare conservatively with casefold on both hosts.
    # The same conservative equivalence blocks case-only transitions on Linux.
    chosen = [i for i in items if i.finding_id in selected]
    result = batch_collisions([(i.finding_id, i.plan.source_path, i.plan.target_path)
                               for i in chosen if i.plan.target_path])
    folded = {}
    for item in chosen:
        source, target = item.plan.source_path, item.plan.target_path
        if target is None:
            continue
        if source != target and _key(source) == _key(target):
            result.append(dict(code='case_only_requires_staging', items=[item.finding_id]))
        folded.setdefault(_key(target), []).append(item.finding_id)
    for ids in folded.values():
        if len(ids) > 1:
            result.append(dict(code='platform_equivalent_target', items=sorted(ids)))
    return canonical(result)


class BulkRenameReviews:
    MAX_SESSIONS = 8
    MAX_ITEMS = 250
    MAX_BYTES = 32 * 1024 * 1024
    TTL = 900

    def __init__(self, maintenance, *, clock=monotonic, checkpoint=None):
        self.maintenance, self.clock, self.checkpoint = maintenance, clock, checkpoint
        self._lock = RLock()
        self._sessions = {}
        self._stale = set()

    def _expire(self):
        for identifier in tuple(self._sessions):
            if self._sessions[identifier].expires_at <= self.clock():
                self._sessions.pop(identifier)
                self._stale.discard(identifier)

    def _retain(self, session):
        size = sum(len(repr(s).encode('utf-8')) for key, s in self._sessions.items() if key != session.id)
        if size + len(repr(session).encode('utf-8')) > self.MAX_BYTES:
            raise RenameReviewError('rename_review_size_limit')
        self._sessions[session.id] = session
        return session

    def create(self, cursor, worklist_id, revision, manifest_digest, finding_ids):
        with self._lock:
            self._expire()
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise RenameReviewError('rename_review_capacity')
            worklist = self.maintenance.get(worklist_id)
            if type(revision) is not int or worklist.revision != revision or worklist.manifest_digest != manifest_digest:
                raise RenameReviewError('stale_worklist_handoff')
            self._validate_selection(finding_ids)
            owned = {i.finding.id: i for i in worklist.items}
            if any(k not in owned or not owned[k].selected or owned[k].excluded
                   or owned[k].action != Action.RENAME or owned[k].capability == Capability.STALE for k in finding_ids):
                raise RenameReviewError('exact_rename_intent_required')
            snapshot = read_snapshot(self.maintenance.database, worklist.report.scope, 20000)
            plans = {}
            revised, _ = build_previews(self.maintenance.database, worklist.report,
                tuple(owned[k] for k in finding_ids), snapshot, individual_plans=plans)
            authorities = capture(cursor, (i.finding.volume_id for i in revised if i.finding.volume_id))
            evidence = {vid: naming_evidence(cursor, vid) for vid in authorities}
            items = []
            for item in revised:
                plan = plans.get(item.finding.id)
                if plan is None or item.capability == Capability.STALE or not plan.associations:
                    raise RenameReviewError('stale_or_unavailable_rename_plan')
                vid = item.finding.volume_id
                if vid not in authorities:
                    raise RenameReviewError('selected_authority_unavailable')
                if plan.target_path and os.path.dirname(plan.source_path) != os.path.dirname(plan.target_path):
                    raise RenameReviewError('folder_transition_not_authorized')
                blockers = tuple(d.code.value for d in plan.diagnostics if d.severity.value in ('blocking', 'review')
                    or d.code.value in ('cross_filesystem_relocation', 'cross_filesystem_status_unknown'))
                if plan.status.value not in ('ready', 'no_changes') and not blockers:
                    blockers = ('planner_not_ready',)
                items.append(RenameItem(item.finding.id, plan, authorities[vid], scoped_digest(snapshot, plan),
                    fingerprint(file_state(plan.source_path)), fingerprint(file_state(plan.target_path)) if plan.target_path else '',
                    evidence[vid], blockers))
            if read_snapshot(self.maintenance.database, worklist.report.scope, 20000)['digest'] != snapshot['digest']:
                raise RenameReviewError('stale_rename_review')
            require_current(cursor, authorities.values())
            if self.maintenance.get(worklist_id) is not worklist:
                raise RenameReviewError('stale_worklist_handoff')
            selected = tuple(sorted(finding_ids))
            return self._retain(RenameReview(token_urlsafe(24), (worklist.id, revision, manifest_digest),
                worklist.report.scope, tuple(items), selected, 0, self.clock() + self.TTL, collisions(items, selected)))

    def _validate_selection(self, selected):
        if (type(selected) is not tuple or not 1 <= len(selected) <= self.MAX_ITEMS
                or any(not isinstance(k, str) or len(k) != 64 for k in selected) or len(set(selected)) != len(selected)):
            raise RenameReviewError('invalid_rename_selection')

    def get(self, identifier):
        with self._lock:
            self._expire()
            if identifier not in self._sessions:
                raise RenameReviewError('rename_review_expired_or_unavailable')
            return self._sessions[identifier]

    def revise(self, identifier, revision, selected):
        with self._lock:
            session = self.get(identifier)
            self._validate_selection(selected)
            if type(revision) is not int or session.revision != revision or revision >= 1000:
                raise RenameReviewError('stale_rename_revision')
            if not set(selected).issubset(i.finding_id for i in session.items):
                raise RenameReviewError('unknown_rename_item')
            return self._retain(replace(session, revision=revision + 1, selected=tuple(sorted(selected)),
                                        collisions_json=collisions(session.items, selected)))

    def delete(self, identifier):
        with self._lock:
            self._sessions.pop(identifier, None)
            self._stale.discard(identifier)

    def _fresh(self, cursor, session):
        if session.id in self._stale:
            raise RenameReviewError('stale_rename_review')
        items = [i for i in session.items if i.finding_id in session.selected]
        authorities = {i.authority.volume_id: i.authority for i in items}
        current = capture(cursor, authorities)
        evidence = {vid: naming_evidence(cursor, vid) for vid in authorities}
        snapshot = read_snapshot(self.maintenance.database, session.scope, 20000)
        changed = current != authorities
        for item in items:
            changed |= (evidence[item.authority.volume_id] != item.naming_evidence
                or scoped_digest(snapshot, item.plan) != item.state_digest
                or fingerprint(file_state(item.plan.source_path)) != item.source_stamp
                or item.plan.target_path is not None and fingerprint(file_state(item.plan.target_path)) != item.destination_stamp)
        if changed:
            self._stale.add(session.id)
            raise RenameReviewError('stale_rename_review')
        if any(i.blockers for i in items) or json.loads(session.collisions_json):
            raise RenameReviewError('rename_selection_blocked')
        for vid in authorities:
            if dependencies(cursor, vid):
                raise RenameReviewError('rename_active_dependency')
            from backend.features.provider_switch_review import observed_tasks
            if any(action != 'maintenance_bulk_rename' for _, action, _ in observed_tasks(vid)):
                raise RenameReviewError('rename_observed_task')
        return items

    def revalidate(self, cursor, identifier):
        with self._lock:
            session = self.get(identifier)
            self._fresh(cursor, session)
            return session

    @staticmethod
    def correlation(identifier, revision, digest):
        if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 128 or type(revision) is not int
                or not 0 <= revision <= 1000 or not isinstance(digest, str) or len(digest) != 64):
            raise RenameReviewError('invalid_rename_confirmation')
        return 'maintenance-rename:' + identifier + ':' + str(revision) + ':' + digest

    def register(self, cursor, identifier, revision, digest, *, confirmed, origin, selected):
        """All-or-none registration, never filesystem effects. Exact durable retry first."""
        if cursor.connection.in_transaction:
            raise RenameReviewError('rename_requires_transaction_boundary')
        if confirmed is not True:
            raise RenameReviewError('explicit_rename_confirmation_required')
        self._validate_selection(selected)
        correlation = self.correlation(identifier, revision, digest)
        with self._lock:
            # A durable result is readable even if a formerly managed root has
            # disappeared. Do not require an executable filesystem to retry.
            store = JobStore(self.maintenance.database)
            try:
                existing = store.db.execute('SELECT id FROM organization_jobs WHERE batch_id=? ORDER BY created_at,id LIMIT 1',
                                            (correlation,)).fetchone()
                if existing:
                    first = store.intent(existing[0])
                    if (first.get('rename_origin', {}).get('worklist') != list(origin)
                            or first.get('rename_origin', {}).get('selection') != sorted(selected)):
                        raise RenameReviewError('rename_retry_identity_mismatch')
                    return self.status(store.db.cursor(), correlation)
            finally:
                store.close()
            roots = tuple(r[0] for r in cursor.execute('SELECT folder FROM root_folders ORDER BY id'))
            executor = OrganizationExecutor(self.maintenance.database, roots, checkpoint=self.checkpoint)
            try:
                with execution_gate(executor.store.path), executor.store.transaction():
                    db = executor.store.db
                    existing = db.execute('SELECT id FROM organization_jobs WHERE batch_id=? ORDER BY created_at,id', (correlation,)).fetchall()
                    if existing:
                        first = executor.store.intent(existing[0][0])
                        if (first.get('rename_origin', {}).get('worklist') != list(origin)
                                or first.get('rename_origin', {}).get('selection') != sorted(selected)):
                            raise RenameReviewError('rename_retry_identity_mismatch')
                        return self.status(db.cursor(), correlation)
                    session = self.get(identifier)
                    if (session.revision != revision or session.digest != digest or session.origin != origin
                            or session.selected != tuple(sorted(selected))):
                        raise RenameReviewError('stale_rename_confirmation')
                    items = self._fresh(db.cursor(), session)
                    no_changes = [i.finding_id for i in items if i.plan.source_path == i.plan.target_path]
                    if len(no_changes) == len(items):
                        return dict(batch_id=correlation, state='no_changes', counts={'no_changes': len(items)},
                            total=len(items), items=[dict(finding_id=i, job_id=None, state='no_changes') for i in no_changes])
                    for ordinal, item in enumerate(items):
                        if item.plan.source_path == item.plan.target_path:
                            continue
                        intent, plan_id = executor._prepare_job(item.plan, batch_id=correlation,
                            rename_authority=item.authority, rename_stamp=item.source_stamp,
                            rename_evidence=item.naming_evidence,
                            rename_origin=dict(worklist=list(origin), selection=sorted(selected),
                                               finding_id=item.finding_id, review_digest=digest, no_changes=no_changes))
                        executor.hook('before_rename_registration', correlation, ordinal)
                        executor.store.create_in_transaction(intent, plan_id,
                            (_key(item.plan.source_path), _key(item.plan.target_path)), correlation)
                return self.status(cursor, correlation)
            finally:
                executor.close()

    def status(self, cursor, correlation, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise RenameReviewError('invalid_page')
        counts = dict(cursor.execute('SELECT state,COUNT(*) FROM organization_jobs WHERE batch_id=? GROUP BY state', (correlation,)))
        state = ('unavailable' if not counts else 'completed' if set(counts) == {'completed'}
                 else 'partially_completed_batch' if counts.get('completed') else 'pending_or_stopped')
        job_count = sum(counts.values())
        first = cursor.execute("SELECT json_extract(intent,'$.rename_origin.no_changes') FROM organization_jobs WHERE batch_id=? ORDER BY created_at,id LIMIT 1",
                               (correlation,)).fetchone()
        no_changes = json.loads(first[0] or '[]') if first else []
        if no_changes:
            counts['no_changes'] = len(no_changes)
        rows = cursor.execute('''SELECT id,state,error,json_extract(intent,'$.rename_origin.finding_id')
            FROM organization_jobs WHERE batch_id=? ORDER BY created_at,id LIMIT ? OFFSET ?''', (correlation, limit, offset)).fetchall()
        page = [dict(job_id=r[0], state=r[1], error=r[2], finding_id=r[3]) for r in rows]
        if len(page) < limit:
            start = max(0, offset - job_count)
            page.extend(dict(job_id=None, state='no_changes', finding_id=i)
                        for i in no_changes[start:start + limit - len(page)])
        return dict(batch_id=correlation, state=state, counts=counts, total=sum(counts.values()), items=page)

    def execute(self, cursor, correlation):
        """Trusted task/worker entry, not an HTTP handler. Never creates new jobs."""
        rows = cursor.execute('SELECT id FROM organization_jobs WHERE batch_id=? ORDER BY created_at,id LIMIT ?',
                              (correlation, self.MAX_ITEMS + 1)).fetchall()
        if len(rows) > self.MAX_ITEMS or not correlation.startswith('maintenance-rename:'):
            raise RenameReviewError('invalid_rename_batch')
        roots = tuple(r[0] for r in cursor.execute('SELECT folder FROM root_folders ORDER BY id'))
        executor = OrganizationExecutor(self.maintenance.database, roots, checkpoint=self.checkpoint)
        try:
            for row in rows:
                executor.apply_job(row[0])
            return self.status(cursor, correlation)
        finally:
            executor.close()

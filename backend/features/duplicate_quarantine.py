"""Trusted reviewed batch adapter, not transport or an alternative job engine."""

import json
import os
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from backend.base.duplicate_review import (DuplicateAction, DuplicateKind,
                                           DuplicateReviewError)
from backend.base.organization_job import EXECUTOR_POLICY, OrganizationError
from backend.features.organization_execution import OrganizationExecutor
from backend.features.organization_quarantine import MOVE, RECORD, VERSION
from backend.implementations.duplicate_evidence import HashBudget, stamp
from backend.implementations.organization_filesystem import execution_gate
from backend.implementations.quarantine_location import (location_context,
                                                         observe_location)
from backend.internals.duplicate_review import read_duplicate_state
from backend.internals.organization_jobs import (MAX_PAYLOAD, JobStore,
                                                 canonical, digest)
from backend.internals.organization_reservations import (load_reservations,
                                                         path_key)
from backend.internals.quarantine_state import project, require_owned, snapshot
from backend.internals.switch_review import dependencies


class DuplicateQuarantine:
    MAX_MUTATIONS = 25
    MAX_INTENTS = 16 * 1024 * 1024
    HASH_BYTES = 256 * 1024 * 1024

    def __init__(self, reviews, *, checkpoint=None, progress=lambda count: None, cancel=lambda: False):
        self.reviews, self.checkpoint = reviews, checkpoint
        self.database = reviews.maintenance.database
        self.progress, self.cancel = progress, cancel

    def _current(self, session):
        worklist = self.reviews.maintenance.get(session.origin[0])
        if (worklist.id, worklist.revision, worklist.manifest_digest) != session.origin:
            raise DuplicateReviewError('stale_quarantine_handoff')
        if read_duplicate_state(self.database, worklist.report.scope)[2] != session.state_digest:
            self.reviews._retain(replace(session, stale=True))
            raise DuplicateReviewError('stale_quarantine_domain')

    def _hash(self, session):
        members = {i for g in session.groups if g.quarantine for i in g.file_ids}
        files = {r['id']: r for r in json.loads(session.files_json)}
        budget, result = HashBudget(maximum=self.HASH_BYTES, progress=self.progress, cancel=self.cancel), {}
        for fid in sorted(members):
            result[fid] = budget.inspect(files[fid]['filepath'])
            if canonical(result[fid]) != canonical(files[fid]['hash']):
                raise DuplicateReviewError('stale_quarantine_hash')
        return files, result

    @staticmethod
    def _provenance(session, review_digest):
        return dict(worklist=session.origin, selection=sorted(g.id for g in session.groups), review_digest=review_digest,
            groups=[dict(id=g.id, action=g.action.value, quarantine=g.quarantine,
                         retained=sorted(set(g.file_ids) - set(g.quarantine))) for g in session.groups])

    @staticmethod
    def _deps(cursor, state, paths):
        index = load_reservations(cursor)
        if any(index.conflicts(p) for p in paths):
            raise DuplicateReviewError('quarantine_path_reserved')
        for volume in state['volumes']:
            if dependencies(cursor, volume['id']):
                raise DuplicateReviewError('quarantine_active_dependency')
            from backend.features.provider_switch_review import observed_tasks
            if any(action != 'maintenance_duplicate_quarantine' for _, action, _ in observed_tasks(volume['id'])):
                raise DuplicateReviewError('quarantine_observed_task')

    def prepare(self, cursor, identifier, revision):
        """Fresh acquisition adds executable evidence and advances review revision.

        No directory creation, reservation or job. Choice edits invalidate this
        extension; the subsequent confirmation binds its new digest explicitly.
        """
        if cursor.connection.in_transaction:
            raise DuplicateReviewError('quarantine_transaction_boundary_required')
        with self.reviews._lock:
            session = self.reviews.get(identifier)
            if session.stale or type(revision) is not int or revision != session.revision or revision >= 1000:
                raise DuplicateReviewError('stale_quarantine_review')
            selected = [g for g in session.groups if g.quarantine]
            removed = [i for g in selected for i in g.quarantine]
            if not removed or len(removed) > self.MAX_MUTATIONS or len(set(removed)) != len(removed):
                raise DuplicateReviewError('quarantine_mutation_bound')
            for group in selected:
                if (group.kind != DuplicateKind.EXACT or not set(group.quarantine) < set(group.file_ids)
                        or set(group.blockers) - {'quarantine_recovery_not_implemented'}):
                    raise DuplicateReviewError('quarantine_selection_blocked')
                if any(other.id != group.id and set(group.quarantine) & set(other.file_ids)
                       for other in session.groups):
                    raise DuplicateReviewError('overlapping_resolution_groups')
            self._current(session)
            _, hashes = self._hash(session)
            # One authoritative batch acquisition; no SQL writes in preparation.
            cursor.execute('SAVEPOINT quarantine_review_read')
            try:
                state = snapshot(cursor, tuple(hashes))
                require_owned(state, set(removed))
                locations = location_context(cursor)
                intents = []
                for group in selected:
                    kept = sorted(set(group.file_ids) - set(group.quarantine))
                    for fid in group.quarantine:
                        location_id = uuid5(NAMESPACE_URL, 'kapowarr-quarantine:' + identifier + ':' + str(fid)).hex
                        location = observe_location(cursor, fid, location_id, context=locations)
                        before = project(cursor, state, (fid, *kept))
                        intents.append(dict(version=EXECUTOR_POLICY, quarantine_effect=VERSION, effects=[MOVE, RECORD],
                            inverse=False, file_id=fid, volume_id=location['volume_id'], root_id=location['root_id'],
                            issue_ids=[r['id'] for r in before['issues']], root=location['root'],
                            location_id=location_id, location=location, source=location['source'], target=location['target'],
                            original=location['source'], quarantine_target=location['target'], hash=hashes[fid],
                            retained_ids=kept, retained_hashes={str(i): hashes[i] for i in kept},
                            batch_removed=removed, quarantine_before=before, group_id=group.id))
                self._deps(cursor, state, tuple(p for i in intents for p in (i['source'], i['target'])))
            finally:
                cursor.execute('RELEASE quarantine_review_read')
            self._current(session)
            if any(list(stamp(os.lstat(r['filepath']))) != list(hashes[r['id']]['stamp']) for r in state['files']):
                raise DuplicateReviewError('stale_quarantine_source')
            payload = canonical(intents)
            # The eventual digest has exactly 64 ASCII characters; account for
            # the complete mixed-action correlation, not guessed headroom.
            provenance = self._provenance(session, '0' * 64)
            sizes = [len(canonical(dict(i, duplicate_origin=provenance))) for i in intents]
            if sum(sizes) > self.MAX_INTENTS or any(size > MAX_PAYLOAD for size in sizes):
                groups = tuple(replace(g, blockers=tuple(sorted(set(g.blockers) | {'journal_intent_too_large'})))
                               if g.quarantine else g for g in session.groups)
                return self.reviews._retain(replace(session, revision=revision + 1, groups=groups, execution_json='[]'))
            groups = tuple(replace(g, blockers=tuple(b for b in g.blockers if b != 'quarantine_recovery_not_implemented'))
                           for g in session.groups)
            return self.reviews._retain(replace(session, revision=revision + 1, groups=groups, execution_json=payload))

    @staticmethod
    def correlation(identifier, revision, review_digest):
        if (type(identifier) is not str or not 1 <= len(identifier) <= 128 or type(revision) is not int
                or not 0 <= revision <= 1000 or type(review_digest) is not str or len(review_digest) != 64):
            raise DuplicateReviewError('invalid_quarantine_confirmation')
        return 'maintenance-quarantine:' + identifier + ':' + str(revision) + ':' + review_digest

    def _retry(self, store, batch, origin, selected):
        row = store.db.execute('SELECT id FROM organization_jobs WHERE batch_id=? ORDER BY id LIMIT 1', (batch,)).fetchone()
        if row:
            recorded = store.intent(row[0]).get('duplicate_origin', {})
            if recorded.get('worklist') != list(origin) or recorded.get('selection') != sorted(selected):
                raise DuplicateReviewError('quarantine_retry_identity_mismatch')
            return self.status(store.db.cursor(), batch)
        return None

    def register(self, cursor, identifier, revision, review_digest, *, origin, selected, confirmed):
        if (confirmed is not True or cursor.connection.in_transaction or type(selected) is not tuple
                or not 1 <= len(selected) <= 100 or any(type(i) is not str or len(i) != 64 for i in selected)
                or len(set(selected)) != len(selected) or type(origin) is not tuple or len(origin) != 3):
            raise DuplicateReviewError('explicit_quarantine_confirmation_required')
        batch = self.correlation(identifier, revision, review_digest)
        with self.reviews._lock:
            store = JobStore(self.database)
            try:
                retry = self._retry(store, batch, origin, selected)
                if retry:
                    return retry
            finally:
                store.close()
            session = self.reviews.get(identifier)
            if (session.stale or session.revision != revision or session.digest != review_digest or session.origin != origin
                    or sorted(selected) != sorted(g.id for g in session.groups)):
                raise DuplicateReviewError('stale_quarantine_confirmation')
            self._current(session)
            if not any(g.quarantine for g in session.groups):
                return dict(batch_id=batch, state='no_changes', jobs=[], groups=[
                    dict(id=g.id, action=g.action.value, quarantine=[], retained=list(g.file_ids)) for g in session.groups])
            if session.execution_json == '[]':
                raise DuplicateReviewError('quarantine_execution_review_required')
            _, hashes = self._hash(session)  # No writer lock across streamed hashes.
            roots = tuple(r[0] for r in cursor.execute('SELECT folder FROM root_folders ORDER BY id'))
            executor = OrganizationExecutor(self.database, roots, checkpoint=self.checkpoint)
            try:
                with execution_gate(executor.store.path), executor.store.transaction():
                    retry = self._retry(executor.store, batch, origin, selected)
                    if retry:
                        return retry
                    self._current(session)
                    intents = json.loads(session.execution_json)
                    if not 1 <= len(intents) <= self.MAX_MUTATIONS:
                        raise DuplicateReviewError('quarantine_mutation_bound')
                    current = snapshot(executor.store.db.cursor(), tuple(hashes))
                    locations = location_context(executor.store.db.cursor())
                    require_owned(current, {i['file_id'] for i in intents})
                    self._deps(executor.store.db.cursor(), current,
                               tuple(p for i in intents for p in (i['source'], i['target'])))
                    provenance = self._provenance(session, review_digest)
                    prepared = []
                    for intent in intents:
                        before = project(executor.store.db.cursor(), current, (intent['file_id'], *intent['retained_ids']))
                        if canonical(before) != canonical(intent['quarantine_before']):
                            raise DuplicateReviewError('stale_quarantine_domain')
                        location = observe_location(executor.store.db.cursor(), intent['file_id'], intent['location_id'], context=locations)
                        if canonical(location) != canonical(intent['location']):
                            raise DuplicateReviewError('stale_quarantine_location')
                        prepared.append(dict(intent, duplicate_origin=provenance))
                    for row in current['files']:
                        if list(stamp(os.lstat(row['filepath']))) != list(hashes[row['id']]['stamp']):
                            raise DuplicateReviewError('stale_quarantine_source')
                    if sum(len(canonical(i)) for i in prepared) > self.MAX_INTENTS:
                        raise DuplicateReviewError('quarantine_journal_batch_bound')
                    for ordinal, intent in enumerate(prepared):
                        executor._validate_intent(intent)
                        executor.hook('before_quarantine_registration', batch, ordinal)
                        executor.store.create_in_transaction(intent, digest(batch + ':' + str(intent['file_id'])),
                            (path_key(intent['source']), path_key(intent['target'])), batch)
                return self.status(cursor, batch)
            finally:
                executor.close()

    def status(self, cursor, batch):
        rows = cursor.execute('''SELECT j.id,j.state,j.error,json_extract(j.intent,'$.file_id'),json_extract(j.intent,'$.group_id'),
            json_extract(j.intent,'$.duplicate_origin'),q.job_id FROM organization_jobs j
            LEFT JOIN quarantined_files q ON q.file_id=json_extract(j.intent,'$.file_id')
            WHERE j.batch_id=? ORDER BY j.id LIMIT ?''',
            (batch, self.MAX_MUTATIONS + 1)).fetchall()
        if len(rows) > self.MAX_MUTATIONS:
            raise DuplicateReviewError('invalid_quarantine_batch')
        if not rows:
            return dict(batch_id=batch, state='unavailable', groups=[], jobs=[])
        origin = json.loads(rows[0][5])
        jobs = [dict(id=r[0], state=r[1], error=r[2], file_id=r[3], group_id=r[4],
                     inactive=r[6] is not None, current_quarantine=r[6] == r[0],
                     restore='requires_fresh_review' if r[6] == r[0] and r[1] == 'completed' else 'unavailable') for r in rows]
        states = {r[1] for r in rows}
        state = 'completed' if states == {'completed'} else 'partially_completed_batch' if 'completed' in states else 'pending_or_stopped'
        return dict(batch_id=batch, state=state, groups=origin['groups'], jobs=jobs)

    def execute(self, cursor, batch):
        if cursor.connection.in_transaction or not batch.startswith('maintenance-quarantine:'):
            raise DuplicateReviewError('invalid_quarantine_batch')
        result = self.status(cursor, batch)
        if result['state'] in ('completed', 'unavailable'):
            return result
        roots = tuple(r[0] for r in cursor.execute('SELECT folder FROM root_folders ORDER BY id'))
        executor = OrganizationExecutor(self.database, roots, checkpoint=self.checkpoint)
        try:
            for job in result['jobs']:
                executor.apply_job(job['id'])
            return self.status(cursor, batch)
        finally:
            executor.close()

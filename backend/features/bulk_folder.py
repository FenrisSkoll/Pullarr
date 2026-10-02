"""Internal same-root folder reviews, atomic registration, independent jobs."""

import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from backend.base.bulk_folder import (FolderItem, FolderReview,
                                      FolderReviewError)
from backend.base.definitions import SpecialVersion
from backend.base.folder_policy import (FolderMode, FolderPolicy,
                                        FolderPublication)
from backend.base.import_candidate import ProviderReference, ResourceKind
from backend.base.library_health import canonical, fingerprint
from backend.base.maintenance_review import Action, Capability
from backend.base.organization_job import EXECUTOR_POLICY
from backend.features.organization_directory import (MOVE, RECORD,
                                                     VERSION, after_state,
                                                     overlaps, projection)
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.folder_inventory import inspect_folder
from backend.implementations.folder_policy import (FolderContext,
                                                   decide_folder,
                                                   preview_folder)
from backend.implementations.maintenance_review import file_state
from backend.implementations.organization_filesystem import execution_gate
from backend.internals.folder_ownership import load_ownership
from backend.internals.library_health import read_snapshot
from backend.internals.organization_jobs import MAX_PAYLOAD, JobStore
from backend.internals.organization_reservations import (ReservationIndex,
                                                         path_key)
from backend.internals.switch_review import dependencies


def collision_groups(items, selected):
    rows = [(path, item.finding_id) for item in items if item.finding_id in selected
            for path in (item.ownership.source, item.target)]
    index = ReservationIndex(rows)
    groups = set()
    for path, identifier in rows:
        others = index.conflicts(path, exclude=identifier)
        if others:
            groups.add(tuple(sorted((identifier, *others))))
    return canonical([dict(code='overlapping_folder_transitions', items=g) for g in sorted(groups)])


def target_observation(target):
    return canonical(dict(target=file_state(target), parent=file_state(str(Path(target).parent))))


def no_op(item):
    return dict(finding_id=item.finding_id, volume_id=item.ownership.volume_id, job_id=None,
                source=item.ownership.source, target=item.target, state='no_changes', error=None)


def directory_intent(item, origin, selected, review_digest, no_changes):
    return dict(version=EXECUTOR_POLICY, directory_effect=VERSION, effects=[MOVE, RECORD],
        source=item.ownership.source, target=item.target, root=item.ownership.root,
        volume_id=item.ownership.volume_id, custom_after=item.custom_after, inverse=False,
        tree_database_before=item.ownership.state_json,
        tree_database_after=after_state(item.ownership.state_json, item.ownership.source, item.target, item.custom_after),
        tree_projection=projection(item.inventory), inventory_digest=item.inventory.digest,
        folder_origin=dict(worklist=list(origin), selection=sorted(selected), finding_id=item.finding_id,
                           review_digest=review_digest, no_changes=no_changes))


class BulkFolderReviews:
    MAX_ITEMS = 50
    MAX_ENTRIES = 20000
    MAX_SESSIONS = 4
    MAX_BYTES = 32 * 1024 * 1024
    TTL = 900

    def __init__(self, maintenance, *, clock=monotonic, checkpoint=None):
        self.maintenance, self.clock, self.checkpoint = maintenance, clock, checkpoint
        self._lock = RLock()
        self._sessions = {}
        self._stale = set()

    def get(self, identifier):
        with self._lock:
            for key in tuple(self._sessions):
                if self._sessions[key].expires_at <= self.clock():
                    del self._sessions[key]
                    self._stale.discard(key)
            if identifier not in self._sessions:
                raise FolderReviewError('folder_review_expired_or_unavailable')
            return self._sessions[identifier]

    def _retain(self, session):
        no_changes = [no_op(i) for i in session.items if i.finding_id in session.selected and i.no_changes]
        items = []
        for item in session.items:
            blockers = tuple(b for b in item.blockers if b != 'journal_intent_too_large')
            size = 0
            if item.inventory.complete and not item.no_changes:
                # The eventual digest is exactly 64 ASCII characters; using a
                # placeholder avoids a self-referential size/digest calculation.
                size = len(canonical(directory_intent(item, session.origin, session.selected, '0' * 64, no_changes)))
                if size > MAX_PAYLOAD:
                    blockers += ('journal_intent_too_large',)
            items.append(replace(item, journal_bytes=size, blockers=blockers))
        session = replace(session, items=tuple(items))
        retained = sum(len(canonical(asdict(s))) for key, s in self._sessions.items() if key != session.id)
        if retained + len(canonical(asdict(session))) > self.MAX_BYTES:
            raise FolderReviewError('folder_review_size_limit')
        self._sessions[session.id] = session
        return session

    def _selection(self, selected):
        if (type(selected) is not tuple or not 1 <= len(selected) <= self.MAX_ITEMS
                or any(type(k) is not str or len(k) != 64 for k in selected) or len(set(selected)) != len(selected)):
            raise FolderReviewError('invalid_folder_selection')

    def create(self, cursor, worklist_id, revision, manifest_digest, selected, *, canonical_custom=()):
        with self._lock:
            try:
                self.get('purge-expired')
            except FolderReviewError:
                pass
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise FolderReviewError('folder_review_capacity')
            self._selection(selected)
            if type(canonical_custom) is not tuple or not set(canonical_custom).issubset(selected):
                raise FolderReviewError('invalid_custom_folder_confirmation')
            worklist = self.maintenance.get(worklist_id)
            if type(revision) is not int or (worklist.revision, worklist.manifest_digest) != (revision, manifest_digest):
                raise FolderReviewError('stale_worklist_handoff')
            owned = {i.finding.id: i for i in worklist.items}
            if any(k not in owned or not owned[k].selected or owned[k].excluded
                   or owned[k].action != Action.FOLDER or owned[k].capability == Capability.STALE for k in selected):
                raise FolderReviewError('exact_folder_intent_required')
            if read_snapshot(self.maintenance.database, worklist.report.scope, 20000)['digest'] != worklist.report.state_digest:
                raise FolderReviewError('stale_worklist_handoff')
            ids = tuple(owned[k].finding.volume_id for k in selected)
            if None in ids or len(set(ids)) != len(ids):
                raise FolderReviewError('one_folder_intent_per_volume_required')
            batch = load_ownership(cursor, ids)
            volumes = {v.volume_id: v for v in batch.volumes}
            context = FolderContext.build(batch.roots, batch.owners, batch.naming,
                                          windows=os.name == 'nt', case_sensitive=False)
            items = []
            for key in selected:
                ownership = volumes[owned[key].finding.volume_id]
                data = json.loads(ownership.state_json)['volume']
                publication = FolderPublication(data['title'], data['year'], data['volume_number'], data['publisher'],
                    ProviderReference(ownership.authority.provider, ResourceKind.VOLUME, ownership.authority.provider_id),
                    ownership.volume_id, ownership.root_id, ownership.source, ownership.custom,
                    data['comicvine_id'], SpecialVersion(data['special_version']))
                preserve = ownership.custom and key not in canonical_custom
                decision = decide_folder(publication, context, FolderPolicy(
                    mode=FolderMode.PRESERVE_EXISTING if preserve else FolderMode.RECALCULATE))
                target = decision.target_folder or ownership.source
                inventory = inspect_folder(ownership.root, ownership.source, ownership.volume_id, ownership.registrations)
                blockers = [d.code.value for d in decision.diagnostics if d.blocking]
                if target == ownership.source and ownership.custom and not preserve:
                    blockers.append('ownership_only_transition_not_supported')
                if not inventory.complete:
                    blockers.append('source_inventory_' + (inventory.reason or inventory.state.value))
                if any(vid != ownership.volume_id and overlaps(folder, ownership.source) for vid, folder in batch.owners):
                    blockers.append('source_folder_ownership_conflict')
                if any(rid != ownership.root_id and overlaps(root, ownership.source) for rid, root in batch.roots):
                    blockers.append('overlapping_managed_roots')
                observation = json.loads(target_observation(target))
                if target != ownership.source:
                    if path_key(target) == path_key(ownership.source):
                        blockers.append('case_only_folder_transition')
                    if overlaps(target, ownership.source):
                        blockers.append('source_target_overlap')
                    if observation['target']['state'] != 'absent':
                        blockers.append('target_occupied_or_unavailable')
                    if observation['parent']['state'] != 'directory':
                        blockers.append('target_parent_unavailable')
                    elif inventory.source_stamp is not None:
                        device = (observation['parent'].get('stamp') or [None])[0]
                        if device is None:
                            blockers.append('target_device_unknown')
                        elif device != inventory.source_stamp.device:
                            blockers.append('cross_device_not_supported')
                    if Path(ownership.root) not in Path(target).parents or decision.root_id != ownership.root_id:
                        blockers.append('root_transition_not_supported')
                    if any(overlaps(path, target) for _, path, _ in batch.all_files):
                        blockers.append('target_registered_path_conflict')
                if dependencies(cursor, ownership.volume_id):
                    blockers.append('active_volume_dependency')
                if batch.reservations.conflicts(ownership.source) or batch.reservations.conflicts(target):
                    blockers.append('subtree_reserved')
                items.append(FolderItem(key, ownership, inventory, target, ownership.custom if preserve else False,
                    canonical(preview_folder(decision)), canonical(observation), tuple(sorted(set(blockers)))))
                # Bound accumulation during acquisition, not only after every
                # selected tree has been retained in memory.
                if sum(len(i.inventory.entries) for i in items) > self.MAX_ENTRIES:
                    raise FolderReviewError('folder_batch_inventory_limit')
                if sum(len(canonical(asdict(i))) for i in items) > self.MAX_BYTES:
                    raise FolderReviewError('folder_review_size_limit')
            if sum(len(i.inventory.entries) for i in items) > self.MAX_ENTRIES:
                raise FolderReviewError('folder_batch_inventory_limit')
            if batch.volumes != load_ownership(cursor, ids).volumes or self.maintenance.get(worklist_id) is not worklist:
                raise FolderReviewError('stale_folder_review')
            chosen = tuple(sorted(selected))
            return self._retain(FolderReview(token_urlsafe(24), (worklist.id, revision, manifest_digest),
                tuple(items), chosen, 0, self.clock() + self.TTL, collision_groups(items, chosen)))

    def revise(self, identifier, revision, selected):
        with self._lock:
            session = self.get(identifier)
            self._selection(selected)
            if type(revision) is not int or session.revision != revision or revision >= 1000:
                raise FolderReviewError('stale_folder_revision')
            if not set(selected).issubset(i.finding_id for i in session.items):
                raise FolderReviewError('unknown_folder_item')
            return self._retain(replace(session, revision=revision + 1, selected=tuple(sorted(selected)),
                                        collisions_json=collision_groups(session.items, selected)))

    def delete(self, identifier):
        with self._lock:
            self._sessions.pop(identifier, None)
            self._stale.discard(identifier)

    def _fresh(self, cursor, session):
        if session.id in self._stale:
            raise FolderReviewError('stale_folder_review')
        items = [i for i in session.items if i.finding_id in session.selected]
        batch = load_ownership(cursor, tuple(i.ownership.volume_id for i in items))
        volumes = {v.volume_id: v for v in batch.volumes}
        for item in items:
            current = volumes[item.ownership.volume_id]
            observed = inspect_folder(current.root, current.source, current.volume_id, current.registrations)
            if current != item.ownership or observed.digest != item.inventory.digest or target_observation(item.target) != item.target_json:
                self._stale.add(session.id)
                raise FolderReviewError('stale_folder_review')
            if (dependencies(cursor, current.volume_id) or batch.reservations.conflicts(current.source)
                    or batch.reservations.conflicts(item.target)):
                raise FolderReviewError('folder_active_dependency')
            if any(vid != current.volume_id and (overlaps(folder, current.source) or overlaps(folder, item.target))
                   for vid, folder in batch.owners):
                raise FolderReviewError('folder_ownership_conflict')
            if any(rid != current.root_id and (overlaps(root, current.source) or overlaps(root, item.target))
                   for rid, root in batch.roots):
                raise FolderReviewError('folder_root_ownership_conflict')
            if item.target != current.source and any(overlaps(path, item.target) for _, path, _ in batch.all_files):
                raise FolderReviewError('folder_target_path_ownership_conflict')
            from backend.features.provider_switch_review import observed_tasks
            if any(action != 'maintenance_bulk_folder' for _, action, _ in observed_tasks(current.volume_id)):
                raise FolderReviewError('folder_observed_task')
        if any(i.blockers for i in items) or json.loads(collision_groups(items, session.selected)):
            raise FolderReviewError('folder_selection_blocked')
        return items

    def revalidate(self, cursor, identifier):
        with self._lock:
            session = self.get(identifier)
            self._fresh(cursor, session)
            return session

    @staticmethod
    def correlation(identifier, revision, digest):
        if (type(identifier) is not str or not 1 <= len(identifier) <= 128 or type(revision) is not int
                or not 0 <= revision <= 1000 or type(digest) is not str or len(digest) != 64):
            raise FolderReviewError('invalid_folder_confirmation')
        return 'maintenance-folder:' + identifier + ':' + str(revision) + ':' + digest

    def register(self, cursor, identifier, revision, digest, *, confirmed, origin, selected):
        if confirmed is not True or cursor.connection.in_transaction:
            raise FolderReviewError('explicit_folder_confirmation_at_transaction_boundary_required')
        self._selection(selected)
        correlation = self.correlation(identifier, revision, digest)
        with self._lock:
            store = JobStore(self.maintenance.database)
            try:
                found = self._retry(store, correlation, origin, selected)
                if found:
                    return found
            finally:
                store.close()
            roots = tuple(r[0] for r in cursor.execute('SELECT folder FROM root_folders ORDER BY id'))
            executor = OrganizationExecutor(self.maintenance.database, roots, checkpoint=self.checkpoint)
            try:
                with execution_gate(executor.store.path), executor.store.transaction():
                    found = self._retry(executor.store, correlation, origin, selected)
                    if found:
                        return found
                    session = self.get(identifier)
                    if (session.revision != revision or session.digest != digest or session.origin != origin
                            or session.selected != tuple(sorted(selected))):
                        raise FolderReviewError('stale_folder_confirmation')
                    items = self._fresh(executor.store.db.cursor(), session)
                    no_changes = [no_op(i) for i in items if i.no_changes]
                    if len(no_changes) == len(items):
                        return dict(batch_id=correlation, state='no_changes', total=len(items), items=no_changes,
                                    counts={'no_changes': len(items)}, job_count=0)
                    for ordinal, item in enumerate(items):
                        if item.no_changes:
                            continue
                        if item.target == item.ownership.source:
                            raise FolderReviewError('ownership_only_transition_not_supported')
                        intent = directory_intent(item, origin, selected, digest, no_changes)
                        if len(canonical(intent)) > MAX_PAYLOAD:
                            raise FolderReviewError('folder_journal_size_limit')
                        executor._validate_intent(intent)
                        executor.hook('before_folder_registration', correlation, ordinal)
                        executor.store.create_in_transaction(intent, fingerprint((correlation, item.finding_id)),
                            (path_key(item.ownership.source), path_key(item.target)), correlation)
                return self.status(cursor, correlation)
            finally:
                executor.close()

    def _retry(self, store, correlation, origin, selected):
        row = store.db.execute('SELECT id FROM organization_jobs WHERE batch_id=? ORDER BY created_at,id LIMIT 1', (correlation,)).fetchone()
        if row:
            recorded = store.intent(row[0]).get('folder_origin', {})
            if recorded.get('worklist') != list(origin) or recorded.get('selection') != sorted(selected):
                raise FolderReviewError('folder_retry_identity_mismatch')
            return self.status(store.db.cursor(), correlation)
        return None

    def status(self, cursor, correlation, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise FolderReviewError('invalid_page')
        counts = dict(cursor.execute('SELECT state,COUNT(*) FROM organization_jobs WHERE batch_id=? GROUP BY state', (correlation,)))
        state = ('unavailable' if not counts else 'completed' if set(counts) == {'completed'} else
                 'partially_completed_batch' if counts.get('completed') else 'pending_or_stopped')
        rows = cursor.execute('''SELECT id,state,error,json_extract(intent,'$.volume_id'),
            json_extract(intent,'$.source'),json_extract(intent,'$.target'),
            json_extract(intent,'$.folder_origin.finding_id') FROM organization_jobs
            WHERE batch_id=? ORDER BY created_at,id LIMIT 51''', (correlation,)).fetchall()
        if len(rows) > self.MAX_ITEMS:
            raise FolderReviewError('invalid_folder_batch')
        items = [dict(job_id=r[0], state=r[1], error=r[2], volume_id=r[3], source=r[4], target=r[5], finding_id=r[6]) for r in rows]
        if rows:
            recorded = cursor.execute("SELECT json_extract(intent,'$.folder_origin.no_changes') FROM organization_jobs WHERE id=?",
                                      (rows[0][0],)).fetchone()[0]
            no_changes = json.loads(recorded or '[]')
            if len(no_changes) + len(rows) > self.MAX_ITEMS:
                raise FolderReviewError('invalid_folder_batch')
            items.extend(n if isinstance(n, dict) else dict(finding_id=n, job_id=None, state='no_changes') for n in no_changes)
            if no_changes:
                counts['no_changes'] = len(no_changes)
        return dict(batch_id=correlation, state=state, counts=counts, total=len(items), job_count=len(rows),
                    items=items[offset:offset + limit])

    def execute(self, cursor, correlation):
        rows = cursor.execute('SELECT id FROM organization_jobs WHERE batch_id=? ORDER BY created_at,id LIMIT 51', (correlation,)).fetchall()
        if len(rows) > self.MAX_ITEMS or not correlation.startswith('maintenance-folder:'):
            raise FolderReviewError('invalid_folder_batch')
        roots = tuple(r[0] for r in cursor.execute('SELECT folder FROM root_folders ORDER BY id'))
        executor = OrganizationExecutor(self.maintenance.database, roots, checkpoint=self.checkpoint)
        try:
            for row in rows:
                executor.apply_job(row[0])
            return self.status(cursor, correlation)
        finally:
            executor.close()

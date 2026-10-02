"""Trusted transport adapters over reviewed domain services, not an executor."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Optional
from uuid import uuid4

from backend.base.bulk_folder import FolderReviewError
from backend.base.bulk_rename import RenameReviewError
from backend.base.custom_exceptions import MetadataSourceRateLimitReached
from backend.base.definitions import Task
from backend.base.duplicate_review import DuplicateReviewError
from backend.base.maintenance_history import HistoryError
from backend.base.maintenance_review import ReviewError
from backend.base.metadata_repair import RepairError
from backend.base.organization_job import OrganizationError
from backend.base.switch_review import SwitchReviewError
from backend.features.bulk_folder_tasks import BulkFolderTask
from backend.features.bulk_rename_tasks import BulkRenameTask
from backend.features.maintenance_history_tasks import MaintenanceHistoryTask
from backend.features.maintenance_recovery import MaintenanceRecovery
from backend.features.maintenance_specialized import SpecializedActions
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.metadata.errors import MetadataProviderError


def action_error(error):
    """Only domain exception classes enter controlled classification."""
    if isinstance(error, OrganizationError):
        return 'blocked'
    if isinstance(error, MetadataSourceRateLimitReached):
        return 'provider_rate_limited'
    if isinstance(error, MetadataProviderError):
        return ('provider_configuration' if error.reason in ('credentials', 'forbidden') else
                'provider_rate_limited' if error.reason in ('rate_limited', 'deferred') else 'provider_unavailable')
    reason = str(error)
    if 'rate_limit' in reason or 'budget' in reason:
        return 'provider_rate_limited'
    if 'credential' in reason or 'authentication' in reason or 'configuration' in reason:
        return 'provider_configuration'
    if 'unavailable' in reason and 'expired' not in reason:
        return 'provider_unavailable'
    if 'incomplete' in reason:
        return 'incomplete_snapshot'
    if 'expired_or_unavailable' in reason:
        return 'review_expired'
    if 'revision' in reason:
        return 'revision_conflict'
    if 'stale' in reason or 'mismatch' in reason:
        return 'stale'
    if 'capacity' in reason:
        return 'capacity'
    if 'limit' in reason or 'too_large' in reason:
        return 'bounded'
    return 'blocked'


class ConfiguredRecovery(MaintenanceRecovery):
    """Resolve roots at worker execution, never from HTTP or a startup cache."""
    def _executor(self):
        with closing(sqlite3.connect(Path(self.database).absolute().as_uri() + '?mode=ro', uri=True)) as db:
            roots = tuple(r[0] for r in db.execute('SELECT folder FROM root_folders ORDER BY id LIMIT 1001'))
        if len(roots) > 1000:
            raise HistoryError('history_root_limit')
        return OrganizationExecutor(self.database, roots)


@dataclass
class Delivery:
    expires: float
    operation: str
    state: str = 'queued'
    task_id: Optional[int] = None
    result_json: Optional[str] = None
    reason: Optional[str] = None


class ActionTask(Task):
    volume_id = None
    issue_id = None
    display_title = 'Reviewed maintenance action'

    def __init__(self, service, identifier, operation, payload):
        self.service, self.identifier, self.operation, self.payload = service, identifier, operation, payload
        # Existing domain dependency loaders recognize their own trusted task.
        self.action = 'maintenance_bulk_rename' if operation.startswith('rename_') else 'maintenance_history_action'
        if operation.startswith('folder_'):
            self.action = 'maintenance_bulk_folder'
        if operation.startswith(('metadata_', 'comicinfo_')):
            self.action = 'metadata_repair'
        if operation.startswith('duplicate_'):
            self.action = 'maintenance_duplicate_quarantine'
        self.stop = False
        self.message = 'Queued reviewed maintenance action'

    def run(self):
        self.service.run(self.identifier, self.operation, self.payload, self.stop)


class MaintenanceActions:
    MAX_HANDLES = 16
    MAX_RESULT_BYTES = 2 * 1024 * 1024
    TTL = 900
    OPERATIONS = ('rename_create', 'rename_revise', 'rename_apply',
                  'folder_create', 'folder_revise', 'folder_apply',
                  'metadata_create', 'metadata_revise', 'metadata_apply',
                  'comicinfo_create', 'comicinfo_revise', 'comicinfo_apply',
                  'duplicate_create', 'duplicate_revise', 'duplicate_prepare', 'duplicate_apply',
                  'recovery_preview', 'inverse_preview', 'recovery_apply', 'inverse_apply')

    def __init__(self, runtime):
        self.runtime = runtime
        self.recovery = ConfiguredRecovery(runtime.database, ())
        self.specialized = SpecializedActions(runtime)
        self._lock = RLock()
        self._handles: dict[str, Delivery] = {}

    def _expire(self):
        for key in tuple(self._handles):
            if self._handles[key].expires <= self.runtime.clock():
                del self._handles[key]

    def submit(self, operation, payload):
        if operation not in self.OPERATIONS:
            raise ReviewError('invalid_operation')
        with self._lock:
            self._expire()
            if len(self._handles) >= self.MAX_HANDLES:
                raise ReviewError('transport_capacity')
            identifier = uuid4().hex
            delivery = Delivery(self.runtime.clock() + self.TTL, operation)
            self._handles[identifier] = delivery
        task = ActionTask(self, identifier, operation, payload)
        try:
            if self.runtime.enqueue is None:
                from backend.features.tasks import TaskHandler
                task_id = TaskHandler().add(task)
            else:
                task_id = self.runtime.enqueue(task)
            with self._lock:
                delivery.task_id = task_id
        except Exception:
            with self._lock:
                self._handles.pop(identifier, None)
            raise ReviewError('transport_enqueue_failed') from None
        return self.status(identifier)

    def status(self, identifier):
        with self._lock:
            self._expire()
            result = self._handles.get(identifier)
            if result is None:
                raise ReviewError('worklist_expired_or_unavailable')
            return dict(id=identifier, operation=result.operation, state=result.state,
                task_id=result.task_id, reason=result.reason,
                result=json.loads(result.result_json) if result.result_json else None,
                operational_only=True, expires_in=max(0, result.expires - self.runtime.clock()))

    def run(self, identifier, operation, payload, stopped):
        with self._lock:
            self._expire()
            delivery = self._handles.get(identifier)
            if delivery is None or delivery.state != 'queued':
                return
            if stopped:
                delivery.state, delivery.reason = 'cancelled', 'cancelled'
                return
            delivery.state = 'running'
        try:
            result = self._execute(operation, payload)
            encoded = json.dumps(result, ensure_ascii=True, allow_nan=False)
            if len(encoded.encode()) > self.MAX_RESULT_BYTES:
                raise HistoryError('transport_result_too_large')
            with self._lock:
                delivery.result_json, delivery.state = encoded, 'complete'
        except (FolderReviewError, RenameReviewError, ReviewError, HistoryError, OrganizationError,
                RepairError, SwitchReviewError, DuplicateReviewError, MetadataProviderError, MetadataSourceRateLimitReached) as error:
            with self._lock:
                delivery.reason, delivery.state = action_error(error), 'failed'
        except Exception:
            from backend.base.logging import LOGGER
            LOGGER.error('Maintenance action transport failed unexpectedly; inspect durable domain history')
            with self._lock:
                delivery.reason, delivery.state = 'internal_error', 'failed'

    def _execute(self, operation, payload):
        if operation.startswith(('metadata_', 'comicinfo_', 'duplicate_')):
            return self.specialized.execute(operation, payload)
        if operation == 'folder_create':
            with closing(sqlite3.connect(Path(self.runtime.database).absolute().as_uri() + '?mode=ro', uri=True)) as db:
                review = self.runtime.folder.create(db.cursor(), payload['worklist_id'], payload['revision'],
                    payload['digest'], tuple(payload['selected']), canonical_custom=tuple(payload['canonical_custom']))
            return dict(kind='folder_review', id=review.id)
        if operation == 'folder_revise':
            review = self.runtime.folder.revise(payload['id'], payload['revision'], tuple(payload['selected']))
            return dict(kind='folder_review', id=review.id)
        if operation == 'folder_apply':
            task = BulkFolderTask(self.runtime.folder, payload['id'], payload['revision'], payload['digest'],
                origin=tuple(payload['origin']), selected=tuple(payload['selected']), confirmed=True)
            task.run()
            result = task.result
            return dict(kind='folder_batch', id=result['batch_id'], state=result['state'], counts=result.get('counts', {}))
        if operation == 'rename_create':
            with closing(sqlite3.connect(Path(self.runtime.database).absolute().as_uri() + '?mode=ro', uri=True)) as db:
                review = self.runtime.rename.create(db.cursor(), payload['worklist_id'], payload['revision'],
                    payload['digest'], tuple(payload['selected']))
            return dict(kind='rename_review', id=review.id)
        if operation == 'rename_revise':
            review = self.runtime.rename.revise(payload['id'], payload['revision'], tuple(payload['selected']))
            return dict(kind='rename_review', id=review.id)
        if operation == 'rename_apply':
            # Exact durable lookup occurs inside register before transient get.
            task = BulkRenameTask(self.runtime.rename, payload['id'], payload['revision'], payload['digest'],
                origin=tuple(payload['origin']), selected=tuple(payload['selected']), confirmed=True)
            task.run()
            result = task.result
            return dict(kind='rename_batch', id=result['batch_id'], state=result['state'],
                        counts=result.get('counts', {}))
        if operation in ('recovery_preview', 'inverse_preview'):
            method = self.recovery.preview_recovery if operation == 'recovery_preview' else self.recovery.preview_inverse
            preview = method(payload['domain'], payload['id'])
            # Never return raw intent or artifact evidence; paths already hidden
            # by the 8H owner for quarantine. Keep only this presentation contract.
            allowed = ('entry', 'eligible', 'capability', 'digest', 'reasons',
                       'manual_inspection_required', 'current_path', 'restore_path',
                       'internal_storage_hidden', 'observations', 'steps')
            allowed += ('filesystem_mutation_required', 'database_reconciliation_required')
            return dict(kind=operation, preview={k: preview[k] for k in allowed if k in preview})
        task = MaintenanceHistoryTask(self.recovery, payload['id'], payload['digest'],
            operation='recovery' if operation == 'recovery_apply' else 'inverse', confirmed=True)
        task.run()
        return dict(kind='organization', entry=task.result)

    def folder_page(self, identifier, offset=0, limit=50):
        if type(offset) is not int or not 0 <= offset <= 50 or type(limit) is not int or not 1 <= limit <= 100:
            raise FolderReviewError('invalid_page')
        review = self.runtime.folder.get(identifier)
        collisions = json.loads(review.collisions_json)
        selected = [i for i in review.items if i.finding_id in review.selected]
        return dict(id=review.id, revision=review.revision, digest=review.digest, origin=review.origin,
            batch_id=self.runtime.folder.correlation(review.id, review.revision, review.digest),
            selected=review.selected, total=len(review.items), offset=offset, limit=limit,
            expires_in=max(0, review.expires_at - self.runtime.clock()),
            apply_available=bool(selected) and not collisions and not any(i.blockers for i in selected),
            mutation_count=sum(not i.no_changes for i in selected), collisions=collisions,
            canonical_custom=[i.finding_id for i in review.items if i.ownership.custom and not i.custom_after],
            items=[dict(finding_id=i.finding_id, volume_id=i.ownership.volume_id,
                selected=i.finding_id in review.selected, source=i.ownership.source, target=i.target,
                custom_before=i.ownership.custom, custom_after=i.custom_after,
                inventory_complete=i.inventory.complete, inventory_count=len(i.inventory.entries),
                registered_count=len(i.ownership.registrations),
                direct_count=sum(bool(r.direct) for r in i.ownership.registrations),
                general_count=sum(bool(r.general) for r in i.ownership.registrations),
                ancillary_count=sum(e.classification == 'unregistered_regular_file' for e in i.inventory.entries),
                directory_count=sum(e.kind == 'directory' for e in i.inventory.entries),
                journal_bytes=i.journal_bytes, blockers=i.blockers,
                state='blocked' if i.blockers else 'no_changes' if i.no_changes else 'reviewable')
                for i in review.items[offset:offset + limit]])

    def rename_page(self, identifier, offset=0, limit=50):
        if type(offset) is not int or not 0 <= offset <= 250 or type(limit) is not int or not 1 <= limit <= 100:
            raise RenameReviewError('invalid_page')
        review = self.runtime.rename.get(identifier)
        collisions = json.loads(review.collisions_json)
        selected = [i for i in review.items if i.finding_id in review.selected]
        return dict(id=review.id, revision=review.revision, digest=review.digest, origin=review.origin,
            batch_id=self.runtime.rename.correlation(review.id, review.revision, review.digest),
            selected=review.selected, total=len(review.items), offset=offset, limit=limit,
            expires_in=max(0, review.expires_at - self.runtime.clock()),
            apply_available=bool(selected) and not collisions and not any(i.blockers for i in selected),
            mutation_count=sum(i.plan.source_path != i.plan.target_path for i in selected),
            collisions=collisions, items=[dict(finding_id=i.finding_id, selected=i.finding_id in review.selected,
                source=i.plan.source_path, target=i.plan.target_path, blockers=i.blockers,
                state='no_changes' if i.plan.source_path == i.plan.target_path else 'reviewable')
                for i in review.items[offset:offset + limit]])

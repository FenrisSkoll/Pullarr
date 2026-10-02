"""Application-owned maintenance services and bounded transport task handles.

TaskHandler remains the only queue. Handles below are transient delivery of
read-only worklist results, never mutation receipts or execution authority.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Optional
from uuid import uuid4

from backend.base.definitions import Task
from backend.base.maintenance_review import ReviewError
from backend.features.bulk_folder import BulkFolderReviews
from backend.features.bulk_rename import BulkRenameReviews
from backend.features.comicinfo_repair import ComicInfoRepairReviews
from backend.features.duplicate_quarantine import DuplicateQuarantine
from backend.features.duplicate_review import DuplicateReviews
from backend.features.maintenance_history import MaintenanceHistory
from backend.features.maintenance_review import MaintenanceReviews
from backend.features.metadata_repair import MetadataRepairReviews


def review_failure(error: ReviewError):
    """Allowlisted categories, not exception text or arbitrary evidence."""
    reason = str(error)
    if reason in ('scan_expired_or_unavailable', 'worklist_expired_or_unavailable'):
        return 'review_expired', 410
    if reason == 'stale_worklist_revision':
        return 'revision_conflict', 409
    if reason in ('scan_capacity', 'worklist_capacity', 'transport_capacity'):
        return 'capacity', 429
    if reason in ('revision_limit', 'worklist_size_limit', 'report_limit'):
        return 'bounded', 409
    if reason == 'scan_not_available':
        return 'scan_not_available', 409
    if reason in ('scan_enqueue_failed', 'transport_enqueue_failed'):
        return 'task_unavailable', 503
    return 'invalid_request', 400


@dataclass
class _Delivery:
    expires: float
    state: str = 'queued'
    task_id: Optional[int] = None
    worklist_id: Optional[str] = None
    reason: Optional[str] = None


class _WorklistTask(Task):
    action = 'maintenance_worklist_review'
    display_title = 'Review maintenance intent (no library changes)'
    volume_id = None
    issue_id = None

    def __init__(self, owner, identifier, operation, args):
        self.owner, self.identifier = owner, identifier
        self.operation, self.args = operation, args
        self.stop = False
        self.message = 'Queued maintenance review'

    def run(self):
        self.owner._run(self.identifier, self.operation, self.args, self.stop)


class MaintenanceRuntime:
    MAX_DELIVERIES = 16
    DELIVERY_TTL = 900

    def __init__(self, database: str, *, enqueue=None, clock=monotonic):
        # All constructors are inert: startup may precede setup_db on fresh DBs.
        self.database, self.enqueue, self.clock = database, enqueue, clock
        self.reviews = MaintenanceReviews(database, enqueue=enqueue, clock=clock)
        self.metadata = MetadataRepairReviews(self.reviews, clock=clock)
        self.comicinfo = ComicInfoRepairReviews(self.reviews, clock=clock)
        self.rename = BulkRenameReviews(self.reviews, clock=clock)
        self.folder = BulkFolderReviews(self.reviews, clock=clock)
        self.duplicates = DuplicateReviews(self.reviews, clock=clock)
        self.quarantine = DuplicateQuarantine(self.duplicates)
        self.history = MaintenanceHistory(database)
        from backend.features.archive_maintenance import ArchiveMaintenance
        self.archives = ArchiveMaintenance(database, enqueue=enqueue, clock=clock)
        self._lock = RLock()
        self._deliveries: dict[str, _Delivery] = {}
        from backend.features.maintenance_actions import MaintenanceActions
        self.actions = MaintenanceActions(self)

    def _expire(self):
        for identifier in tuple(self._deliveries):
            if self._deliveries[identifier].expires <= self.clock():
                del self._deliveries[identifier]

    def volume_page(self, query='', after=0, limit=50):
        """Small local selector, not the legacy unbounded library/stat list.

        Stable ID keyset; literal title substring only. No provider search,
        matching decisions, paths or per-volume ownership/stat queries.
        """
        if (type(query) is not str or len(query) > 200 or '\x00' in query
                or type(after) is not int or not 0 <= after <= 2**63 - 1
                or type(limit) is not int or not 1 <= limit <= 100):
            raise ReviewError('invalid_volume_page')
        pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        with closing(sqlite3.connect(Path(self.database).absolute().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT id,title,year,volume_number,metadata_provider
                FROM volumes WHERE id>? AND title LIKE ? ESCAPE '\\'
                ORDER BY id LIMIT ?""", (after, pattern, limit + 1)).fetchall()
        return dict(items=[dict(row) for row in rows[:limit]],
                    next_after=rows[limit - 1]['id'] if len(rows) > limit else None)

    def submit_worklist(self, operation, *args):
        if operation not in ('revise', 'select_filtered', 'revalidate'):
            raise ReviewError('invalid_operation')
        with self._lock:
            self._expire()
            if len(self._deliveries) >= self.MAX_DELIVERIES:
                raise ReviewError('transport_capacity')
            identifier = uuid4().hex
            delivery = _Delivery(self.clock() + self.DELIVERY_TTL)
            self._deliveries[identifier] = delivery
        task = _WorklistTask(self, identifier, operation, args)
        try:
            if self.enqueue is None:
                from backend.features.tasks import TaskHandler
                task_id = TaskHandler().add(task)
            else:
                task_id = self.enqueue(task)
            with self._lock:
                delivery.task_id = task_id
        except Exception:
            with self._lock:
                self._deliveries.pop(identifier, None)
            raise ReviewError('transport_enqueue_failed') from None
        return self.delivery(identifier)

    def _run(self, identifier, operation, args, stopped):
        with self._lock:
            self._expire()
            delivery = self._deliveries.get(identifier)
            if delivery is None or delivery.state != 'queued':
                return
            if stopped:
                delivery.state, delivery.reason = 'cancelled', 'cancelled'
                return
            delivery.state = 'running'
        try:
            # No arbitrary method names or callables admitted by transport.
            methods = dict(revise=self.reviews.revise,
                           select_filtered=self.reviews.select_filtered,
                           revalidate=self.reviews.revalidate)
            result = methods[operation](*args)
            with self._lock:
                delivery.worklist_id, delivery.state = result.id, 'complete'
        except ReviewError as error:
            with self._lock:
                delivery.state, delivery.reason = 'failed', review_failure(error)[0]
        except Exception:
            # Do not leak archive/provider/path exception representations through
            # TaskHandler's generic exception logger or browser task status.
            from backend.base.logging import LOGGER
            LOGGER.error('Maintenance worklist task failed unexpectedly')
            with self._lock:
                delivery.state, delivery.reason = 'failed', 'internal_error'

    def delivery(self, identifier):
        with self._lock:
            self._expire()
            delivery = self._deliveries.get(identifier)
            if delivery is None:
                raise ReviewError('worklist_expired_or_unavailable')
            return dict(id=identifier, state=delivery.state, task_id=delivery.task_id,
                        worklist_id=delivery.worklist_id, reason=delivery.reason,
                        expires_in=max(0, delivery.expires - self.clock()),
                        operational_only=True, library_mutation=False)

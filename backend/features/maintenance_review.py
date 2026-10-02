"""Application-owned bounded scans and revisioned review, never execution.

One instance per application/database. No module-global worker, disk persistence,
HTTP handler, provider client, OrganizationJob registration or apply method.
"""

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, RLock
from uuid import uuid4

from backend.base.definitions import Task
from backend.base.library_health import (HealthLevel, HealthLimits,
                                         HealthScope, canonical)
from backend.base.maintenance_review import (Action, Capability, Edit,
                                             FindingFilter, ReviewError,
                                             ReviewItem, Worklist)
from backend.features.library_health import scan_health
from backend.implementations.maintenance_review import (build_previews,
                                                        file_state)
from backend.internals.library_health import read_snapshot


@dataclass
class _ScanRecord:
    scope: HealthScope
    level: HealthLevel
    expires: float
    cancel: Event = field(default_factory=Event)
    state: str = 'queued'
    progress: dict = field(default_factory=dict)
    report: object = None
    task_id: object = None


class MaintenanceScanTask(Task):
    action = 'maintenance_health'
    display_title = 'Read-only library health scan'
    volume_id = None
    issue_id = None

    def __init__(self, service, identifier):
        self.service, self.identifier = service, identifier
        self.stop = False
        self.message = 'Queued health discovery'

    def run(self):
        self.service._run_scan(self.identifier, lambda: self.stop)
        return None


class MaintenanceReviews:
    MAX_SCANS = 4
    MAX_WORKLISTS = 4
    MAX_FINDINGS = 2000
    MAX_REVISIONS = 1000
    MAX_BYTES = 16 * 1024 * 1024
    TTL = 3600

    def __init__(self, database: str, *, clock=time.monotonic, enqueue=None):
        self.database = str(Path(database).absolute())
        self.clock = clock
        self.enqueue = enqueue
        self._lock = RLock()
        self._scans: dict[str, _ScanRecord] = {}
        self._worklists: dict[str, Worklist] = {}

    def _expire(self):
        now = self.clock()
        for key in tuple(self._scans):
            if self._scans[key].expires <= now:
                self._scans.pop(key).cancel.set()
        for key in tuple(self._worklists):
            if self._worklists[key].expires_at <= now:
                del self._worklists[key]

    def request_scan(self, scope=HealthScope(), level=HealthLevel.INVENTORY):
        if not isinstance(scope, HealthScope) or not isinstance(level, HealthLevel):
            raise ReviewError('invalid_scan')
        with self._lock:
            self._expire()
            if len(self._scans) >= self.MAX_SCANS:
                raise ReviewError('scan_capacity')
            identifier = uuid4().hex
            record = _ScanRecord(scope, level, self.clock() + self.TTL)
            self._scans[identifier] = record
        task = MaintenanceScanTask(self, identifier)
        try:
            if self.enqueue is None:
                from backend.features.tasks import TaskHandler
                task_id = TaskHandler().add(task)
            else:
                task_id = self.enqueue(task)
            with self._lock:
                record.task_id = task_id
        except Exception:
            with self._lock:
                self._scans.pop(identifier, None)
            raise ReviewError('scan_enqueue_failed') from None
        return identifier

    def _run_scan(self, identifier, stopped):
        with self._lock:
            self._expire()
            record = self._scans.get(identifier)
            if record is None or record.state != 'queued':
                return
            record.state = 'running'
        def progress(counts):
            with self._lock:
                record.progress = dict(counts)
        try:
            report = scan_health(self.database, record.scope, record.level,
                HealthLimits(findings=self.MAX_FINDINGS),
                cancel=lambda: record.cancel.is_set() or stopped() or self.clock() >= record.expires,
                progress=progress)
            if len(report.findings) > self.MAX_FINDINGS or len(canonical(report.page(limit=100))) > self.MAX_BYTES:
                raise ReviewError('report_limit')
            with self._lock:
                if self._scans.get(identifier) is record:
                    record.report, record.state = report, report.state.value
        except Exception:
            with self._lock:
                record.state = 'failed'
                record.progress = dict(reason='scan_failed_or_unavailable')

    def scan_status(self, identifier):
        with self._lock:
            self._expire()
            record = self._scans.get(identifier)
            if record is None:
                raise ReviewError('scan_expired_or_unavailable')
            return dict(id=identifier, state=record.state, task_id=record.task_id,
                        expires_in=max(0, record.expires - self.clock()), progress=dict(record.progress),
                        summary=record.report.summary() if record.report is not None else None)

    def findings(self, identifier, **filters):
        with self._lock:
            self._expire()
            record = self._scans.get(identifier)
            if record is None or record.report is None:
                raise ReviewError('scan_not_available')
            return record.report.page(**filters)

    def cancel_scan(self, identifier):
        with self._lock:
            record = self._scans.get(identifier)
            if record is None:
                raise ReviewError('scan_expired_or_unavailable')
            record.cancel.set()
            # Do not set Task.stop: a running TaskHandler task needs normal
            # queue cleanup. A queued task will produce a cancelled report.

    def create(self, scan_id):
        with self._lock:
            self._expire()
            record = self._scans.get(scan_id)
            if record is None or record.report is None:
                raise ReviewError('scan_not_available')
            if len(self._worklists) >= self.MAX_WORKLISTS:
                raise ReviewError('worklist_capacity')
            report = record.report
            if len({f.id for f in report.findings}) != len(report.findings):
                raise ReviewError('duplicate_finding_identity')
            now = self.clock()
            worklist = Worklist(uuid4().hex, report, now, now + self.TTL, 0,
                                tuple(ReviewItem(f) for f in report.findings))
            self._check_size(worklist)
            self._worklists[worklist.id] = worklist
            return worklist

    def get(self, identifier):
        with self._lock:
            self._expire()
            if identifier not in self._worklists:
                raise ReviewError('worklist_expired_or_unavailable')
            return self._worklists[identifier]

    def delete(self, identifier, revision):
        with self._lock:
            worklist = self.get(identifier)
            if type(revision) is not int or worklist.revision != revision:
                raise ReviewError('stale_worklist_revision')
            del self._worklists[identifier]

    def _expected(self, identifier, revision):
        worklist = self.get(identifier)
        if type(revision) is not int or worklist.revision != revision:
            raise ReviewError('stale_worklist_revision')
        if revision >= self.MAX_REVISIONS:
            raise ReviewError('revision_limit')
        return worklist

    def _check_size(self, worklist):
        size = len(worklist.collisions_json) + len(worklist.selection_filter_json)
        for item in worklist.items:
            size += len(canonical(item.view()))
            if size > self.MAX_BYTES:
                raise ReviewError('worklist_size_limit')

    def select_filtered(self, identifier, revision, report_id, snapshot_digest,
                        filters: object, selected=True):
        with self._lock:
            worklist = self._expected(identifier, revision)
            if (report_id != worklist.report.id or snapshot_digest != worklist.report.state_digest
                    or not isinstance(filters, FindingFilter) or type(selected) is not bool):
                raise ReviewError('invalid_filter_snapshot')
            edits = tuple(Edit(i.finding.id, selected, False, i.action) for i in worklist.items if filters.matches(i))
        return self._revise(worklist, edits, canonical(dict(report_id=report_id,
            snapshot=snapshot_digest, filter=filters.__dict__, meaning='all known matching findings, not visible page')))

    def revise(self, identifier, revision, edits: tuple[Edit, ...]):
        with self._lock:
            worklist = self._expected(identifier, revision)
        return self._revise(worklist, edits, worklist.selection_filter_json)

    def revalidate(self, identifier, revision):
        return self.revise(identifier, revision, ())

    def _revise(self, worklist, edits, selection_filter):
        if type(edits) is not tuple or len(edits) > self.MAX_FINDINGS or any(not isinstance(e, Edit) for e in edits):
            raise ReviewError('invalid_edits')
        changes = {e.finding_id: e for e in edits}
        if len(changes) != len(edits) or set(changes) - {i.finding.id for i in worklist.items}:
            raise ReviewError('unknown_or_duplicate_finding')
        items = []
        for item in worklist.items:
            edit = changes.get(item.finding.id)
            if edit:
                item = replace(item, selected=edit.selected, excluded=edit.excluded, action=edit.action)
            items.append(item)
        try:
            snapshot = read_snapshot(self.database, worklist.report.scope, 20000)
            revised, collisions = build_previews(self.database, worklist.report, tuple(items), snapshot)
            # The identity loader and filesystem observations are separate reads.
            # Reject intervening changes; never publish a silently retargeted plan.
            after = read_snapshot(self.database, worklist.report.scope, 20000)
            cache = {}
            final = []
            import json
            for item in revised:
                fresh = after['digest'] == snapshot['digest']
                for path, expected in json.loads(item.freshness_json).get('paths', {}).items():
                    if path not in cache:
                        cache[path] = file_state(path)
                    fresh = fresh and cache[path] == expected
                final.append(replace(item, capability=Capability.STALE, blockers=('changed_during_review',))
                             if item.selected and not fresh else item)
        except ReviewError:
            raise
        except Exception:
            raise ReviewError('review_inputs_unavailable') from None
        result = replace(worklist, revision=worklist.revision + 1, items=tuple(final),
                         collisions_json=canonical(collisions), selection_filter_json=selection_filter)
        self._check_size(result)
        with self._lock:
            current = self._expected(worklist.id, worklist.revision)
            if current is not worklist:
                raise ReviewError('stale_worklist_revision')
            self._worklists[result.id] = result
        return result

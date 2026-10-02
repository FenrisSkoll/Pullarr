"""Trusted TaskHandler adapter for exact confirmed duplicate review batches."""

import sqlite3
from pathlib import Path

from backend.base.definitions import Task
from backend.base.duplicate_review import DuplicateReviewError


class DuplicateQuarantineTask(Task):
    action = 'maintenance_duplicate_quarantine'
    display_title = 'Reviewed duplicate quarantine'
    volume_id = None
    issue_id = None

    def __init__(self, service, identifier, revision, digest, *, origin, selected, confirmed):
        if confirmed is not True:
            raise DuplicateReviewError('explicit_quarantine_confirmation_required')
        service.correlation(identifier, revision, digest)
        self.service, self.confirmation = service, (identifier, revision, digest)
        self.origin, self.selected = origin, selected
        self.stop, self.result = False, None
        self.message = 'Queued explicitly confirmed duplicate quarantine'

    def run(self):
        from backend.features.duplicate_quarantine import DuplicateQuarantine
        def progress(count):
            self.message = f'Validating reviewed duplicate bytes: {count} bytes'
        worker = DuplicateQuarantine(self.service.reviews, checkpoint=self.service.checkpoint,
                                     progress=progress, cancel=lambda: self.stop)
        db = sqlite3.connect(Path(self.service.database).absolute().as_uri() + '?mode=rw', uri=True, timeout=10)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            registered = worker.register(db.cursor(), *self.confirmation, confirmed=True,
                origin=self.origin, selected=self.selected)
            self.message = 'Executing independently journaled duplicate quarantine jobs'
            self.result = (registered if registered['state'] in ('completed', 'no_changes')
                           else worker.execute(db.cursor(), registered['batch_id']))
        finally:
            db.close()
        return None

    def enqueue(self):
        from backend.features.tasks import TaskHandler
        return TaskHandler().add(self)

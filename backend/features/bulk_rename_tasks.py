"""Trusted TaskHandler adapter; not registered as a raw HTTP task constructor."""

import sqlite3
from pathlib import Path

from backend.base.definitions import Task


class BulkRenameTask(Task):
    action = 'maintenance_bulk_rename'
    display_title = 'Reviewed bulk rename'
    volume_id = None
    issue_id = None

    def __init__(self, service, identifier, revision, digest, *, origin, selected, confirmed):
        from backend.base.bulk_rename import RenameReviewError
        if confirmed is not True:
            raise RenameReviewError('explicit_rename_confirmation_required')
        service.correlation(identifier, revision, digest)
        service._validate_selection(selected)
        self.service = service
        self.confirmation = identifier, revision, digest
        self.origin, self.selected = origin, selected
        self.stop = False
        self.message = 'Queued explicitly confirmed filename-only rename'
        self.result = None

    def run(self):
        db = sqlite3.connect(Path(self.service.maintenance.database).absolute().as_uri() + '?mode=rw', uri=True, timeout=10)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            registered = self.service.register(db.cursor(), *self.confirmation, confirmed=True,
                                               origin=self.origin, selected=self.selected)
            self.result = (registered if registered['state'] in ('no_changes', 'completed')
                           else self.service.execute(db.cursor(), registered['batch_id']))
        finally:
            db.close()
        return None

    def enqueue(self):
        from backend.features.tasks import TaskHandler
        return TaskHandler().add(self)

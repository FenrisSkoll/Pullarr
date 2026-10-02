"""Trusted TaskHandler adapter; no HTTP task constructor or arbitrary paths."""

import sqlite3
from pathlib import Path

from backend.base.definitions import Task


class BulkFolderTask(Task):
    action = 'maintenance_bulk_folder'
    display_title = 'Reviewed folder organization'
    volume_id = None
    issue_id = None

    def __init__(self, service, identifier, revision, digest, *, origin, selected, confirmed):
        from backend.base.bulk_folder import FolderReviewError
        if confirmed is not True:
            raise FolderReviewError('explicit_folder_confirmation_required')
        service.correlation(identifier, revision, digest)
        service._selection(selected)
        self.service = service
        self.confirmation = identifier, revision, digest
        self.origin, self.selected = origin, selected
        self.stop = False
        self.message = 'Queued explicitly confirmed same-root folder organization'
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

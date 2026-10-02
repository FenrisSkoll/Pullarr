"""Explicit single-job TaskHandler adapter; task history is not a receipt."""

from contextlib import closing

from backend.base.definitions import Task
from backend.base.maintenance_history import HistoryError


class MaintenanceHistoryTask(Task):
    action = 'maintenance_history_action'
    display_title = 'Reviewed maintenance recovery/inverse'
    volume_id = None
    issue_id = None

    def __init__(self, service, identifier, digest, *, operation, confirmed):
        service._confirmation(digest, confirmed)
        if operation not in ('recovery', 'inverse'):
            raise HistoryError('unsupported_history_action')
        self.service, self.identifier, self.digest = service, identifier, digest
        self.operation = operation
        self.stop = False
        self.message = 'Queued explicitly reviewed ' + operation
        self.result = None

    def run(self):
        if self.operation == 'recovery':
            self.result = self.service.recover(self.identifier, self.digest, confirmed=True)
        else:
            registered = self.service.create_inverse(self.identifier, self.digest, confirmed=True)
            if registered['state'] == 'pending':
                with closing(self.service._executor()) as executor:
                    executor.apply_job(registered['id'])
            # Do not retry failed/recovering inverses from an old registration
            # confirmation. Those require a separately reviewed recovery.
            self.result = self.service.history.get('organization', registered['id'])
        return None

    def enqueue(self):
        from backend.features.tasks import TaskHandler
        return TaskHandler().add(self)

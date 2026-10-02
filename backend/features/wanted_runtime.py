"""One application-owned automation worker; all network calls remain bounded."""

from threading import Event, Thread
from time import time

from backend.base.logging import LOGGER
from backend.base.organization_job import OrganizationError
from backend.features.wanted_automation import WantedAutomation
from backend.features.wanted_search import UNIFIED_SEARCH


class WantedRuntime:
    def __init__(self, database, app, *, interval=30, clock=time):
        if interval < 1:
            raise ValueError('Bounded wanted cadence required')
        self.database, self.app, self.interval, self.clock = database, app, interval, clock
        self.stop_event, self.thread = Event(), None
        self.last_tick, self.last_error = None, None

    def tick(self):
        with self.app.app_context():
            # Reuse the application cadence; actual Discover IO stays on TaskHandler.
            discover = self.app.extensions.get('discover')
            if discover is not None:
                discover.tick()
            service = WantedAutomation(self.database, clock=self.clock)
            try:
                service.tick(cancelled=self.stop_event.is_set)
                self.last_tick, self.last_error = self.clock(), None
            finally:
                service.close()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except OrganizationError:
                self.last_error = 'busy'
            except Exception:
                self.last_error = 'systemic_failure'
                LOGGER.error('Wanted automation paused; durable decisions retained')
            self.stop_event.wait(self.interval)

    def start(self):
        if self.thread is not None:
            raise RuntimeError('Wanted worker already started')
        self.thread = Thread(target=self._run, name='WantedAutomation', daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                LOGGER.warning('Wanted worker awaiting bounded network operation during shutdown')
            else:
                UNIFIED_SEARCH.close_all()

    def health(self):
        return {'running': bool(self.thread and self.thread.is_alive()),
                'last_tick': self.last_tick, 'last_error': self.last_error}

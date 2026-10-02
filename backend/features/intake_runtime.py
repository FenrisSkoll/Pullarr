"""One bounded, owned completion-intake worker. No release/provider acquisition."""

import json
import time
from threading import Event, Thread

from backend.base.logging import LOGGER
from backend.base.organization_job import OrganizationError
from backend.features.acquisition_completion import repair_sab_completions
from backend.features.acquisition_intake import IntakeCoordinator
from backend.implementations.organization_filesystem import execution_gate
from backend.internals.organization_jobs import now


class IntakeRuntime:
    def __init__(self, database: str, interval: float = 10, *, clock=time.time):
        if interval < 1:
            raise ValueError('Bounded intake cadence required')
        self.database, self.interval = database, interval
        self.clock = clock
        self.stop_event = Event()
        self.thread = None

    def tick(self) -> None:
        with execution_gate(self.database + '.intake-worker'):
            self._tick()

    def _tick(self) -> None:
        coordinator = IntakeCoordinator(self.database, clock=self.clock)
        try:
            db = coordinator.store.db
            with coordinator.store.transaction():
                repair_sab_completions(db)
            identifiers = tuple(r[0] for r in db.execute('''SELECT id FROM acquisition_intakes
                WHERE state IN ('pending','waiting','observing','ready') AND next_observation<=?
                ORDER BY created_at,id LIMIT 8''', (self.clock(),)))
            # Preview the known batch before allowing any member to execute.
            for identifier in identifiers:
                if self.stop_event.is_set():
                    return
                coordinator.process(identifier, defer_apply=True)
            ready = {}
            ready_rows = db.execute('''SELECT a.id,a.intake_id,a.summary FROM acquisition_artifacts a
                JOIN acquisition_intakes i ON i.id=a.intake_id
                WHERE a.state='ready' AND i.state='ready' AND a.organization_job_id IS NULL
                ORDER BY a.intake_id,a.id LIMIT 8001''').fetchall()
            if len(ready_rows) > 8000:
                # Never apply with a truncated view of potential collisions.
                return
            for row in ready_rows:
                target = json.loads(row[2]).get('target')
                if target:
                    ready.setdefault(target.casefold(), []).append((row[0], row[1]))
            with coordinator.store.transaction():
                for group in ready.values():
                    if len(group) < 2:
                        continue
                    for artifact, intake in group:
                        db.execute("UPDATE acquisition_artifacts SET state='blocked',error='planning',updated_at=? WHERE id=?",
                                   (now(), artifact))
                        coordinator.store.state(intake, 'review')
            for identifier in identifiers:
                if self.stop_event.is_set():
                    return
                if coordinator.store.get(identifier)['state'] == 'ready':
                    coordinator.process(identifier)
        finally:
            coordinator.close()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except OrganizationError:
                LOGGER.warning('Acquisition intake is busy or requires organizer review')
            except Exception:
                LOGGER.error('Acquisition intake paused; durable evidence retained')
            self.stop_event.wait(self.interval)

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError('Intake worker already started')
        self.thread = Thread(target=self._run, name='AcquisitionIntake', daemon=False)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                LOGGER.warning('Intake awaiting bounded archive/filesystem operation during shutdown')

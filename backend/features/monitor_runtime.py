"""Application-owned monitoring worker; disabled installs never scan roots."""

import sqlite3
from threading import Event, Thread
from time import time
from typing import Optional

from backend.base.logging import LOGGER
from backend.base.organization_job import OrganizationError
from backend.features.folder_monitor import FolderMonitor, connect
from backend.features.library_reconciliation import LibraryReconciler


class MonitorRuntime:
    def __init__(self, database: str):
        self.database = database
        self.stop_event = Event()
        self.error: Optional[str] = None
        self.thread = Thread(target=self.run, name='folder-monitor', daemon=False)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            LOGGER.warning('Folder monitor is waiting for a filesystem call; supervisor shutdown may be required')

    def run(self) -> None:
        monitor: Optional[FolderMonitor] = None
        db: Optional[sqlite3.Connection] = None
        failed = False
        try:
            while not self.stop_event.is_set():
                try:
                    if db is None:
                        db = connect(self.database)
                    row = db.execute("SELECT value FROM config WHERE key='folder_monitoring'").fetchone()
                    enabled = bool(row and str(row[0]).lower() in ('1', 'true'))
                    if enabled:
                        if monitor is None:
                            monitor = FolderMonitor(self.database, LibraryReconciler(self.database, db),
                                                    cancelled=self.stop_event.is_set)
                            LOGGER.info('Folder monitor started: bounded polling, DB-only reconciliation')
                        monitor.reevaluate = LibraryReconciler(self.database, db)
                        monitor.tick(time())
                    elif monitor is not None:
                        monitor.close()
                        monitor = None
                        LOGGER.info('Folder monitor disabled')
                    failed = False
                    self.error = None
                except (OSError, sqlite3.Error, OrganizationError, ValueError):
                    if not failed:
                        LOGGER.warning('Folder monitor paused after observation/reconciliation failure; manual workflows remain available')
                    failed = True
                    self.error = 'monitoring_unavailable'
                    if monitor is not None:
                        monitor.close()
                        monitor = None
                    if db is not None:
                        db.close()
                        db = None
                self.stop_event.wait(5 if monitor is None else 1)
        finally:
            if monitor is not None:
                monitor.close()
            if db is not None:
                db.close()
            LOGGER.info('Folder monitor stopped')

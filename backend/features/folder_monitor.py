"""Bounded polling and durable coalescing. No library mutation in this module.

The supplied reevaluator is a domain boundary, not a filesystem event callback.
Only complete generations publish evidence. A failed scan is never an empty scan.
"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import Callable, Iterator, Optional

from backend.base.definitions import FileConstants
from backend.base.folder_monitor import (MonitorLimits, MonitorObservation,
                                         PathStamp, ReevaluationResult)
from backend.base.logging import LOGGER
from backend.base.organization_job import OrganizationError
from backend.implementations.organization_filesystem import (execution_gate,
                                                             safe_path)


def stamp(path: str) -> PathStamp:
    safe_path(path)
    value = os.lstat(path)
    if not (S_ISREG(value.st_mode) or S_ISDIR(value.st_mode)):
        raise ValueError('Unsupported filesystem entry')
    return PathStamp(path, value.st_size, value.st_mtime_ns, value.st_dev,
                     value.st_ino, S_ISDIR(value.st_mode))


def relevant(name: str) -> bool:
    return not name.startswith('.') and (
        name.lower().endswith(tuple(e.lower() for e in FileConstants.CONTAINER_EXTENSIONS))
        or name.lower() in FileConstants.METADATA_FILES)


def walk(root: str, limits: MonitorLimits) -> Iterator[Optional[PathStamp]]:
    """Depth-bounded scandir handles; every entry consumes the caller's budget.

    Links/reparse points fail the generation rather than silently certifying its
    completeness. No directory enumeration list or unbounded pending-path list.
    """
    count = 0

    def descend(path: str, depth: int) -> Iterator[Optional[PathStamp]]:
        nonlocal count
        if depth > limits.max_depth:
            raise ValueError('depth_limit')
        safe_path(path)
        with os.scandir(path) as entries:
            for entry in entries:
                count += 1
                if count > limits.max_entries:
                    raise ValueError('entry_limit')
                if entry.name.startswith('.'):
                    yield None
                    continue
                observed = stamp(entry.path)
                if observed.directory:
                    yield observed
                    yield from descend(entry.path, depth + 1)
                else:
                    yield observed if relevant(entry.name) else None

    yield from descend(root, 0)


def connect(database: str) -> sqlite3.Connection:
    db = sqlite3.connect(Path(database).absolute().as_uri() + '?mode=rw', uri=True,
                         timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def request_reconciliation(database: str, root_id: Optional[int] = None) -> None:
    """Durable root-level hint. No path from a caller is traversed here."""
    with closing(connect(database)) as db, db:
        db.execute("UPDATE monitor_roots SET requested=1,health='unknown' WHERE ? IS NULL OR root_id=?", (root_id, root_id))


def monitoring_status(database: str) -> dict:
    """Bounded read-only projection; no scanning, jobs or raw exceptions."""
    with closing(connect(database)) as db:
        return dict(backend='bounded_polling',
                    roots=[dict(r) for r in db.execute('SELECT * FROM monitor_roots ORDER BY root_id')],
                    counts=[dict(r) for r in db.execute('SELECT status,COUNT(*) AS count FROM monitor_paths GROUP BY status')],
                    review=[dict(r) for r in db.execute('''SELECT root_id,path,change,status,reason,job_id
                        FROM monitor_paths WHERE status IN ('review','missing','error') ORDER BY root_id,path LIMIT 100''')])


class FolderMonitor:
    """Single owned polling session. Clock and domain service are injected.

    No timers or unmanaged threads. The runtime owns tick/shutdown. A separate
    OS gate prevents two monitor processes from sharing incomplete staging.
    """

    def __init__(self, database: str, reevaluate: Callable[[int, PathStamp], ReevaluationResult],
                 limits: MonitorLimits = MonitorLimits(), *, cancelled: Callable[[], bool] = lambda: False):
        self.gate = execution_gate(database + '.monitor')
        self.gate.__enter__()
        try:
            self.db = connect(database)
            self.limits = limits
            self.reevaluate = reevaluate
            self.cancelled = cancelled
            self.scan: Optional[Iterator[Optional[PathStamp]]] = None
            self.root: Optional[sqlite3.Row] = None
            self.root_stamp: Optional[PathStamp] = None
            self.entries_observed = 0
            self.hinted: set[int] = set()
            self.hint_overflow = False
            with self.db:
                self.db.execute('DELETE FROM monitor_staging')
                self.db.execute('UPDATE monitor_roots SET requested=1')
        except BaseException:
            db = getattr(self, 'db', None)
            if db is not None:
                db.close()
            self.gate.__exit__(None, None, None)
            raise

    def close(self) -> None:
        if self.scan is not None:
            closer = getattr(self.scan, 'close', None)
            if closer is not None:
                closer()
        self.scan = None
        self.db.close()
        self.gate.__exit__(None, None, None)

    def hint(self, observation: MonitorObservation) -> None:
        """Coalesce even an event flood to one dirty bit per configured root.

        Notifications cannot supply traversal paths or prove a move/deletion.
        Overflow has the same conservative outcome: a complete reconciliation.
        """
        if len(self.hinted) >= 1024:
            self.hint_overflow = True
        else:
            self.hinted.add(observation.root_id)

    def _roots(self) -> None:
        with self.db:
            self.db.execute('''INSERT INTO monitor_roots(root_id,path)
                SELECT id,folder FROM root_folders WHERE true ON CONFLICT(root_id) DO NOTHING''')
            # Root migration is not an incidental monitoring setting refresh.
            self.db.execute('''UPDATE monitor_roots SET health='review',error='configured_root_changed'
                WHERE path != (SELECT folder FROM root_folders WHERE id=root_id)''')
            if self.hint_overflow:
                self.db.execute('UPDATE monitor_roots SET requested=1')
            else:
                self.db.executemany('UPDATE monitor_roots SET requested=1 WHERE root_id=?', ((r,) for r in self.hinted))
            self.hinted.clear()
            self.hint_overflow = False

    def _start(self, now: float) -> None:
        self._roots()
        self.root = self.db.execute('''SELECT * FROM monitor_roots WHERE health != 'review'
            AND (attempted_at IS NULL OR attempted_at<=?)
            AND (requested=1 OR completed_at IS NULL OR completed_at<=?)
            ORDER BY COALESCE(attempted_at,0),root_id LIMIT 1''',
            (now - min(30, self.limits.scan_interval), now - self.limits.scan_interval)).fetchone()
        if self.root is None:
            return
        root = self.root
        with self.db:
            self.db.execute("UPDATE monitor_roots SET attempted_at=?,requested=0,health='scanning' WHERE root_id=?", (now, root['root_id']))
            self.db.execute('DELETE FROM monitor_staging WHERE root_id=?', (root['root_id'],))
        self.root_stamp = stamp(root['path'])
        if not self.root_stamp.directory:
            raise NotADirectoryError(root['path'])
        if root['device'] is not None and (root['device'], root['inode']) != (self.root_stamp.device, self.root_stamp.inode):
            self._fail('root_identity_changed', review=True)
            return
        self.scan = walk(root['path'], self.limits)

    def _fail(self, code: str, *, review: bool = False) -> None:
        if self.root is not None:
            if self.root['error'] != code:
                LOGGER.warning('Folder monitor root %s paused: %s', self.root['root_id'], code)
            with self.db:
                self.db.execute('UPDATE monitor_roots SET health=?,error=? WHERE root_id=?',
                                ('review' if review else 'unavailable', code, self.root['root_id']))
                self.db.execute('DELETE FROM monitor_staging WHERE root_id=?', (self.root['root_id'],))
        if self.scan is not None:
            closer = getattr(self.scan, 'close', None)
            if closer is not None:
                closer()
        self.scan = None

    def _publish(self, now: float) -> None:
        assert self.root is not None and self.root_stamp is not None
        current = stamp(self.root['path'])
        if (current.device, current.inode) != (self.root_stamp.device, self.root_stamp.inode):
            raise OSError('Root changed during enumeration')
        rid, generation = self.root['root_id'], self.root['generation'] + 1
        # A changed stamp resets stability. Only a distinct complete generation
        # can increment samples. Never infer absence from a partial generation.
        with self.db:
            self.db.execute('''INSERT INTO monitor_paths
                (root_id,path,size,mtime_ns,device,inode,directory,generation,stable_since,samples,change,status)
                SELECT root_id,path,size,mtime_ns,device,inode,directory,?, ?,1,
                       CASE WHEN directory THEN 'directory_changed' ELSE 'appeared' END,
                       CASE WHEN directory THEN 'observed' ELSE 'pending' END
                FROM monitor_staging WHERE root_id=?
                ON CONFLICT(root_id,path) DO UPDATE SET
                    stable_since=CASE WHEN monitor_paths.size=excluded.size AND monitor_paths.mtime_ns=excluded.mtime_ns
                        AND monitor_paths.device=excluded.device AND monitor_paths.inode=excluded.inode
                        AND monitor_paths.status!='missing' THEN monitor_paths.stable_since ELSE excluded.stable_since END,
                    samples=CASE WHEN monitor_paths.size=excluded.size AND monitor_paths.mtime_ns=excluded.mtime_ns
                        AND monitor_paths.device=excluded.device AND monitor_paths.inode=excluded.inode
                        AND monitor_paths.status!='missing' THEN MIN(monitor_paths.samples+1,2) ELSE 1 END,
                    status=CASE WHEN monitor_paths.size=excluded.size AND monitor_paths.mtime_ns=excluded.mtime_ns
                        AND monitor_paths.device=excluded.device AND monitor_paths.inode=excluded.inode
                        AND monitor_paths.status!='missing' THEN monitor_paths.status ELSE excluded.status END,
                    change=CASE WHEN excluded.directory THEN 'directory_changed'
                        WHEN monitor_paths.status='missing' THEN 'appeared'
                        WHEN monitor_paths.size!=excluded.size OR monitor_paths.mtime_ns!=excluded.mtime_ns
                        OR monitor_paths.device!=excluded.device OR monitor_paths.inode!=excluded.inode
                        THEN 'modified' ELSE monitor_paths.change END,
                    size=excluded.size,mtime_ns=excluded.mtime_ns,device=excluded.device,inode=excluded.inode,
                    directory=excluded.directory,generation=excluded.generation''', (generation, now, rid))
            self.db.execute('''UPDATE monitor_paths SET status='missing',change='disappeared',reason='absence_requires_review'
                WHERE root_id=? AND generation!=?''', (rid, generation))
            if self.db.execute('SELECT COUNT(*) FROM monitor_paths WHERE root_id=?', (rid,)).fetchone()[0] > self.limits.max_entries:
                raise ValueError('retained_evidence_limit')
            self.db.execute('''UPDATE monitor_roots SET generation=?,completed_at=?,health='healthy',error=NULL,
                device=?,inode=? WHERE root_id=?''', (generation, now, current.device, current.inode, rid))
            self.db.execute('DELETE FROM monitor_staging WHERE root_id=?', (rid,))
        self.scan = None
        if self.root['health'] == 'unavailable':
            LOGGER.info('Folder monitor root %s restored after complete reconciliation', rid)

    def _process(self, now: float) -> None:
        rows = self.db.execute('''SELECT p.* FROM monitor_paths p JOIN monitor_roots r USING(root_id)
            WHERE p.status='pending' AND p.samples>=2 AND p.stable_since<=? AND r.health='healthy'
            AND r.generation=p.generation ORDER BY p.checked_at,p.stable_since,p.root_id,p.path LIMIT ?''',
            (now - self.limits.stability_interval, self.limits.work_per_tick)).fetchall()
        for row in rows:
            if self.cancelled():
                return
            observed = PathStamp(row['path'], row['size'], row['mtime_ns'], row['device'], row['inode'], bool(row['directory']))
            try:
                if stamp(observed.path) != observed:
                    self.db.execute('UPDATE monitor_roots SET requested=1 WHERE root_id=?', (row['root_id'],))
                    self.db.execute('UPDATE monitor_paths SET checked_at=? WHERE root_id=? AND path=?',
                                    (now, row['root_id'], observed.path))
                    self.db.commit()
                    continue
                result = self.reevaluate(row['root_id'], observed)
            except (OSError, OrganizationError):
                # A vanished/locked file is not stable work; retry after a scan.
                with self.db:
                    self.db.execute("UPDATE monitor_paths SET checked_at=?,reason='observation_unavailable' WHERE root_id=? AND path=?",
                                    (now, row['root_id'], observed.path))
                continue
            if result.status not in ('pending', 'review', 'reconciled', 'error'):
                raise ValueError('Invalid domain reevaluation result')
            with self.db:
                self.db.execute('''UPDATE monitor_paths SET status=?,reason=?,job_id=?,checked_at=?
                    WHERE root_id=? AND path=? AND generation=?''',
                    (result.status, result.reason, result.job_id, now, row['root_id'], row['path'], row['generation']))

    def tick(self, now: float) -> None:
        """One bounded enumeration slice and bounded ready-work batch."""
        try:
            if self.scan is None:
                self._start(now)
            if self.scan is not None:
                values = []
                complete = False
                for _ in range(self.limits.entries_per_tick):
                    if self.cancelled():
                        return
                    try:
                        value = next(self.scan)
                    except StopIteration:
                        complete = True
                        break
                    self.entries_observed += 1
                    if value is not None:
                        assert self.root is not None
                        values.append((self.root['root_id'], value.path, value.size, value.mtime_ns,
                                       value.device, value.inode, int(value.directory)))
                with self.db:
                    self.db.executemany('INSERT INTO monitor_staging VALUES(?,?,?,?,?,?,?)', values)
                if complete:
                    self._publish(now)
        except (OSError, OrganizationError, ValueError) as error:
            code = ('permission_denied' if isinstance(error, PermissionError) else
                    str(error) if isinstance(error, ValueError) and str(error) in
                    ('entry_limit', 'depth_limit', 'retained_evidence_limit') else 'unsafe_or_incomplete_scan')
            self._fail(code, review=isinstance(error, ValueError))
        self._process(now)

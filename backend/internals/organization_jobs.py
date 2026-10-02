"""Dedicated short SQLite checkpoints on the application database.

No caller transaction is committed. Connections must point to an existing,
migrated file-backed database. No table creation or migration occurs here.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4

from backend.base.organization_job import (EXECUTOR_POLICY, ExecutionCode,
                                           JobState, OrganizationError,
                                           OrganizationJob, StepReceipt,
                                           StepState)
from backend.internals.organization_reservations import load_reservations

MAX_PAYLOAD = 6 * 1024 * 1024


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value: str) -> str:
    return sha256(value.encode('utf-8')).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_intent(reader, job: str) -> dict:
    """Verify durable intent on either the writer or a query-only connection."""
    row = reader.execute('SELECT executor_version,intent,intent_digest FROM organization_jobs WHERE id=?', (job,)).fetchone()
    if row is None:
        raise KeyError(job)
    if row[0] != EXECUTOR_POLICY or len(row[1]) > MAX_PAYLOAD or digest(row[1]) != row[2]:
        raise OrganizationError(ExecutionCode.CORRUPT)
    try:
        value = json.loads(row[1])
        if not isinstance(value, dict) or value.get('version') != EXECUTOR_POLICY:
            raise ValueError()
        return value
    except (ValueError, TypeError, RecursionError):
        raise OrganizationError(ExecutionCode.CORRUPT) from None


class JobStore:
    def __init__(self, database: str):
        self.path = str(Path(database).absolute())
        if not Path(self.path).is_file():
            raise OrganizationError(ExecutionCode.DATABASE, 'Existing application database required')
        self.db = sqlite3.connect(Path(self.path).as_uri() + '?mode=rw', uri=True, timeout=10,
                                  isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("""SELECT value,EXISTS(SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='quarantined_files')
            FROM config WHERE key='database_version'""").fetchone()
        if version is None or int(version[0]) not in (54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72):
            self.db.close()
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Supported organizer schema required (54–72)')
        self.has_quarantine_state = bool(version[1])

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.db.in_transaction:
            raise RuntimeError('Nested organizer write transaction')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def event(self, job: str, event: str, detail: object = None,
              ordinal: Optional[int] = None) -> None:
        self.db.execute('INSERT INTO organization_events(job_id,ordinal,created_at,event,detail) VALUES(?,?,?,?,?)',
                        (job, ordinal, now(), event, canonical(detail)))

    def create(self, intent: dict, plan_digest: str, reservations: tuple[str, ...],
               batch_id: Optional[str] = None, inverse_of: Optional[str] = None) -> str:
        with self.transaction():
            return self.create_in_transaction(intent, plan_digest, reservations, batch_id, inverse_of)

    def create_in_transaction(self, intent: dict, plan_digest: str, reservations: tuple[str, ...],
                              batch_id: Optional[str] = None, inverse_of: Optional[str] = None) -> str:
        """Registration only: caller owns the serialized transaction; no commit."""
        if not self.db.in_transaction:
            raise RuntimeError('Organizer registration requires owned write transaction')
        payload = canonical(intent)
        if len(payload) > MAX_PAYLOAD:
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Journal intent too large')
        authority = intent.get('rename_authority') or intent.get('repair_authority')
        if authority is not None:
            from backend.base.switch_review import SwitchReviewError
            from backend.internals.provider_authority import (AuthorityToken,
                                                              require_current)
            try:
                require_current(self.db.cursor(), (AuthorityToken(**authority),))
            except (SwitchReviewError, TypeError, ValueError):
                raise OrganizationError(ExecutionCode.STALE) from None
        existing = self.db.execute('SELECT id,intent_digest FROM organization_jobs WHERE plan_digest=?', (plan_digest,)).fetchone()
        if existing:
            if existing['intent_digest'] != digest(payload):
                raise OrganizationError(ExecutionCode.CORRUPT)
            return str(existing['id'])
        index = load_reservations(self.db.cursor())
        if any(index.conflicts(path) for path in reservations):
            raise OrganizationError(ExecutionCode.BUSY)
        job, stamp = uuid4().hex, now()
        self.db.execute('''INSERT INTO organization_jobs
            (id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at,inverse_of,batch_id)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (job, plan_digest, EXECUTOR_POLICY, payload, digest(payload), JobState.PENDING.value, stamp, stamp, inverse_of, batch_id))
        self.db.executemany('INSERT INTO organization_steps(job_id,ordinal,kind,state) VALUES(?,?,?,?)',
                            ((job, n, k, StepState.PENDING.value) for n, k in enumerate(intent['effects'])))
        try:
            self.db.executemany('INSERT INTO organization_reservations(path_key,job_id) VALUES(?,?)',
                                ((p, job) for p in sorted(set(reservations))))
        except sqlite3.IntegrityError:
            raise OrganizationError(ExecutionCode.BUSY) from None
        self.event(job, 'intent_persisted')
        return job

    def intent(self, job: str) -> dict:
        return read_intent(self.db, job)

    def get(self, job: str) -> OrganizationJob:
        intent = self.intent(job)
        row = self.db.execute('SELECT * FROM organization_jobs WHERE id=?', (job,)).fetchone()
        assert row is not None
        try:
            rows = self.db.execute('SELECT ordinal,kind,state,evidence,evidence_digest FROM organization_steps WHERE job_id=? ORDER BY ordinal', (job,)).fetchall()
            if any(len(r[3]) > MAX_PAYLOAD or digest(r[3]) != r[4] or not isinstance(json.loads(r[3]), dict) for r in rows):
                raise ValueError()
            steps = tuple(StepReceipt(r[0], r[1], StepState(r[2]), r[3]) for r in rows)
            if tuple(s.kind for s in steps) != tuple(intent['effects']) or tuple(s.ordinal for s in steps) != tuple(range(len(steps))):
                raise ValueError()
            return OrganizationJob(job, row['plan_digest'], JobState(row['state']), intent['source'], intent['target'],
                                   row['created_at'], row['updated_at'], steps, row['error'], row['inverse_of'], row['batch_id'])
        except (ValueError, KeyError, TypeError):
            raise OrganizationError(ExecutionCode.CORRUPT) from None

    def state(self, job: str, state: JobState, error: Optional[ExecutionCode] = None) -> None:
        self.db.execute('UPDATE organization_jobs SET state=?,updated_at=?,error=? WHERE id=?',
                        (state.value, now(), error.value if error else None, job))
        self.event(job, state.value, error.value if error else None)

    def checkpoint(self, job: str, ordinal: int, state: StepState, evidence: dict) -> None:
        payload = canonical(evidence)
        self.db.execute('UPDATE organization_steps SET state=?,evidence=?,evidence_digest=? WHERE job_id=? AND ordinal=?',
                        (state.value, payload, digest(payload), job, ordinal))
        self.event(job, state.value, None, ordinal)

    def history(self) -> tuple[str, ...]:
        return tuple(r[0] for r in self.db.execute('SELECT id FROM organization_jobs ORDER BY created_at,id'))

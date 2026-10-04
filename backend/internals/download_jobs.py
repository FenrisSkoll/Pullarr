"""Short durable checkpoints. Never persist NZB bytes, URLs or API keys."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, DownloadJobState as S,
                                       GrabIntent, RemoteDownload)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def intent_receipt(intent: GrabIntent) -> dict:
    result = dict(request_id=intent.request_id, evaluation_id=intent.evaluation_id,
        candidate_id=intent.candidate_id, source_key=intent.source_key, resolver_key=intent.resolver_key,
        volume_id=intent.volume_id, issue_ids=intent.issue_ids, target_digest=intent.target_digest,
        scoring_fingerprint=intent.scoring_fingerprint, title=intent.title, source_name=intent.source_name,
        client_id=intent.client_id, client_instance=intent.client_instance, category=intent.category,
        priority=intent.priority, policy=intent.policy, client_kind=intent.client_kind, protocol=intent.protocol)
    if intent.authorization != 'manual':
        result['authorization'] = intent.authorization
    return result


class DownloadStore:
    def __init__(self, database: str):
        self.path = str(Path(database).absolute())
        self.db = sqlite3.connect(Path(self.path).as_uri() + '?mode=rw', uri=True,
                                  timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
        if not version or int(version[0]) not in (56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72):
            self.db.close()
            raise DownloadFailure(E.CONFIGURATION)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, identifier):
        row = self.db.execute('SELECT * FROM acquisition_downloads WHERE id=?', (identifier,)).fetchone()
        if row is None or sha256(row['intent'].encode()).hexdigest() != row['intent_digest']:
            raise DownloadFailure(E.SELECTION)
        return dict(row)

    def event(self, identifier, state, error=None):
        self.db.execute('INSERT INTO acquisition_download_events(job_id,created_at,state,error) VALUES(?,?,?,?)',
                        (identifier, now(), state, error))

    def create(self, intent: GrabIntent):
        payload = canonical(intent_receipt(intent))
        if len(payload) > 32768:
            raise DownloadFailure(E.SELECTION)
        digest = sha256(payload.encode()).hexdigest()
        with self.transaction():
            existing = self.db.execute('SELECT intent_digest FROM acquisition_downloads WHERE id=?', (intent.request_id,)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise DownloadFailure(E.SELECTION)
            else:
                stamp = now()
                self.db.execute('''INSERT INTO acquisition_downloads
                    (id,intent_digest,intent,client_id,client_instance,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (intent.request_id, digest, payload,
                    intent.client_id, intent.client_instance, S.PENDING.value, stamp, stamp))
                self.event(intent.request_id, S.PENDING.value)
        return self.get(intent.request_id)

    def recover(self):
        """Caller must own submission gate; never steal a live SUBMITTING claim."""
        with self.transaction():
            rows = self.db.execute('SELECT id FROM acquisition_downloads WHERE state=?', (S.SUBMITTING.value,)).fetchall()
            for row in rows:
                self.db.execute('UPDATE acquisition_downloads SET state=?,error=?,updated_at=? WHERE id=?',
                    (S.AMBIGUOUS.value, E.AMBIGUOUS.value, now(), row[0]))
                self.event(row[0], S.AMBIGUOUS.value, E.AMBIGUOUS.value)

    def begin_submission(self, identifier, nzb):
        with self.transaction():
            result = self.db.execute('''UPDATE acquisition_downloads SET state=?,nzb_digest=?,nzb_size=?,
                error=NULL,updated_at=? WHERE id=? AND state=?''',
                (S.SUBMITTING.value, nzb.digest, len(nzb.data), now(), identifier, S.PENDING.value))
            if result.rowcount != 1:
                raise DownloadFailure(E.BUSY)
            row = self.get(identifier)
            if row['state'] != S.SUBMITTING.value or row['nzb_digest'] != nzb.digest or row['nzb_size'] != len(nzb.data):
                raise DownloadFailure(E.BUSY)
            self.event(identifier, S.SUBMITTING.value)

    def submitted(self, identifier, nzo_id):
        with self.transaction():
            stamp = now()
            result = self.db.execute('''UPDATE acquisition_downloads SET state=?,nzo_id=?,submitted_at=?,updated_at=?,error=NULL
                WHERE id=? AND state=?''', (S.SUBMITTED.value, nzo_id, stamp, stamp, identifier, S.SUBMITTING.value))
            row = self.get(identifier)
            if result.rowcount != 1 or row['nzo_id'] != nzo_id or row['state'] != S.SUBMITTED.value:
                raise DownloadFailure(E.AMBIGUOUS)
            self.event(identifier, S.SUBMITTED.value)

    def failure(self, identifier, code, *, submission=False):
        with self.transaction():
            row = self.get(identifier)
            state = row['state']
            if submission and state == S.SUBMITTING.value:
                state = S.FAILED.value if code in (E.AUTHENTICATION, E.REJECTED) else S.AMBIGUOUS.value
            self.db.execute('UPDATE acquisition_downloads SET state=?,error=?,updated_at=?,observed_at=? WHERE id=?',
                (state, code.value, now(), now(), identifier))
            if state != row['state'] or code.value != row['error'] or state == S.PENDING.value:
                self.event(identifier, state, code.value)

    def observe(self, identifier, remote: RemoteDownload):
        with self.transaction():
            row = self.get(identifier)
            if row['nzo_id'] != remote.nzo_id:
                raise DownloadFailure(E.SELECTION)
            state = row['state']
            # Terminal historical receipts never regress after remote deletion/retry.
            terminal = state in (S.COMPLETED.value, S.FAILED.value)
            observation = canonical(dict(status=remote.status, category=remote.category,
                progress=remote.progress, storage=remote.storage, completed=remote.completed, paths=remote.paths))
            if terminal:
                observation = row['observation']
            else:
                state = remote.state.value
            completed = row['completed_at'] or (now() if state == S.COMPLETED.value else None)
            error = remote.error.value if remote.error else None
            self.db.execute('''UPDATE acquisition_downloads SET state=?,observation=?,error=?,
                updated_at=?,observed_at=?,completed_at=? WHERE id=?''',
                (state, observation, error, now(), now(), completed, identifier))
            if state != row['state'] or error != row['error']:
                self.event(identifier, state, error)
            version = self.db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
            if state == S.COMPLETED.value and version and int(version[0]) >= 57:
                from backend.features.acquisition_completion import \
                    ensure_sab_completion
                ensure_sab_completion(self.db, self.get(identifier))

    def pending_observations(self, limit=1000):
        if not 1 <= limit <= 1000:
            raise DownloadFailure(E.CONFIGURATION)
        return tuple(dict(row) for row in self.db.execute('''SELECT * FROM acquisition_downloads
            WHERE nzo_id IS NOT NULL AND state NOT IN (?,?) ORDER BY observed_at,id LIMIT ?''',
            (S.COMPLETED.value, S.FAILED.value, limit)))

    def preview(self, identifier):
        row = self.get(identifier)
        intent = json.loads(row['intent'])
        return dict(id=row['id'], title=intent['title'], source=intent['source_name'],
            volume_id=intent['volume_id'], issue_ids=intent['issue_ids'], candidate_id=intent['candidate_id'],
            evaluation_id=intent['evaluation_id'], downloader=row['client_id'], category=intent['category'],
            state=row['state'], nzo_id=row['nzo_id'], error=row['error'], submitted_at=row['submitted_at'],
            completed_at=row['completed_at'], observation=json.loads(row['observation']))

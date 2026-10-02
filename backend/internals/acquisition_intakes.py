"""Durable intake facts. No filesystem mutations, source URLs or domain blobs."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Iterator, Optional

from backend.base.acquisition_intake import (AcquisitionCompletion,
                                             AcquisitionKind,
                                             DownloaderPathMapping,
                                             IntakeErrorCode as E,
                                             IntakeFailure)
from backend.internals.organization_jobs import canonical, now


def completion_receipt(completion: AcquisitionCompletion) -> dict:
    value = asdict(completion)
    value['kind'] = completion.kind.value
    return value


def ensure_intake(db: sqlite3.Connection, completion: AcquisitionCompletion, *,
                  rename: bool, auto_apply: bool, local_root: Optional[str] = None) -> str:
    """Caller transaction owns atomicity with its authoritative completion.

    Repeat observation does not overwrite policy, paths or provenance. Conflicting
    completion facts for the same durable download identity fail explicitly.
    """
    payload = canonical(completion_receipt(completion))
    if len(payload.encode()) > 65536:
        raise IntakeFailure(E.CONFIGURATION)
    digest = sha256(payload.encode()).hexdigest()
    identifier = sha256(canonical((completion.kind.value, completion.download_id)).encode()).hexdigest()
    row = db.execute('SELECT completion_digest FROM acquisition_intakes WHERE id=?', (identifier,)).fetchone()
    if row is not None:
        if row[0] != digest:
            raise IntakeFailure(E.CONFIGURATION)
        return identifier
    db.execute('''INSERT INTO acquisition_intakes
        (id,kind,download_id,completion,completion_digest,local_root,rename,auto_apply,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (identifier, completion.kind.value, completion.download_id,
        payload, digest, local_root, int(rename), int(auto_apply), now(), now()))
    return identifier


class IntakeStore:
    def __init__(self, database: str):
        self.path = str(Path(database).absolute())
        self.db = sqlite3.connect(Path(self.path).as_uri() + '?mode=rw', uri=True,
                                 timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        # Runtime never silently installs tables in an unmigrated database.
        if self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='acquisition_intakes'").fetchone() is None:
            self.db.close()
            raise IntakeFailure(E.CONFIGURATION)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, identifier: str) -> dict:
        row = self.db.execute('SELECT * FROM acquisition_intakes WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        result = dict(row)
        if (len(result['completion']) > 65536
                or sha256(result['completion'].encode()).hexdigest() != result['completion_digest']):
            raise IntakeFailure(E.CONFIGURATION)
        return result

    def completion(self, identifier: str) -> AcquisitionCompletion:
        try:
            data = json.loads(self.get(identifier)['completion'])
            data['kind'] = AcquisitionKind(data['kind'])
            data['issue_ids'] = tuple(data['issue_ids'])
            data['reported_paths'] = tuple(data['reported_paths'])
            return AcquisitionCompletion(**data)
        except (ValueError, TypeError, KeyError):
            raise IntakeFailure(E.CONFIGURATION) from None

    def state(self, identifier: str, state: str, error: Optional[E] = None,
              next_observation: float = 0) -> None:
        self.db.execute('''UPDATE acquisition_intakes SET state=?,error=?,next_observation=?,updated_at=?
            WHERE id=?''', (state, error.value if error else None, next_observation, now(), identifier))

    def artifacts(self, identifier: str) -> tuple[dict, ...]:
        return tuple(dict(r) for r in self.db.execute(
            '''SELECT a.*,j.state AS job_state FROM acquisition_artifacts a
               LEFT JOIN organization_jobs j ON j.id=a.organization_job_id
               WHERE a.intake_id=? ORDER BY a.path,a.id''', (identifier,)))

    def mappings(self) -> tuple[DownloaderPathMapping, ...]:
        return tuple(DownloaderPathMapping(r['id'], r['client_id'], r['client_instance'],
            r['remote_prefix'], r['local_root'], r['remote_style'], bool(r['enabled']), r['local_prefix'])
            for r in self.db.execute('SELECT * FROM acquisition_path_mappings ORDER BY id LIMIT 101'))

    def preview(self, identifier: str) -> dict:
        row, completion = self.get(identifier), self.completion(identifier)
        preparation = json.loads(row['preparation'])
        notes = ['ComicInfo writing is OFF; no staging cleanup or overwrite.']
        if any(preparation.get(k) not in (None, '', 'None') for k in ('change_file_date', 'chmod_folder', 'chown_group')):
            notes.append('Legacy date/permission/ownership overrides are not applied: they are not journaled organizer effects. Existing file properties are preserved by move.')
        return dict(id=identifier, kind=completion.kind.value, download_id=completion.download_id,
            title=completion.title, volume_id=completion.volume_id, issue_ids=list(completion.issue_ids),
            state=row['state'], error=row['error'], source=completion.source_key,
            mapping_fingerprint=row['mapping_fingerprint'], next_observation=row['next_observation'],
            policy_notes=notes,
            artifacts=[dict(
                id=a['id'], path=a['path'], state=a['state'], error=a['error'],
                organization_job_id=a['organization_job_id'], job_state=a['job_state'],
                final_file_id=a['final_file_id'], summary=json.loads(a['summary']))
                for a in self.artifacts(identifier)])

    def retry(self, identifier: str) -> None:
        from backend.implementations.organization_filesystem import \
            execution_gate
        with execution_gate(self.path + '.intake'), self.transaction():
            row = self.get(identifier)
            if row['state'] == 'completed':
                raise IntakeFailure(E.CONFIGURATION)
            # Linked work is inspected by the coordinator; never cleared here.
            linked = self.db.execute('''SELECT 1 FROM acquisition_artifacts a
                WHERE a.intake_id=? AND (a.organization_job_id IS NOT NULL OR EXISTS
                (SELECT 1 FROM organization_jobs j WHERE j.batch_id='intake-artifact:'||a.id)) LIMIT 1''',
                (identifier,)).fetchone()
            if not linked:
                self.db.execute("UPDATE acquisition_artifacts SET stamp='',stable_since=0,state='observing' WHERE intake_id=? AND state!='prepared'",
                                (identifier,))
            self.state(identifier, 'pending')

"""Trusted internal TaskHandler adapters; no HTTP or second worker queue."""

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from backend.base.definitions import Task
from backend.base.metadata_repair import RepairError
from backend.base.switch_review import SwitchReviewError
from backend.internals.provider_authority import AuthorityToken


@dataclass(frozen=True)
class RepairConfirmation:
    family: str
    session_id: str
    revision: int
    digest: str
    authority: AuthorityToken

    def __post_init__(self):
        if (self.family not in ('database', 'comicinfo') or not isinstance(self.session_id, str)
                or not 1 <= len(self.session_id) <= 128 or type(self.revision) is not int or self.revision < 0
                or not isinstance(self.digest, str) or len(self.digest) != 64 or not isinstance(self.authority, AuthorityToken)):
            raise RepairError('invalid_repair_confirmation')


def apply_batch(cursor, database_service, comicinfo_service, confirmations, *, confirmed):
    if (confirmed is not True or type(confirmations) is not tuple or not 1 <= len(confirmations) <= 100
            or any(not isinstance(c, RepairConfirmation) for c in confirmations)):
        raise RepairError('explicit_bounded_batch_confirmation_required')
    outcomes, seen, blocked = [], {}, set()
    for n, command in enumerate(confirmations):
        service = database_service if command.family == 'database' else comicinfo_service
        try:
            session = service.get(cursor, command.session_id)
            if command.revision != session.revision or command.digest != session.digest or command.authority != session.authority:
                raise RepairError('stale_repair_confirmation')
            key = (command.family, session.authority.volume_id if command.family == 'database' else session.plan.source_path)
            if key in seen:
                blocked.update((n, seen[key]))
            seen[key] = n
        except (RepairError, SwitchReviewError):
            # Durable retry is still attempted below; absent transient review is
            # not proof of failure. A fresh unknown session will fail safely.
            pass
    for n, command in enumerate(confirmations):
        if n in blocked:
            outcomes.append(dict(state='blocked', reason='duplicate_repair_target', session_id=command.session_id))
            continue
        service = database_service if command.family == 'database' else comicinfo_service
        try:
            result = service.apply(cursor, command.session_id, command.revision, command.digest,
                                   confirmed=True, expected_authority=command.authority)
            outcomes.append(dict(result, family=command.family, session_id=command.session_id))
        except (RepairError, SwitchReviewError) as error:
            outcomes.append(dict(state='blocked', reason=str(error), session_id=command.session_id))
        except Exception:
            # Each callee owns rollback/journaling. Never conceal previous wins.
            outcomes.append(dict(state='failed', reason='repair_failed_inspect_history', session_id=command.session_id))
    successes = sum(r['state'] in ('applied', 'already_applied', 'completed', 'no_changes') for r in outcomes)
    return dict(state='completed' if successes == len(outcomes) else 'partially_completed_batch' if successes else 'blocked_or_failed',
                outcomes=outcomes, atomic=False)


class MetadataRepairTask(Task):
    action = 'metadata_repair'
    display_title = 'Reviewed metadata repair'
    volume_id = None
    issue_id = None

    def __init__(self, database_service, *, handoff=None, comicinfo_service=None, confirmations=None, confirmed=False):
        if (handoff is None) == (confirmations is None):
            raise RepairError('choose_acquisition_or_confirmed_batch')
        if confirmations is not None and confirmed is not True:
            raise RepairError('explicit_bounded_batch_confirmation_required')
        self.service, self.comicinfo = database_service, comicinfo_service
        self.handoff, self.confirmations = handoff, confirmations
        self.stop = False
        self.message = 'Queued metadata repair acquisition' if handoff else 'Queued explicitly confirmed repair batch'
        self.result = None

    def run(self):
        db = sqlite3.connect(Path(self.service.maintenance.database).as_uri() + '?mode=rw', uri=True, timeout=10)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if self.handoff is not None:
                self.result = asyncio.run(self.service.create(db.cursor(), *self.handoff))
            else:
                self.result = apply_batch(db.cursor(), self.service, self.comicinfo, self.confirmations, confirmed=True)
        finally:
            db.close()
        return None

    def enqueue(self):
        from backend.features.tasks import TaskHandler
        return TaskHandler().add(self)

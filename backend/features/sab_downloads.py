"""Explicit selection/submission and restart-safe observation; no import."""

from contextlib import contextmanager
from hashlib import sha256
from threading import Event, Thread
from time import time
from typing import Optional
from uuid import UUID, uuid4

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, DownloadJobState as S,
                                       GrabIntent, SABConfig)
from backend.base.logging import LOGGER
from backend.base.organization_job import OrganizationError
from backend.base.release_candidate import (AcquisitionMechanism,
                                            ReleaseCandidate)
from backend.base.release_evaluation import (Compatibility, ReleaseEvaluation,
                                             ScoringPolicy, WantedTarget)
from backend.implementations.nzb_resolution import SelectedSourceSession
from backend.implementations.organization_filesystem import execution_gate
from backend.implementations.release_explanations import evaluation_identity
from backend.implementations.sabnzbd import STATUS_BATCH, SABClient
from backend.internals.download_jobs import DownloadStore
from backend.internals.managed_clients import load_clients


@contextmanager
def download_gate(database):
    # Reuse the established OS-lock primitive in a separate acquisition namespace.
    try:
        with execution_gate(database + '.sab'):
            yield
    except OrganizationError:
        raise DownloadFailure(E.BUSY) from None


def create_grab_intent(candidate: ReleaseCandidate, evaluation: ReleaseEvaluation,
                       target: WantedTarget, policy: ScoringPolicy, client: SABConfig,
                       *, request_id: Optional[str] = None, force: bool = False) -> GrabIntent:
    """Only explicit caller selection invokes this. No ranking or scorer call."""
    import json

    from backend.features.direct_downloads import force_available
    allowed = (force_available(evaluation) if force else
               evaluation.state == Compatibility.COMPATIBLE and evaluation.score is not None
               and not evaluation.rejections and not any(c.gate is not None for c in evaluation.components))
    if (candidate != evaluation.candidate or target != evaluation.target
            or evaluation.policy_id != policy.policy_id or evaluation.policy_fingerprint != policy.fingerprint
            or not allowed
            or evaluation.quality_receipt and json.loads(evaluation.quality_receipt)['result'] in ('not_allowed', 'equal', 'downgrade')
            or candidate.acquisition.mechanism.value != getattr(client, 'protocol', 'nzb')
            or not candidate.candidate_id or not candidate.acquisition.key or not client.enabled):
        raise DownloadFailure(E.SELECTION)
    identifier = request_id or uuid4().hex
    try:
        if UUID(identifier).hex != identifier:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DownloadFailure(E.SELECTION) from None
    if client.api_key and (client.api_key in candidate.raw_title or client.api_key in candidate.source.name):
        raise DownloadFailure(E.SELECTION)
    return GrabIntent(identifier, evaluation_identity(evaluation), candidate.candidate_id,
        candidate.source.key, candidate.acquisition.key, target.publication.id, target.issue_ids,
        sha256(repr(target).encode()).hexdigest(), evaluation.policy_fingerprint,
        candidate.raw_title, candidate.source.name + (' via ' + candidate.source.via if candidate.source.via else ''),
        client.key, client.instance, client.category, client.priority,
        client_kind=getattr(client, 'kind', 'sabnzbd'), protocol=getattr(client, 'protocol', 'nzb'),
        authorization='forced_manual' if force else 'manual')


def submit_selected(store: DownloadStore, intent: GrabIntent, candidate: ReleaseCandidate,
                    evaluation: ReleaseEvaluation, target: WantedTarget, policy: ScoringPolicy,
                    session: SelectedSourceSession, client: SABClient, *, checkpoint=lambda stage: None):
    """Persist, validate, resolve, commit SUBMITTING, upload once, persist receipt.

    The caller supplies trusted server-side objects, not client-authored DTOs.
    A new UUID is a new explicit repeat; reusing an intent UUID is replay-safe.
    """
    expected = create_grab_intent(candidate, evaluation, target, policy, client.config,
                                 request_id=intent.request_id, force=intent.authorization == 'forced_manual')
    if intent != expected:
        raise DownloadFailure(E.SELECTION)
    with download_gate(store.path):
        store.recover()
        row = store.create(intent)
        if row['state'] != S.PENDING.value:
            return store.preview(intent.request_id)
        try:
            session.check(candidate)
            checkpoint('intent_persisted')
            client.check()
            nzb = session.resolve(candidate)
            if nzb.candidate_id != intent.candidate_id or nzb.source_key != intent.source_key:
                raise DownloadFailure(E.SELECTION)
            checkpoint('resolved')
        except DownloadFailure as exc:
            store.failure(intent.request_id, exc.code)
            raise
        store.begin_submission(intent.request_id, nzb)
        if intent.protocol == 'torrent':
            from dataclasses import asdict

            from backend.base.torrent import ResolvedTorrent
            from backend.internals.organization_jobs import canonical
            if not isinstance(nzb, ResolvedTorrent):
                raise DownloadFailure(E.SELECTION)
            duplicate = store.db.execute('''SELECT t.download_id FROM acquisition_torrents t
                JOIN acquisition_downloads d ON d.id=t.download_id
                WHERE t.download_id<>? AND d.client_instance=? AND
                ((? IS NOT NULL AND t.infohash_v1=?) OR (? IS NOT NULL AND t.infohash_v2=?))
                AND t.state NOT IN ('removed_keep','removed_data') LIMIT 1''',
                (intent.request_id, client.config.instance, nzb.identity.v1, nzb.identity.v1,
                 nzb.identity.v2, nzb.identity.v2)).fetchone()
            if duplicate:
                store.failure(intent.request_id, E.REJECTED, submission=True)
                raise DownloadFailure(E.REJECTED)
            facts = dict(candidate.torrent_facts)
            requirements = {k: facts[k] for k in ('minimumratio','minimumseedtime','seedtype') if k in facts}
            if 'minimumseedtime' in requirements:
                requirements['minimumseedtime'] = int(requirements['minimumseedtime'])
            store.db.execute('''INSERT INTO acquisition_torrents(download_id,infohash_v1,infohash_v2,policy,requirements)
                VALUES(?,?,?,?,?)''', (intent.request_id, nzb.identity.v1, nzb.identity.v2,
                canonical(asdict(client.config.retention)), canonical(requirements)))
        checkpoint('submitting')
        try:
            identifier = client.submit(nzb, intent.title)
        except DownloadFailure as exc:
            store.failure(intent.request_id, exc.code, submission=True)
            raise
        checkpoint('remote_accepted')
        try:
            store.submitted(intent.request_id, identifier)
        except Exception:
            # The pre-upload SUBMITTING receipt survives even if this write fails.
            try:
                store.failure(intent.request_id, E.AMBIGUOUS, submission=True)
            except Exception:
                pass
            raise DownloadFailure(E.AMBIGUOUS) from None
        checkpoint('receipt_persisted')
        return store.preview(intent.request_id)


def poll_downloads(store: DownloadStore, configs, *, client_factory=SABClient,
                   cancelled=lambda: False, identifiers=None):
    """Observe only owned IDs; explicit callers can also inspect terminal jobs."""
    with download_gate(store.path):
        store.recover()
        rows = store.pending_observations() if identifiers is None else tuple(store.get(i) for i in identifiers)
        if len(rows) > 1000:
            raise DownloadFailure(E.CONFIGURATION)
        clients = {c.key: c for c in configs}
        groups = {}
        for row in rows:
            if not row['nzo_id']:
                continue
            config = clients.get(row['client_id'])
            if not config or config.instance != row['client_instance'] or not config.enabled:
                store.failure(row['id'], E.DRIFT)
                continue
            groups.setdefault(config.key, []).append(row)
        for key in sorted(groups):
            client = client_factory(clients[key])
            for offset in range(0, len(groups[key]), STATUS_BATCH):
                if cancelled():
                    return
                batch = groups[key][offset:offset + STATUS_BATCH]
                try:
                    observed = client.observe(tuple(row['nzo_id'] for row in batch))
                except DownloadFailure as exc:
                    for row in batch:
                        store.failure(row['id'], exc.code)
                    break  # One unavailable client is not queried once per job.
                for row in batch:
                    store.observe(row['id'], observed[row['nzo_id']])
                    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_provenance'").fetchone():
                        current = store.get(row['id'])
                        state = current['state']
                        if state in ('completed', 'failed'):
                            store.db.execute("""UPDATE acquisition_provenance SET state=?,updated_at=?
                                WHERE client_job=? AND state IN ('selected','grabbed')""",
                                ('downloaded' if state == 'completed' else 'failed', time(), row['id']))


class SABRuntime:
    """One owned observation worker, never a submission/search worker."""
    def __init__(self, database, interval=30):
        if interval < 30:
            raise ValueError('SAB polling interval must be at least 30 seconds')
        self.database, self.interval = database, interval
        self.stop_event = Event()
        self.thread = None

    def tick(self):
        store = DownloadStore(self.database)
        try:
            from backend.implementations.managed_clients import client_for
            configs = load_clients(store.db)
            from backend.features.torrent_lifecycle import recover_submissions
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_torrents'").fetchone():
                with download_gate(store.path):
                    store.recover()
                    recover_submissions(store, configs, client_for)
            poll_downloads(store, configs, client_factory=client_for, cancelled=self.stop_event.is_set)
            from backend.features.torrent_lifecycle import (automatic_cleanup,
                                                            observe_torrents)
            observe_torrents(store, configs, client_for)
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_torrents'").fetchone():
                automatic_cleanup(store, configs, client_for)
        finally:
            store.close()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except DownloadFailure as exc:
                if exc.code != E.BUSY:
                    LOGGER.warning('SAB observation unavailable: %s', exc.code.value)
            except Exception:
                LOGGER.error('SAB observation failed; retained durable receipts')
            self.stop_event.wait(self.interval)

    def start(self):
        if self.thread is not None:
            raise RuntimeError('SAB observation worker already started')
        self.thread = Thread(target=self._run, name='SABObservation', daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                LOGGER.warning('SAB observation awaiting bounded network call during shutdown')

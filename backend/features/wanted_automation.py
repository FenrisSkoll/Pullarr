"""Wanted policy orchestration. Existing Phase 5 grabs and Phase 6 own effects."""

import json
from dataclasses import replace
from time import time

from backend.base.download_job import DownloadErrorCode, DownloadFailure
from backend.base.release_candidate import AcquisitionMechanism
from backend.base.release_evaluation import (Compatibility,
                                             CoverageBand, TargetKind)
from backend.base.release_search import SourceFailure
from backend.features.sab_downloads import create_grab_intent, submit_selected
from backend.features.wanted_search import UNIFIED_SEARCH
from backend.implementations.auto_selection import select_automatically
from backend.implementations.direct_download_source import DDLError
from backend.implementations.managed_clients import client_for
from backend.implementations.organization_filesystem import execution_gate
from backend.implementations.release_scoring import (evaluate_release,
                                                     evaluate_releases)
from backend.implementations.sabnzbd import SABClient
from backend.internals.download_jobs import DownloadStore
from backend.internals.managed_clients import load_managed_clients
from backend.internals.provider_identity import MetadataIdentityError
from backend.internals.sab_clients import load_sab_clients
from backend.internals.wanted import WantedConflict, WantedStore
from backend.internals.wanted_configuration import load_automation


class WantedAutomation:
    def __init__(self, database, *, searches=None, clock=time,
                 client_factory=client_for, checkpoint=lambda stage: None):
        self.store = WantedStore(database, clock=clock)
        self.searches, self.client_factory, self.checkpoint = searches or UNIFIED_SEARCH, client_factory, checkpoint

    def close(self):
        self.store.close()

    def protocol_client(self, protocol):
        config = load_automation(self.store.db)
        if protocol == 'nzb' and config['sab_client_id']:
            choices = [c for c in load_sab_clients(self.store.db) if c.enabled and c.key == config['sab_client_id']]
        else:
            choices = [c for c in load_managed_clients(self.store.db) if c.enabled and c.protocol == protocol]
        return self.client_factory(choices[0]) if len(choices) == 1 else None

    @staticmethod
    def failure_code(error):
        if isinstance(error, DDLError):
            return error.code
        if isinstance(error, (DownloadFailure, SourceFailure)):
            return error.code.value
        return str(error)  # WantedConflict contains only internal constant IDs.

    def unavailable(self, session, *, check_client=False):
        blocks = {(r[0], r[1], r[2]) for r in self.store.db.execute(
            '''SELECT source_kind,source_key,candidate_id FROM wanted_blocks
               WHERE candidate_id IN (SELECT value FROM json_each(?))''',
            (json.dumps([e.candidate.candidate_id for e in session.evaluations]),))}
        blocked = {e.candidate.candidate_id for e in session.evaluations if
            (e.candidate.source.kind.value, e.candidate.source.key, e.candidate.candidate_id) in blocks}
        if session.ddl_search:
            links = session.ddl.block_loader()
            ddl = session.ddl.sessions[session.ddl_search]
            blocked.update(s.evaluation.candidate.candidate_id for s in ddl.selections.values() if s.raw['link'] in links)
        config = load_automation(self.store.db)
        try:
            client = self.protocol_client('nzb')
            torrent_client = self.protocol_client('torrent')
        except DownloadFailure:
            client, torrent_client = None, None
        if client and check_client:
            try:
                client.check()
            except DownloadFailure:
                client = None
        if torrent_client and check_client:
            try:
                torrent_client.check()
            except DownloadFailure:
                torrent_client = None
        unavailable = set(blocked)
        if self.store.has_quality and session.policy.quality_context:
            from hashlib import sha256

            from backend.internals.quality import canonical
            context = json.loads(session.policy.quality_context)
            profile = context['profile']
            rejected = {r[0] for r in self.store.db.execute('''SELECT candidate_key FROM quality_rejections
                WHERE issue_id IN (SELECT value FROM json_each(?)) AND profile_id=? AND profile_revision=?''',
                (json.dumps(session.target.issue_ids), profile['id'], profile['revision']))}
            for e in session.evaluations:
                from backend.base.release_candidate import acquisition_identity
                key = acquisition_identity(e.candidate)
                if key in rejected:
                    unavailable.add(e.candidate.candidate_id)
        unavailable.update(e.candidate.candidate_id for e in session.evaluations
                           if e.candidate.acquisition.mechanism not in (AcquisitionMechanism.NZB, AcquisitionMechanism.TORRENT, AcquisitionMechanism.DIRECT_DOWNLOAD))
        if torrent_client is None:
            unavailable.update(e.candidate.candidate_id for e in session.evaluations
                               if e.candidate.acquisition.mechanism == AcquisitionMechanism.TORRENT)
        if client is None:
            unavailable.update(e.candidate.candidate_id for e in session.evaluations
                               if e.candidate.acquisition.mechanism == AcquisitionMechanism.NZB)
        return frozenset(unavailable), frozenset(blocked), client

    def reconcile(self):
        self.store.reconcile_ownership()
        # Compact downstream facts; never poll remote services from this method.
        rows = self.store.db.execute('''WITH batch AS (
            SELECT id,state FROM wanted_decisions WHERE state IN ('tracking','review') ORDER BY updated_at,id LIMIT 1000)
            SELECT b.id,b.state,a.kind,t.state intake_state,s.state acquisition_state,q.id queue_id
            FROM batch b LEFT JOIN wanted_acquisitions a ON a.decision_id=b.id
            LEFT JOIN acquisition_intakes t ON t.kind=a.kind AND t.download_id=a.acquisition_id
            LEFT JOIN acquisition_downloads s ON a.kind IN ('sabnzbd','nzbget','qbittorrent') AND s.id=a.acquisition_id
            LEFT JOIN download_queue q ON a.kind='direct_download' AND a.acquisition_id=
                CASE WHEN json_valid(q.covered_issues) THEN json_extract(q.covered_issues,'$.completion_id') END''').fetchall()
        reasons = {}
        for row in rows:
            reason = None
            if row['intake_state'] in ('review', 'blocked', 'partial', 'failed', 'completed'):
                reason = 'intake_' + row['intake_state']
            elif row['intake_state']:
                continue
            elif row['kind'] in ('sabnzbd','nzbget','qbittorrent') and row['acquisition_state'] in (None, 'ambiguous', 'remote_unknown', 'failed'):
                reason = 'acquisition_' + (row['acquisition_state'] or 'missing')
            elif row['kind'] == 'direct_download' and row['queue_id'] is None:
                reason = 'ddl_dispatch_unavailable'
            if reason and row['state'] == 'tracking':
                reasons[row['id']] = reason
        with self.store.transaction():
            self.store.db.executemany("UPDATE wanted_decisions SET state='review',error=? WHERE id=? AND state='tracking'",
                                     ((reason, identifier) for identifier, reason in reasons.items()))
            # Fair bounded reconciliation without an append-only per-poll log.
            self.store.db.executemany('UPDATE wanted_decisions SET updated_at=? WHERE id=?',
                                     ((self.store.clock(), identifier) for identifier in {r['id'] for r in rows}))

    def search_manual(self, volume_id, issue_id=None):
        from backend.features.quality import search_quality_context
        identifier, session = self.searches.search(volume_id, issue_id,
            quality_context=search_quality_context(volume_id,issue_id,self.store.db.cursor()))
        session.run_id = self.store.begin_search(session.target, 'manual')
        unavailable, blocked, _ = self.unavailable(session)
        self.store.finish_search(session.run_id, 'manual_results', sources=session.source_receipts,
                                 counts=self.counts(session), cooldown=False)
        preview = self.searches.preview(identifier, unavailable=unavailable, blocked=blocked)
        choice = select_automatically(session.evaluations, search_state=session.state, unavailable=unavailable)
        preview['automatic_policy'] = {'outcome': choice.reason.value,
                                       'fingerprint': choice.policy_fingerprint}
        return preview

    @staticmethod
    def counts(session):
        return {state.value: sum(e.state == state for e in session.evaluations) for state in Compatibility}

    def source_backoff(self, session):
        delays = []
        for source in session.source_receipts['nzb']:
            if any(code in ('rate_limited', 'timeout', 'unavailable', 'http_failure', 'authentication') for code in source['errors']):
                delays.append((source['source'], self.store.clock() + max(900, min(source.get('retry_after', 0), 86400)), source['errors'][0]))
        if session.source_receipts['ddl']:
            for error in session.source_receipts['ddl']['errors']:
                if error['source'] in session.ddl_configuration and error['code'].startswith('source_'):
                    config = session.ddl_configuration[error['source']]
                    delays.append((f'ddl:{config.id}:{config.identity}', self.store.clock() + 900, error['code']))
        with self.store.transaction():
            self.store.db.executemany('''INSERT INTO wanted_source_retry VALUES(?,?,?) ON CONFLICT(source_key)
                DO UPDATE SET next_request=MAX(next_request,excluded.next_request),reason=excluded.reason''', delays)

    def sources_paused(self):
        rows = self.store.db.execute('SELECT source_key FROM wanted_source_retry WHERE next_request>?', (self.store.clock(),)).fetchall()
        if not rows:
            return False
        nzb = tuple(c.namespace for c in self.searches.nzb_loader() if c.enabled)
        ddl = {f'ddl:{c.id}:{c.identity}' for c in self.searches.ddl_loader().values()}
        return any(r[0] in ddl or any(r[0] == key or r[0].startswith(key + '-indexer-') for key in nzb) for r in rows)

    def covered_ids(self, session, evaluation, unavailable, *, automatic=True):
        """Only existing local IDs independently proved by unchanged Phase 5B.

        Additional members are reserved only if this same release is uniquely best
        for them in this result set. No new numeric interval/greedy quality rule.
        """
        proven = set(session.target.issue_ids)
        if session.target.kind != TargetKind.ISSUES or evaluation.band == CoverageBand.EXACT:
            return tuple(sorted(proven))
        members = self.store.missing_members(session.target.publication.id)
        for issue in session.target.catalog:
            if issue.id not in members or issue.id in proven:
                continue
            # First reject nonmembers with the existing evaluator before ranking
            # alternatives. This is an evidence filter, not a coverage parser.
            target = replace(session.target, issue_ids=(issue.id,))
            if evaluate_release(target, evaluation.candidate, session.policy).state != Compatibility.COMPATIBLE:
                continue
            if members[issue.id]:
                raise WantedConflict('coverage_reserved')
            if not automatic:
                # Explicit selection chooses the release, but still claims every
                # proven missing member so background work cannot overlap it.
                proven.add(issue.id)
                continue
            decision = select_automatically(evaluate_releases(target,
                (e.candidate for e in session.evaluations), session.policy),
                search_state=session.state, unavailable=unavailable)
            if decision.selected and decision.selected.candidate.candidate_id == evaluation.candidate.candidate_id:
                proven.add(issue.id)
            else:
                # Never under-reserve a physically covering release to work
                # around another target's better/tied result. Abstain instead.
                raise WantedConflict('coverage_preference_conflict')
        return tuple(sorted(proven))

    def grab(self, session, evaluation, *, automatic, force=False, offering_id=None, cancelled=lambda: False):
        key = evaluation.candidate.candidate_id
        if key in session.receipts:
            return session.receipts[key]
        self.searches.revalidate(session)
        if evaluation.quality_receipt and json.loads(evaluation.quality_receipt)['result'] in ('not_allowed','equal','downgrade'):
            raise WantedConflict('quality_not_allowed')
        original_configuration = load_automation(self.store.db)
        unavailable, blocked, client = self.unavailable(session, check_client=automatic)
        if evaluation.candidate.candidate_id in unavailable and not (force and evaluation.candidate.candidate_id in session.ddl_ids):
            raise WantedConflict('operationally_unavailable')
        if not force and evaluation.state != Compatibility.COMPATIBLE:
            raise WantedConflict('incompatible_selection')
        if automatic and force:
            raise WantedConflict('automatic_force_forbidden')
        if force and evaluation.candidate.candidate_id not in session.ddl_ids:
            raise WantedConflict('force_unavailable')
        ids = (session.target.issue_ids if force else
               self.covered_ids(session, evaluation, unavailable, automatic=automatic))
        decision = session.decisions.get(key)
        if decision:
            row = self.store.db.execute('SELECT * FROM wanted_decisions WHERE id=?', (decision,)).fetchone()
            if automatic or offering_id is None or row['state'] != 'review' or row['error'] != 'offering_selection_required':
                raise WantedConflict('selection_already_claimed')
        else:
            decision = self.store.reserve(session.run_id, evaluation,
                authorization='automatic' if automatic else 'forced_manual' if force else 'manual', ids=ids)
            session.decisions[key] = decision
        if self.store.has_quality:
            from backend.internals.quality import QualityStore
            quality_store = QualityStore(self.store.db.cursor())
            reason = self.store.db.execute('SELECT acquisition_reason FROM wanted_decisions WHERE id=?', (decision,)).fetchone()[0]
            if ids:
                quality_store.selected(ids[0], evaluation.candidate, reason=reason, identifier=decision,
                    decision=dict(authorization='automatic' if automatic else 'manual', score=evaluation.score,
                        issue_ids=list(ids),
                        components=[dict(axis=c.axis,rule=c.rule.value,outcome=c.outcome.value,points=c.points,
                                         gate=c.gate.value if c.gate else None) for c in evaluation.components],
                        source_name=evaluation.candidate.source.name[:200],
                        protocol=evaluation.candidate.acquisition.mechanism.value,
                        reported_size=evaluation.candidate.size_bytes,
                        scoring_fingerprint=evaluation.policy_fingerprint,
                        source_priority=evaluation.source_priority, quality=json.loads(evaluation.quality_receipt) if evaluation.quality_receipt else None,
                        discovery=session.source_receipts.get('discovery')))
                if len(ids) != 1:
                    self.store.db.execute('UPDATE acquisition_provenance SET issue_id=NULL WHERE id=?', (decision,))
        self.checkpoint('selection_persisted')
        # Policy/configuration, source/target, block and monitoring races are checked
        # after the durable receipt and immediately before starting the grab.
        def validate_before_effect():
            if cancelled():
                raise WantedConflict('cancelled')
            self.searches.revalidate(session)
            from backend.features.quality import search_quality_context
            if search_quality_context(session.target.publication.id, session.issue_id, self.store.db.cursor()) != session.policy.quality_context:
                raise WantedConflict('policy_changed')
            if load_automation(self.store.db) != original_configuration:
                raise WantedConflict('policy_changed')
            if automatic:
                slots = ','.join('?' for _ in ids)
                upgrade = 'OR i.id IN (SELECT issue_id FROM quality_upgrade_issues)' if self.store.has_quality else ''
                count = self.store.db.execute(f'''SELECT COUNT(*) FROM issues i JOIN volumes v ON v.id=i.volume_id
                    WHERE i.id IN ({slots}) AND i.monitored=1 AND v.monitored=1
                    AND (NOT EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id) {upgrade})''', ids).fetchone()[0]
                if count != len(ids):
                    raise WantedConflict('target_changed')
            _, latest_blocks, _ = self.unavailable(session)
            if evaluation.candidate.candidate_id in latest_blocks and not force:
                raise WantedConflict('blocked')
        try:
            validate_before_effect()
        except (DDLError, WantedConflict):
            self.store.transition(decision, 'abandoned', error='stale_selection')
            raise
        self.store.transition(decision, 'grabbing')
        self.checkpoint('grab_started')
        try:
            mechanism = evaluation.candidate.acquisition.mechanism
            if mechanism in (AcquisitionMechanism.NZB, AcquisitionMechanism.TORRENT):
                client = self.protocol_client(mechanism.value)
                if force or client is None:
                    raise WantedConflict('nzb_client_unavailable')
                target = replace(session.target, issue_ids=ids)
                grab_evaluation = evaluate_release(target, evaluation.candidate, session.policy) if ids != session.target.issue_ids else evaluation
                intent = create_grab_intent(evaluation.candidate, grab_evaluation, target,
                                           session.policy, client.config, request_id=decision)
                with self.store.transaction():
                    self.store.db.execute("INSERT INTO wanted_acquisitions VALUES(?,?,?)", (decision, getattr(client.config, 'kind', 'sabnzbd'), decision))
                downloads = DownloadStore(self.store.path)
                try:
                    source = self.searches.nzb_session(session, evaluation,
                        load_automation(self.store.db)['redirect_origins'])
                    def before_submission(stage):
                        if stage == 'resolved':
                            self.checkpoint('nzb_resolved')
                            try:
                                validate_before_effect()
                            except (DDLError, WantedConflict):
                                raise DownloadFailure(DownloadErrorCode.SELECTION) from None
                    result = submit_selected(downloads, intent, evaluation.candidate, grab_evaluation,
                                             target, session.policy, source, client, checkpoint=before_submission)
                finally:
                    downloads.close()
                acquisition_id = decision
            elif mechanism == AcquisitionMechanism.DIRECT_DOWNLOAD:
                selected = session.ddl.sessions[session.ddl_search].selections[session.ddl_ids[evaluation.candidate.candidate_id]]
                selected.authorization = {'automation_decision_id': decision,
                    'authorization': 'automatic' if automatic else 'forced_manual' if force else 'manual'}
                selected.before_dispatch = validate_before_effect
                original_dispatch = session.ddl.dispatch
                def dispatch(selection, offering, group, issue_id, forced, mirrors):
                    validate_before_effect()
                    expanded = replace(offering.target, issue_ids=ids)
                    observed = evaluate_release(expanded, offering.candidate, session.policy)
                    quality_receipt = json.loads(observed.quality_receipt) if observed.quality_receipt else None
                    if quality_receipt and quality_receipt['result'] in ('not_allowed', 'equal', 'downgrade'):
                        raise DDLError('quality_not_allowed')
                    if not forced and observed.state != Compatibility.COMPATIBLE:
                        raise DDLError('offering_coverage_changed')
                    if self.store.has_quality and quality_receipt:
                        # The actual offering, not its listing page, is what is
                        # selected for dispatch. All of these facts are still
                        # pre-download; retain the earlier listing separately.
                        from backend.internals.quality import canonical
                        row = self.store.db.execute('SELECT decision,release_title FROM acquisition_provenance WHERE id=?', (decision,)).fetchone()
                        if row:
                            snapshot = json.loads(row[0])
                            snapshot.update(listing_title=row[1],quality=quality_receipt,score=observed.score,
                                components=[dict(axis=c.axis,rule=c.rule.value,outcome=c.outcome.value,points=c.points,
                                                 gate=c.gate.value if c.gate else None) for c in observed.components])
                            self.store.db.execute('''UPDATE acquisition_provenance SET release_title=?,claims=?,decision=?,updated_at=?
                                WHERE id=? AND state IN ('selected','failed')''',
                                (offering.candidate.raw_title,canonical(quality_receipt['claims']),canonical(snapshot),self.store.clock(),decision))
                    return original_dispatch(selection, observed, group, issue_id, forced, mirrors)
                session.ddl.dispatch = dispatch
                try:
                    result = session.ddl.select(session.ddl_search, session.ddl_ids[evaluation.candidate.candidate_id],
                                                force=force, offering_id=offering_id)
                finally:
                    session.ddl.dispatch = original_dispatch
                if result.get('state') != 'dispatched':
                    self.store.transition(decision, 'review', error='offering_selection_required')
                    return dict(result, decision_id=decision)
                if not self.store.db.execute('SELECT 1 FROM wanted_acquisitions WHERE decision_id=?', (decision,)).fetchone():
                    raise WantedConflict('dispatch_not_correlated')
                acquisition_id = None  # Each queue member has its exact completion correlation.
            else:
                raise WantedConflict('unsupported_mechanism')
        except (DDLError, DownloadFailure, WantedConflict, SourceFailure) as error:
            self.store.transition(decision, 'review', error=self.failure_code(error))
            if self.store.has_quality:
                self.store.db.execute("UPDATE acquisition_provenance SET state='failed',error=?,updated_at=? WHERE id=?",
                    (self.failure_code(error),self.store.clock(),decision))
            raise
        self.checkpoint('acquisition_persisted')
        if self.store.has_quality:
            link = self.store.db.execute('SELECT kind,acquisition_id FROM wanted_acquisitions WHERE decision_id=? LIMIT 1', (decision,)).fetchone()
            if link:
                self.store.db.execute("UPDATE acquisition_provenance SET state='grabbed',client_kind=?,client_job=?,updated_at=? WHERE id=?",
                    (link[0],link[1],self.store.clock(),decision))
        self.store.transition(decision, 'tracking', acquisition_id=acquisition_id)
        receipt = {'state': 'tracking', 'decision_id': decision, 'download': result}
        session.receipts[key] = receipt
        return receipt

    def run_target(self, volume_id, issue_id, *, trigger='automatic_missing', allow_grab=False,
                   cancelled=lambda: False):
        if not self.store.eligible((issue_id,)):
            return {'state': 'not_wanted_or_reserved'}
        try:
            target = self.searches.target_loader(volume_id, issue_id)
        except (DDLError, MetadataIdentityError):
            identifier = self.store.unavailable_target(volume_id, issue_id, trigger)
            return {'state': 'target_unavailable', 'run_id': identifier}
        original_configuration = load_automation(self.store.db)
        run_id = self.store.begin_search(target, trigger)
        self.checkpoint('search_claimed')
        identifier = None
        search_finished = False
        try:
            from backend.features.quality import search_quality_context
            identifier, session = self.searches.search(volume_id, issue_id, target=target, cancelled=cancelled,
                quality_context=search_quality_context(volume_id,issue_id,self.store.db.cursor()))
            session.run_id = run_id
            self.source_backoff(session)
            unavailable, _, _ = self.unavailable(session, check_client=allow_grab)
            choice = select_automatically(session.evaluations, search_state=session.state, unavailable=unavailable)
            self.store.finish_search(run_id, choice.reason.value, sources=session.source_receipts, counts=self.counts(session))
            search_finished = True
            if not cancelled() and allow_grab and choice.selected:
                if load_automation(self.store.db) != original_configuration:
                    raise WantedConflict('policy_changed')
                return self.grab(session, choice.selected, automatic=True, cancelled=cancelled)
            return {'state': choice.reason.value, 'run_id': run_id}
        except (DDLError, DownloadFailure, WantedConflict, SourceFailure) as error:
            if not search_finished:
                self.store.finish_search(run_id, 'operational_failure')
            # A later grab failure does not rewrite the source/scoring receipt.
            with self.store.transaction():
                self.store.db.execute('UPDATE wanted_searches SET error=? WHERE id=?', (self.failure_code(error), run_id))
            return {'state': 'operational_failure', 'run_id': run_id}
        finally:
            if identifier:
                self.searches.close(identifier)

    def tick(self, *, cancelled=lambda: False):
        with execution_gate(self.store.path + '.wanted'):
            self.store.recover_claims()
            self.reconcile()
            self.store.prune_search_history()
            configuration = load_automation(self.store.db)
            # Complete-search v1 cannot choose while an enabled participant is
            # cooling down. Pause the cohort rather than hammer another target.
            if self.sources_paused():
                return
            for row in self.store.due(limit=1, requested_only=configuration['mode'] == 'off'):
                if cancelled():
                    break
                self.run_target(row['volume_id'], row['id'], trigger='incremental_discovery' if row['requested'] == 2 else 'retry' if row['requested'] else 'automatic_missing',
                    allow_grab=row['requested'] == 1 or configuration['mode'] == 'grab', cancelled=cancelled)
                # One target per tick bounds the complete source request budget.
                break


def request_wanted_search(volume_id, issue_id=None):
    from backend.internals.db import DBConnection

    store = WantedStore(DBConnection.default_file)
    try:
        return store.request_search(volume_id, issue_id)
    finally:
        store.close()


def run_wanted_discovery():
    from backend.features.wanted_discovery import discover_wanted
    from backend.internals.db import DBConnection

    store = WantedStore(DBConnection.default_file)
    try:
        with execution_gate(store.path + '.wanted'):
            if load_automation(store.db)['mode'] == 'off':
                return {'state': 'disabled'}
            return discover_wanted(store)
    finally:
        store.close()

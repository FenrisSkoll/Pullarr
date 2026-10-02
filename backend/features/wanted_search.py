"""One retained manual/automatic source scope over the existing Phase 5 services."""

import json
from contextlib import ExitStack
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from uuid import uuid4

from backend.base.release_evaluation import ScoringPolicy, SourcePriority
from backend.base.release_search import (QUERY_POLICY, SearchDiagnostic,
                                         SearchState, SourceFailure,
                                         SourceSearchResult)
from backend.features.direct_downloads import (ManualDDL, configured_sources,
                                               force_available, load_target)
from backend.implementations.direct_download_source import DDLError
from backend.implementations.nzb_resolution import SelectedSourceSession
from backend.implementations.release_explanations import (explain_release,
                                                          preview_explanation)
from backend.implementations.release_scoring import rank_evaluations
from backend.implementations.release_search import (evaluate_search,
                                                    retained_search_sources)
from backend.internals.release_sources import load_sources


@dataclass
class UnifiedSearch:
    target: object
    issue_id: object
    policy: ScoringPolicy
    state: SearchState
    evaluations: tuple
    source_receipts: dict
    expires: float
    resources: ExitStack = field(repr=False)
    nzb_sources: dict = field(repr=False)
    ddl: object = field(repr=False)
    ddl_search: object = field(repr=False)
    ddl_ids: dict = field(repr=False)
    source_configuration: tuple = field(repr=False)
    ddl_configuration: dict = field(repr=False)
    selections: dict = field(default_factory=dict)
    run_id: object = None
    decisions: dict = field(default_factory=dict)
    receipts: dict = field(default_factory=dict)


class UnifiedReleaseSearch:
    def __init__(self, *, clock=monotonic, target_loader=load_target,
                 nzb_loader=load_sources, ddl_loader=configured_sources,
                 ddl_factory=ManualDDL, source_scope=retained_search_sources):
        self.clock, self.target_loader = clock, target_loader
        self.nzb_loader, self.ddl_loader = nzb_loader, ddl_loader
        self.ddl_factory, self.source_scope = ddl_factory, source_scope
        self.sessions = {}
        self.lock = RLock()

    def close(self, identifier):
        with self.lock:
            session = self.sessions.pop(identifier, None)
            if session:
                session.resources.close()

    def close_all(self):
        for identifier in tuple(self.sessions):
            self.close(identifier)

    def _prune(self):
        for identifier, session in tuple(self.sessions.items()):
            if session.expires <= self.clock():
                self.close(identifier)

    def search(self, volume_id, issue_id=None, *, target=None, cancelled=lambda: False, quality_context=None):
        target = target or self.target_loader(volume_id, issue_id)
        configs, ddl_configs = tuple(self.nzb_loader()), self.ddl_loader()
        resources = ExitStack()
        try:
            sources, reports, diagnostics = [], [], []
            configuration_fingerprint = None
            if any(c.enabled for c in configs):
                try:
                    sources, reports, diagnostics, configuration_fingerprint = resources.enter_context(self.source_scope(
                        target, configs, cancelled=cancelled))
                except SourceFailure as error:
                    reports = [SourceSearchResult(c.source(), SearchState.FAILED,
                        diagnostics=(SearchDiagnostic(c.namespace, error.code, retry_after=error.retry_after),))
                        for c in configs if c.enabled]
            from backend.features.quality import search_quality_context
            if quality_context is None:
                quality_context = search_quality_context(volume_id, issue_id) if self.target_loader is load_target else ''
            policy = ScoringPolicy(quality_context=quality_context,
                reject_owned=not (quality_context and json.loads(quality_context)['upgrade']),
                source_priorities=tuple(sorted((
                SourcePriority(s.source.kind, s.source.key, s.priority) for s in sources),
                key=lambda p: (p.kind.value, p.key))))
            nzb = evaluate_search(target, sources, policy=policy, initial=reports, diagnostics=diagnostics)
            ddl = self.ddl_factory(target_loader=self.target_loader, sources_loader=self.ddl_loader,
                                   policy_loader=lambda: policy)
            ddl_result = ddl.search(volume_id, issue_id, target=target, cancelled=cancelled) if ddl_configs and not cancelled() else None
            ddl_session = ddl.sessions[ddl_result['search_id']] if ddl_result else None
            ddl_ids = {s.evaluation.candidate.candidate_id: key for key, s in ddl_session.selections.items()} if ddl_session else {}
            evaluations = rank_evaluations((*nzb.evaluations,
                *(s.evaluation for s in ddl_session.selections.values()))) if ddl_session else nzb.evaluations
            states = ([nzb.state] if any(c.enabled for c in configs) else [])
            if ddl_result:
                states.append(SearchState(ddl_result['state']))
            state = (SearchState.COMPLETE if states and all(s == SearchState.COMPLETE for s in states)
                     else SearchState.PARTIAL if any(s != SearchState.FAILED for s in states)
                     else SearchState.FAILED)
            if cancelled():
                state = SearchState.PARTIAL
            receipt = {'nzb': [{'source': r.source.key, 'state': r.state.value,
                'errors': [d.code.value for d in r.diagnostics],
                'retry_after': max((d.retry_after or 0 for d in r.diagnostics), default=0)} for r in nzb.sources],
                'ddl': {'state': ddl_result['state'], 'errors': ddl_result['errors']} if ddl_result else None,
                'query_policy': QUERY_POLICY, 'scoring_fingerprint': policy.fingerprint,
                'errors': [] if states else ['no_enabled_sources'],
                'nzb_configuration_fingerprint': configuration_fingerprint,
                'ddl_configuration_fingerprints': sorted(c.identity for c in ddl_configs.values())}
            identifier = uuid4().hex
            session = UnifiedSearch(target, issue_id, policy, state, evaluations, receipt,
                self.clock() + 900, resources, {s.source.key: s for s in sources}, ddl,
                ddl_result['search_id'] if ddl_result else None, ddl_ids, configs, ddl_configs,
                {uuid4().hex: e for e in evaluations})
            with self.lock:
                self._prune()
                while len(self.sessions) >= 16:
                    self.close(next(iter(self.sessions)))
                self.sessions[identifier] = session
            return identifier, session
        except BaseException:
            resources.close()
            raise

    def lookup(self, identifier, selection_id=None):
        with self.lock:
            self._prune()
            session = self.sessions.get(identifier)
            if session is None or (selection_id is not None and selection_id not in session.selections):
                raise DDLError('selection_expired')
            return session

    def observe_ddl(self, volume_id, issue_id, raw, source, *, quality_context, evidence):
        """Server-owned Discover observation enters the SAME retained search scope.

        No network search, reservation or grab. Candidate identity is the normal
        DDL adapter identity. Existing select/revalidation/dispatch own effects.
        """
        from backend.features.direct_downloads import SearchSession, Selection
        from backend.implementations.release_candidates import adapt_ddl_result
        from backend.implementations.release_explanations import \
            evaluation_identity
        from backend.implementations.release_scoring import evaluate_release

        target = self.target_loader(volume_id, issue_id)
        configs, ddl_configs = tuple(self.nzb_loader()), self.ddl_loader()
        if source.config.id not in ddl_configs or source.config != ddl_configs[source.config.id]:
            raise DDLError('source_changed')
        policy = ScoringPolicy(quality_context=quality_context,
            reject_owned=not json.loads(quality_context)['upgrade'])
        candidate = adapt_ddl_result(raw)
        evaluation = evaluate_release(target, candidate, policy)
        ddl = self.ddl_factory(target_loader=self.target_loader, sources_loader=self.ddl_loader, policy_loader=lambda: policy)
        search_id, selection_id = uuid4().hex, uuid4().hex
        selection = Selection(source, raw, evaluation, evaluation_identity(evaluation))
        ddl.sessions[search_id] = SearchSession(target, issue_id, policy, self.clock()+900, {selection_id: selection})
        receipt = dict(nzb=[], ddl=dict(state='complete', errors=[]), errors=[], discovery=evidence)
        session = UnifiedSearch(target, issue_id, policy, SearchState.COMPLETE, (evaluation,), receipt,
            self.clock()+900, ExitStack(), {}, ddl, search_id, {candidate.candidate_id: selection_id}, configs, ddl_configs,
            {selection_id: evaluation})
        with self.lock:
            self._prune()
            if len(self.sessions) >= 16:
                raise DDLError('selection_capacity')
            self.sessions[search_id] = session
        return search_id, session, selection

    def revalidate(self, session):
        if session.expires <= self.clock():
            raise DDLError('selection_expired')
        if (tuple(self.nzb_loader()) != session.source_configuration
                or self.ddl_loader() != session.ddl_configuration):
            raise DDLError('source_changed')
        if self.target_loader(session.target.publication.id, session.issue_id) != session.target:
            raise DDLError('target_changed')

    def nzb_session(self, session, evaluation, origins=()):
        source = session.nzb_sources.get(evaluation.candidate.source.key)
        if source is None:
            raise DDLError('source_changed')
        selected = SelectedSourceSession(source, redirect_origins=origins)
        selected.check(evaluation.candidate)
        return selected

    def preview(self, identifier, *, unavailable=frozenset(), blocked=frozenset()):
        session = self.lookup(identifier)
        return {'search_id': identifier, 'state': session.state.value,
                'errors': [dict(source=r['source'], code=code) for r in session.source_receipts['nzb'] for code in r['errors']]
                    + (session.source_receipts['ddl']['errors'] if session.source_receipts['ddl'] else [])
                    + [dict(source=None, code=code) for code in session.source_receipts['errors']],
                'sources': session.source_receipts, 'expires_in': max(0, int(session.expires - self.clock())),
                'results': [{'selection_id': key, 'explanation': preview_explanation(explain_release(e)),
                    'quality': json.loads(e.quality_receipt) if e.quality_receipt else None,
                    'mechanism': e.candidate.acquisition.mechanism.value,
                    'blocked': e.candidate.candidate_id in blocked,
                    'operationally_available': e.candidate.candidate_id not in unavailable,
                    'download_eligible': e.state.value == 'compatible' and e.candidate.candidate_id not in unavailable,
                    'force_eligible': e.candidate.candidate_id in session.ddl_ids and force_available(e)
                        and (not e.quality_receipt or json.loads(e.quality_receipt)['result'] not in ('not_allowed','equal','downgrade'))}
                    for key, e in session.selections.items()]}


UNIFIED_SEARCH = UnifiedReleaseSearch()

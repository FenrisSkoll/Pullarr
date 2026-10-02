"""Common manual source scope and exact server-only selections."""

import json
from contextlib import contextmanager
from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock

from Tbackend.features import (direct_downloads as ddl,
                               release_scoring as facts, release_search as nzb)

from backend.features.direct_downloads import ManualDDL
from backend.features.wanted_search import UnifiedReleaseSearch
from backend.implementations.direct_download_source import (DDLError,
                                                            GetComicsSource)
from backend.implementations.release_search import retained_search_sources


class UnifiedScopeTests(TestCase):
    def setUp(self):
        self.http = ddl.FixtureHTTP()
        self.transport = nzb.FixtureTransport()
        self.target = facts.target()
        self.dispatch = Mock()
        self.now = 0
        self.configs = (nzb.config(),)
        @contextmanager
        def scope(target, configs, **kwargs):
            with retained_search_sources(target, configs, transport=self.transport, **kwargs) as resources:
                yield resources
        self.search = UnifiedReleaseSearch(clock=lambda: self.now, target_loader=lambda *_: self.target,
            nzb_loader=lambda: self.configs, ddl_loader=lambda: {1: ddl.config()}, source_scope=scope,
            ddl_factory=lambda **kw: ManualDDL(**kw, block_loader=lambda: set(), dispatch=self.dispatch,
                source_factory=lambda c: GetComicsSource(c, self.http)))
        self.addCleanup(self.search.close_all)

    def test_two_mechanisms_share_policy_receipts_and_never_pre_resolve(self):
        identifier, session = self.search.search(1, 5)
        dto = self.search.preview(identifier)
        self.assertEqual({r['mechanism'] for r in dto['results']}, {'nzb', 'direct_download'})
        self.assertEqual(len({e.policy_fingerprint for e in session.evaluations}), 1)
        self.assertTrue(all('/page/' in url for url in self.http.calls))
        self.dispatch.assert_not_called()
        self.assertNotIn('https://', json.dumps(dto))
        self.assertNotIn(nzb.SECRET, json.dumps(dto))

    def test_selections_are_operation_bound_and_expire(self):
        first, a = self.search.search(1, 5)
        second, b = self.search.search(1, 5)
        with self.assertRaises(DDLError):
            self.search.lookup(second, next(iter(a.selections)))
        self.now = 901
        with self.assertRaises(DDLError):
            self.search.lookup(first)

    def test_source_or_target_change_invalidates_before_resolution(self):
        identifier, session = self.search.search(1, 5)
        self.configs = ()
        with self.assertRaises(DDLError):
            self.search.revalidate(session)
        self.dispatch.assert_not_called()

    def test_session_memory_cap(self):
        for _ in range(20):
            self.search.search(1, 5)
        self.assertEqual(len(self.search.sessions), 16)

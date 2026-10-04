"""Collected edition matching and strict automatic/manual source separation."""
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from Tbackend.features.release_scoring import candidate, target

from backend.base.release_candidate import (AcquisitionMechanism,
                                            AcquisitionReference, LocatorKind)
from backend.base.release_evaluation import Compatibility
from backend.base.release_search import SearchState
from backend.features.wanted_automation import WantedAutomation
from backend.implementations.auto_selection import select_automatically
from backend.implementations.identification import title_key
from backend.implementations.release_scoring import evaluate_release
from backend.implementations.release_search import (configured_search,
                                                    plan_queries)


class CollectedReleaseTests(TestCase):
    def setUp(self):
        base = target()
        self.target = replace(base,
            publication=replace(base.publication, title='Batman: Rebirth Deluxe Edition', year=2017),
            catalog=(replace(base.catalog[0], raw_number='1', year=2017, title='Book 1'),))

    def test_book_title_and_punctuation_without_semantic_deletion(self):
        for title in (
            'Batman – Rebirth Deluxe Edition Book 1 (2017)',
            'Batman.Rebirth.Deluxe.Edition.Book.01.2017.digital.Son.of.Ultron-Empire',
            'Batman: Rebirth Deluxe Edition Book 1 (2017)',
        ):
            with self.subTest(title=title):
                evaluation = evaluate_release(self.target, candidate(title))
                self.assertEqual(evaluation.state, Compatibility.COMPATIBLE, evaluation)
        self.assertEqual(title_key('Batman - Rebirth Deluxe Edition'), title_key(self.target.publication.title))
        self.assertNotEqual(title_key('Batman Detective Comics Rebirth Deluxe Edition'), title_key(self.target.publication.title))

    def test_wrong_publication_book_and_known_year_remain_rejected(self):
        for title in (
            'Batman - Detective Comics - Rebirth Deluxe Edition Book 1 (2017)',
            'Superman Rebirth Deluxe Edition Book 1 (2017)',
            'Batman Rebirth Deluxe Edition Book 4 (2017)',
            'Batman Rebirth Deluxe Edition Book 1 (2019)',
        ):
            with self.subTest(title=title):
                self.assertEqual(evaluate_release(self.target, candidate(title)).state, Compatibility.REJECTED)

    def test_issue_title_queries_remain_bounded(self):
        queries = plan_queries(self.target)
        self.assertEqual(len(queries), 3)
        self.assertIn('Book 1 2017', queries[0].query)
        self.assertTrue(queries[1].query.endswith('Book 1'))

    def test_comic_capability_selection_and_conservative_fallback(self):
        from Tbackend.features.release_search import (FixtureTransport,
                                                      config, rss)
        for categories, expected in (
            ('<category id="2000" name="Movies"/><category id="5000" name="TV"/><category id="7000" name="Books"><subcat id="7020" name="EBooks"/><subcat id="7030" name="Comics"/></category>', '7030'),
            ('<category id="2000" name="Movies"/><category id="7000" name="Books"><subcat id="7020" name="EBooks"/></category>', '7020'),
            ('<category id="2000" name="Movies"/><category id="5000" name="TV"/>', None),
        ):
            with self.subTest(expected=expected):
                caps = ('<caps><searching><search available="yes" supportedParams="q"/></searching><categories>'+categories+'</categories></caps>').encode()
                transport = FixtureTransport(callback=lambda c,s,p: caps if p['t']=='caps' else rss())
                batch = configured_search(self.target, (config(categories=()),), transport=transport)
                attempts = batch.sources[0].attempts
                self.assertEqual(len(attempts), 3 if expected else 0)
                self.assertTrue(all(a.request.categories==(int(expected),) for a in attempts))

    def test_automatic_filters_before_ranking_and_never_uses_manual_mechanisms(self):
        ddl = evaluate_release(self.target, candidate('Batman Rebirth Deluxe Edition Book 1 (2017)'))
        alternatives = []
        for mechanism in (AcquisitionMechanism.NZB, AcquisitionMechanism.TORRENT):
            release = replace(ddl.candidate, acquisition=replace(ddl.candidate.acquisition, mechanism=mechanism))
            alternatives.append(replace(evaluate_release(self.target, release), score=9999))
        session = SimpleNamespace(evaluations=(ddl, *alternatives), ddl_ids={ddl.candidate.candidate_id: 'owned'})
        allowed = WantedAutomation.automatic_evaluations(session)
        self.assertEqual(allowed, (ddl,))
        session.evaluations = tuple(alternatives)
        self.assertEqual(WantedAutomation.automatic_evaluations(session), ())
        self.assertEqual(select_automatically((), search_state=SearchState.COMPLETE).reason.value, 'no_acceptable_getcomics_release')

    def test_manual_clients_are_independent_of_automation_and_require_exact_choice(self):
        service = object.__new__(WantedAutomation)
        service.store = SimpleNamespace(db=None)
        service.client_factory = lambda config: SimpleNamespace(config=config)
        sab = SimpleNamespace(key='sab', name='SAB', enabled=True)
        nzbget = SimpleNamespace(key='nzbget', name='NZBGet', enabled=True, protocol='nzb')
        torrent = SimpleNamespace(key='torrent', name='qBittorrent', enabled=True, protocol='torrent')
        with patch('backend.features.wanted_automation.load_automation', return_value={'sab_client_id':None}), \
                patch('backend.features.wanted_automation.load_sab_clients', return_value=(sab,)), \
                patch('backend.features.wanted_automation.load_managed_clients', return_value=(torrent,)) as managed:
            self.assertIs(service.protocol_client('nzb').config, sab)
            self.assertIs(service.protocol_client('torrent').config, torrent)
            managed.return_value = (nzbget, torrent)
            self.assertIsNone(service.protocol_client('nzb'))
            self.assertIs(service.protocol_client('nzb', 'nzbget').config, nzbget)
            self.assertIsNone(service.protocol_client('torrent', 'sab'))
            sab.enabled = False
            nzbget.enabled = False
            self.assertIsNone(service.protocol_client('nzb'))
            self.assertIsNone(service.protocol_client('direct_download'))

    def test_automatic_cannot_enter_manual_force_or_remote_client_dispatch(self):
        from backend.internals.wanted import WantedConflict
        evaluation = evaluate_release(self.target, candidate('Batman Rebirth Deluxe Edition Book 1 (2017)'))
        service = object.__new__(WantedAutomation)
        session = SimpleNamespace(ddl_ids={})
        for mechanism in (AcquisitionMechanism.NZB, AcquisitionMechanism.TORRENT):
            release = replace(evaluation.candidate, acquisition=replace(evaluation.candidate.acquisition, mechanism=mechanism))
            observed = replace(evaluation, candidate=release)
            for force in (False, True):
                with self.subTest(mechanism=mechanism, force=force), self.assertRaises(WantedConflict):
                    service.grab(session, observed, automatic=True, force=force)

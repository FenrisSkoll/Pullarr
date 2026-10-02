"""Current adapters/budgets plus local loopback GCD, not invented family APIs."""

from unittest import TestCase
from unittest.mock import patch

from Tbackend.features import aggregated_metadata_search as search_fixture

from backend.features.collections import Collections
from backend.internals.collections import CollectionStore


class CollectionProviderTests(TestCase):
    def setUp(self):
        self.fixture = search_fixture.AggregateSearchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.store = CollectionStore(self.db.cursor())
        self.node = self.store.create('Family')['nodes'][0]['id']
        self.tasks = []
        self.owner = Collections(enqueue=lambda task: self.tasks.append(task) or len(self.tasks))
        p = patch('backend.features.collections.get_db', side_effect=self.db.cursor)
        p.start(); self.addCleanup(p.stop)

    def test_real_three_adapter_search_partial_failure_decisions_preserved(self):
        h = self.owner.submit_search(self.node, 'Batman', suggestions=True)
        self.tasks[-1].run()
        result = self.owner.delivery(h['id'])
        self.assertEqual(result['state'], 'complete')
        self.assertEqual({r['provider'] for r in result['items']}, {'comicvine', 'metron', 'gcd'})
        suggestions = self.store.suggestions(self.node)['items']
        cv = next(s for s in suggestions if s['provider'] == 'comicvine')
        self.store.decide(cv['id'], 0, 0, 'rejected')
        self.fixture.settings.gcd_enabled = False
        h = self.owner.submit_search(self.node, 'Batman', suggestions=True)
        self.tasks[-1].run()
        result = self.owner.delivery(h['id'])
        self.assertEqual(next(p for p in result['providers'] if p['provider'] == 'gcd')['state'], 'disabled')
        self.assertEqual(self.store.suggestions(self.node, 'rejected')['items'][0]['id'], cv['id'])
        self.assertEqual(len(self.store.suggestions(self.node)['items']), 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_memberships').fetchone()[0], 0)

    def test_explicit_provider_only_and_no_network_on_local_pages(self):
        before = len(self.fixture.fake.requests)
        h = self.owner.submit_search(self.node, 'Batman', provider='metron')
        self.tasks[-1].run()
        self.assertEqual([p['provider'] for p in self.owner.delivery(h['id'])['providers']], ['metron'])
        self.assertEqual(len(self.fixture.fake.requests), before)
        with patch('socket.socket', side_effect=AssertionError('no network on Collection pages')):
            self.store.tree(1)
            self.store.publications(1)
            self.store.suggestions(self.node)

"""One-hop exact continuation and optional artwork budgets without live providers."""

import asyncio
import json
from dataclasses import replace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

from fixtures.metron import issue, issue_summary, page

from backend.base.custom_exceptions import InvalidKeyValue
from backend.features.metadata_artwork import SearchArtwork
from backend.features.metadata_search import expand_related, rank_results
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.gcd import GcdMetadataProvider
from backend.implementations.metadata.gcd_client import GcdClient
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.metadata.metron_client import (MetronClient,
                                                            MetronError)
from backend.implementations.metadata.models import VolumeSearchResult
from backend.implementations.metadata.search_presentation import \
    comicvine_relations


def candidate(identity='1', title='Batman: Rebirth Deluxe Edition', provider='comicvine'):
    return VolumeSearchResult(provider, identity, title, 2017, 1, None, None, None, [], None, 3, False, None)


class RelationExpansionTests(TestCase):
    def setUp(self):
        self.source = candidate()
        self.source.description = '<p>Books 1–3. Continued in <a href="/batman/4050-128991/">Batman: Deluxe Edition</a></p>'
        self.source.relations = comicvine_relations('1', self.source.description)
        self.target = candidate('128991', 'Batman: Deluxe Edition')
        self.target.description = '<p>Books 4–6. Preceded by <a href="/batman/4050-1/">Batman: Rebirth Deluxe Edition</a></p>'
        self.target.relations = comicvine_relations('128991', self.target.description)
        self.provider = Mock(search_aggregate=AsyncMock(return_value=[self.target]))

    def test_batman_exact_lookup_distinct_records_cycle_not_followed(self):
        results, left = asyncio.run(expand_related(self.provider, 'comicvine', [self.source], 4))
        self.provider.search_aggregate.assert_awaited_once_with('cv:128991')
        self.assertEqual([r.provider_id for r in results], ['1', '128991'])
        self.assertEqual(left, 3)
        self.assertEqual(results[1].search_origin, 'relation')
        self.assertEqual(results[1].relation_reason, self.source.relations[0])
        self.assertIn('1–3', results[0].description)
        self.assertIn('4–6', results[1].description)
        self.assertFalse(hasattr(results[0], 'issues'))
        self.assertEqual([r.provider_id for r in rank_results('Batman Rebirth Deluxe Edition', results)], ['1', '128991'])

    def test_normal_result_already_contains_target_no_lookup_or_relabel(self):
        results, left = asyncio.run(expand_related(self.provider, 'comicvine', [self.source, self.target], 4))
        self.provider.search_aggregate.assert_not_called()
        self.assertEqual([r.search_origin for r in results], ['direct', 'direct'])

    def test_undirected_related_series_is_not_a_continuation_expansion(self):
        self.source.relations = [replace(self.source.relations[0], relation_type='related_series')]
        results, left = asyncio.run(expand_related(self.provider, 'comicvine', [self.source], 4))
        self.assertEqual(results, [self.source])
        self.assertEqual(left, 4)
        self.provider.search_aggregate.assert_not_called()

    def test_unavailable_or_wrong_exact_identity_retains_primary(self):
        for reply in ([candidate('9')], []):
            self.provider.search_aggregate.return_value = reply
            results, _ = asyncio.run(expand_related(self.provider, 'comicvine', [self.source], 4))
            self.assertEqual(results, [self.source])
        self.provider.search_aggregate.side_effect = MetadataProviderError('comicvine', 'rate_limited')
        results, _ = asyncio.run(expand_related(self.provider, 'comicvine', [self.source], 4))
        self.assertEqual(results, [self.source])

    def test_global_four_attempts_per_result_two_and_no_cross_provider(self):
        rows = []
        for i in range(10, 260):
            row = candidate(str(i))
            row.relations = [replace(self.source.relations[0], source_id=str(i), target_id=str(i + j * 1000)) for j in range(1, 5)]
            rows.append(row)
        self.provider.search_aggregate.return_value = []
        _, left = asyncio.run(expand_related(self.provider, 'comicvine', rows, 4))
        self.assertEqual(left, 0)
        self.assertEqual([c.args[0] for c in self.provider.search_aggregate.await_args_list], ['cv:1010', 'cv:2010', 'cv:1011', 'cv:2011'])
        self.source.relations = [replace(self.source.relations[0], target_provider='gcd')]
        self.provider.search_aggregate.reset_mock()
        asyncio.run(expand_related(self.provider, 'comicvine', [self.source], 4))
        self.provider.search_aggregate.assert_not_called()

    def test_explainable_ranking_and_meaningful_terms_retained(self):
        related = replace(self.target, search_origin='relation')
        weak = candidate('3', 'Unrelated Title')
        alias = candidate('4', 'Collection'); alias.aliases = ['Batman Rebirth Deluxe Edition']
        results = rank_results('Batman Rebirth Deluxe Edition', [weak, related, self.target, alias, self.source], 2017)
        self.assertEqual([r.provider_id for r in results], ['1', '4', '128991', '128991', '3'])
        self.assertFalse(self.target.rank_components['exact_title'])
        self.assertTrue(self.source.rank_components['year_match'])
        self.assertEqual(self.source.rank_components['query_tokens_total'], 4)


class ArtworkCacheTests(TestCase):
    def setUp(self):
        self.now = 10
        self.fetcher = Mock()
        self.fetcher.fetch_image.return_value = b'jpeg-thumbnail'
        self.service = SearchArtwork(clock=lambda: self.now, fetcher=self.fetcher)
        self.provider = MetronMetadataProvider()
        self.provider.search_artwork_url = Mock(return_value='https://static.metron.cloud/media/issue/a.jpg')
        self.provider.search_unavailable = Mock(return_value=None)
        p = patch('backend.features.metadata_artwork.get_search_provider', return_value=self.provider)
        p.start(); self.addCleanup(p.stop)
        self.rows = [replace(candidate(str(i), provider='metron'), artwork_hint=str(i)) for i in range(1, 251)]
        self.ticket = self.service.register(self.rows)

    def test_250_results_registration_has_zero_enrichment_calls(self):
        self.provider.search_artwork_url.assert_not_called()
        self.fetcher.fetch_image.assert_not_called()

    def test_success_cache_qualified_id_and_no_token_url(self):
        first = self.service.batch(self.ticket, ['metron:1'])
        self.assertEqual(first[0]['artwork_state'], 'available')
        self.assertTrue(first[0]['image'].startswith('data:image/jpeg;base64,'))
        self.assertNotIn('https:', json.dumps(first))
        self.assertEqual(first, self.service.batch(self.ticket, ['metron:1']))
        self.provider.search_artwork_url.assert_called_once_with('1', '1')
        self.service.batch(self.ticket, ['metron:2'])
        self.assertEqual(self.provider.search_artwork_url.call_count, 2)

    def test_unavailable_negative_cache_expiry(self):
        self.provider.search_artwork_url.return_value = None
        self.assertEqual(self.service.batch(self.ticket, ['metron:1'])[0]['artwork_state'], 'unavailable')
        self.service.batch(self.ticket, ['metron:1'])
        self.assertEqual(self.provider.search_artwork_url.call_count, 1)
        self.now += 601
        ticket = self.service.register(self.rows)
        self.service.batch(ticket, ['metron:1'])
        self.assertEqual(self.provider.search_artwork_url.call_count, 2)

    def test_failure_is_optional_and_cached(self):
        self.provider.search_artwork_url.side_effect = MetadataProviderError('metron', 'budget')
        self.assertEqual(self.service.batch(self.ticket, ['metron:1'])[0]['artwork_state'], 'unavailable')
        self.service.batch(self.ticket, ['metron:1'])
        self.assertEqual(self.provider.search_artwork_url.call_count, 1)

    def test_server_batch_search_ticket_bounds(self):
        for identities in ([], ['metron:1'] * 2, ['metron:' + str(i) for i in range(1, 6)], ['gcd:1'], ['metron:999']):
            with self.assertRaises(InvalidKeyValue):
                self.service.batch(self.ticket, identities)
        for start in (1, 5, 9):
            self.service.batch(self.ticket, ['metron:' + str(i) for i in range(start, start + 4)])
        with self.assertRaises(InvalidKeyValue):
            self.service.batch(self.ticket, ['metron:13'])
        self.assertEqual(self.provider.search_artwork_url.call_count, 12)

    def test_cache_and_ticket_eviction(self):
        for i in range(1, 141):
            ticket = self.service.register(self.rows)
            self.service.batch(ticket, ['metron:' + str(i)])
        self.assertEqual(len(self.service.cache), 128)
        self.assertEqual(len(self.service.tickets), 32)
        with self.assertRaises(InvalidKeyValue):
            self.service.batch(self.ticket, ['metron:1'])

    def test_concurrent_batch_does_not_duplicate_requests(self):
        self.service.active.acquire()
        try:
            self.assertEqual(self.service.batch(self.ticket, ['metron:1'])[0]['artwork_state'], 'unavailable')
            self.provider.search_artwork_url.assert_not_called()
        finally:
            self.service.active.release()

    def test_cache_eviction_cannot_repeat_enrichment_with_same_ticket(self):
        self.service.batch(self.ticket, ['metron:1'])
        self.service.cache.clear()
        self.assertEqual(self.service.batch(self.ticket, ['metron:1'])[0]['artwork_state'], 'unavailable')
        self.provider.search_artwork_url.assert_called_once()


class ArtworkProviderTests(TestCase):
    def test_metron_first_page_only_exact_parent(self):
        row = issue_summary(issue(image='https://static.metron.cloud/media/issue/a.jpg'))
        with patch.object(MetronClient, 'get', return_value=page([row], 'https://metron.cloud/api/next/', 100)) as get:
            with patch('backend.implementations.metadata.metron_client.Settings', create=True):
                with patch.object(MetronClient, '__init__', return_value=None):
                    self.assertTrue(MetronMetadataProvider().search_artwork_url('700', '700'))
            get.assert_called_once_with('series/700/issue_list/', bounded=True)

    def test_metron_malformed_parent_or_page(self):
        for data in ({'results': None}, page([issue(series={'id': 999})]), page([issue()] * 101)):
            with patch.object(MetronClient, '__init__', return_value=None), patch.object(MetronClient, 'get', return_value=data):
                with self.assertRaises(MetronError):
                    MetronMetadataProvider().search_artwork_url('700', '700')

    def test_gcd_one_issue_exact_parent_and_close(self):
        client = Mock()
        client.identity = GcdClient.identity
        # Use the real origin validator on an uninitialized production-origin client.
        validator = GcdClient.__new__(GcdClient); validator.origin = ('https', 'www.comics.org')
        client.identity = validator.identity
        url = 'https://files1.comics.org//img/gcd/covers_by_id/1/w400/1000.jpg'
        client.get.return_value = dict(api_url='https://www.comics.org/api/issue/2/', series='https://www.comics.org/api/series/1/', cover=url)
        self.assertEqual(GcdMetadataProvider(lambda: client).search_artwork_url('1', '2'), url)
        client.get.assert_called_once_with('issue/2/')
        client.close.assert_called_once()
        with self.assertRaises(MetadataProviderError):
            GcdMetadataProvider(lambda: client).search_artwork_url('3', '2')

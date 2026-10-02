"""Real adapters, Flask, SQLite and loopback GCD; no live provider access."""

import asyncio
import json
from dataclasses import replace
from time import perf_counter
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

from fixtures.comicvine_search import FAKE_APP_KEY, volume_response
from fixtures.metron import page, series_summary
from Tbackend.implementations.gcd_lifecycle import GcdLifecycleHarness

from backend.base.custom_exceptions import InvalidKeyValue
from backend.base.helpers import AsyncSession
from backend.features.metadata_search import (aggregate_response,
                                              aggregated_search, search_scope)
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.metron_client import (MetronClient,
                                                            MetronError)
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.metadata.registry import (PROVIDERS,
                                                       get_search_provider)
from backend.implementations.volumes import Library
from backend.internals.server import Server
from frontend.metadata import qualified_volume_search_result


class AggregateSearchTests(GcdLifecycleHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.settings.metron_api_token = 'synthetic-token'
        self.settings.gcd_enabled = True
        for module in ('backend.internals.settings', 'backend.implementations.metadata.metron',
                       'backend.implementations.metadata.gcd', 'frontend.api'):
            self.start_patch(module + '.Settings').return_value.sv = self.settings
        for module in ('backend.features.metadata_search', 'backend.implementations.metadata.metron'):
            self.start_patch(module + '.get_db', side_effect=self.db.cursor)
        self.http = self.start_patch('backend.implementations.metadata.metron_client.Session').return_value.__enter__.return_value
        self.http.get.side_effect = self.metron_response
        p = patch.dict('backend.implementations.metadata.metron_client.RATE_STATE', {}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.respond([volume_response(name='Batman', start_year='2016')])
        self.fake.series.update(name='Batman', year_began=2016)
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        for name in ('WebSocket', 'SimpleQueue', 'MPWebSocketQueue'):
            self.start_patch('backend.internals.server.' + name)
        self.app = Server._create_app()
        self.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = self.app.test_client()

    def metron_response(self, url, **kwargs):
        row = series_summary()
        row.update(series='Batman', year_began=2016, cv_id=2127)
        response = Mock(status_code=200, headers={})
        response.json.return_value = page([row])
        return response

    def search(self, query='Batman', **params):
        return self.client.get('/api/volumes/search', query_string={
            'api_key': FAKE_APP_KEY, 'query': query, 'provider': 'all', **params})

    def aggregate(self, query='Batman', year=None):
        return aggregate_response(query, asyncio.run(aggregated_search(query, year)), qualified_volume_search_result, year)

    def test_three_success_native_fields_order_xrefs_not_merged_request_sum(self):
        singles = [asyncio.run(get_search_provider(key).search_volumes('Batman')) for key in PROVIDERS]
        before = (self.session.get.await_count, self.http.get.call_count, len(self.fake.requests))
        result = self.search().json['result']
        self.assertEqual(result['schema'], 'metadata-search/v2')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([g['provider'] for g in result['providers']], list(PROVIDERS))
        for group, single in zip(result['providers'], singles):
            actual = group['results'][0].copy()
            for key in ('result_key', 'local_identity_annotations', 'identity_conflict'):
                actual.pop(key)
            self.assertEqual(actual, qualified_volume_search_result(single[0]))
        self.assertEqual([g['results'][0]['title'] for g in result['providers']], ['Batman'] * 3)
        self.assertEqual([g['results'][0]['result_key'] for g in result['providers']], ['comicvine:2127', 'metron:700', 'gcd:1'])
        # Metron's old search DTO intentionally does not transport raw cv_id.
        self.assertEqual(result['providers'][1]['results'][0]['external_ids'], {'metron': '700'})
        after = (self.session.get.await_count, self.http.get.call_count, len(self.fake.requests))
        self.assertEqual(tuple(b - a for a, b in zip(before, after)), (1, 1, 1))
        self.assertFalse(any('/issue/' in path for path, _ in self.fake.requests))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)

    def test_partial_rate_failure_keeps_other_groups(self):
        self.http.get.return_value = Mock(status_code=429, headers={'Retry-After': '60'})
        self.http.get.side_effect = None
        result = self.aggregate()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual([g['status'] for g in result['providers']], ['complete', 'rate_limited', 'complete'])
        self.assertEqual([g['result_count'] for g in result['providers']], [1, 0, 1])
        self.assertIsNotNone(result['providers'][1]['retry_at'])

    def test_disabled_missing_auth_and_all_unavailable_zero_http(self):
        self.settings.metron_api_token = ''
        self.settings.comicvine_api_key = ''
        self.settings.gcd_enabled = False
        result = self.aggregate()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual([g['status'] for g in result['providers']], ['auth_required', 'auth_required', 'disabled'])
        self.session.get.assert_not_called()
        self.http.get.assert_not_called()
        self.assertEqual(self.fake.requests, [])

    def test_all_zero_is_complete_not_failure(self):
        self.respond([])
        response = Mock(status_code=200, headers={})
        response.json.return_value = page([])
        self.http.get.side_effect = None
        self.http.get.return_value = response
        self.fake.override = lambda _: (200, page([]), 'application/json', {})
        result = self.aggregate()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([g['result_count'] for g in result['providers']], [0, 0, 0])

    def test_exact_duplicate_dedup_contradiction_protocol_failure(self):
        self.respond([volume_response(), volume_response()])
        result = self.aggregate()['providers'][0]
        self.assertEqual((result['result_count'], result['duplicate_count']), (1, 1))
        self.respond([volume_response(), volume_response(name='Contradiction')])
        result = self.aggregate()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['providers'][0]['reason'], 'inconsistent_results')
        self.assertEqual(result['providers'][0]['results'], [])

    def test_exact_local_authority_vs_persisted_xref_no_switch(self):
        self.db.execute("INSERT INTO volumes(id,title,root_folder,metadata_provider,comicvine_id) VALUES(1,'Local',1,'metron',2127)")
        self.db.executemany('''INSERT INTO volume_external_ids(volume_id,provider,provider_id,provenance) VALUES(?,?,?,?)
            ON CONFLICT(volume_id,provider) DO UPDATE SET provenance=excluded.provenance''',
            [(1, 'metron', '700', 'metron'), (1, 'comicvine', '2127', 'metron')])
        self.db.commit()
        result = self.aggregate()
        cv, metron = [g['results'][0] for g in result['providers'][:2]]
        self.assertIsNone(cv['already_added'])
        self.assertEqual(cv['local_identity_annotations'][0]['kind'], 'local_persisted_cross_reference')
        self.assertEqual(metron['already_added'], 1)
        self.assertEqual(self.db.execute('SELECT metadata_provider FROM volumes').fetchone()[0], 'metron')
        # Historical CV duplicate identity is representable; don't resolve it.
        self.db.execute("INSERT INTO volumes(id,title,root_folder,metadata_provider,comicvine_id) VALUES(2,'Other',1,'comicvine',2127)")
        self.db.commit()
        result = self.aggregate()['providers'][0]['results'][0]
        self.assertTrue(result['identity_conflict'])
        self.assertEqual(result['already_added'], 2)
        self.db.execute("INSERT INTO volumes(id,title,root_folder,metadata_provider,comicvine_id) VALUES(3,'Duplicate',1,'comicvine',2127)")
        self.db.commit()
        result = self.aggregate()['providers'][0]['results'][0]
        self.assertTrue(result['identity_conflict'])
        self.assertIsNone(result['already_added'])  # No arbitrary local winner.

    def test_qualified_direct_query_only_selected_provider_and_exact_add(self):
        result = self.search('gcd:1').json['result']
        self.assertEqual([g['provider'] for g in result['providers']], ['gcd'])
        self.session.get.assert_not_called()
        self.http.get.assert_not_called()
        chosen = result['providers'][0]['results'][0]['metadata_source']
        local = Library.add_metadata(ProviderVolumeIdentity(chosen['provider'], chosen['id']), 1, True)
        self.assertEqual(self.db.execute('SELECT metadata_provider FROM volumes WHERE id=?', (local,)).fetchone()[0], 'gcd')
        self.assertEqual(len(self.fake.requests), 5)  # one search, N+3 add
        self.session.get.assert_not_called()
        self.http.get.assert_not_called()

    def test_chosen_add_failure_never_falls_back(self):
        self.search('gcd:1')
        self.fake.override = lambda _: (404, {}, 'application/json', {})
        with self.assertRaises(MetadataProviderError):
            Library.add_metadata(ProviderVolumeIdentity('gcd', '1'), 1, True)
        self.session.get.assert_not_called()
        self.http.get.assert_not_called()
        self.assert_empty_library()

    def test_year_only_gcd_literal_query_and_bare_number(self):
        result = self.search('Batman 2016', year='2021').json['result']
        self.assertEqual([g['year_applied'] for g in result['providers']], [False, False, True])
        self.assertIn('/name/Batman%202016/year/2021/', self.fake.requests[0][0])
        self.assertEqual(self.session.get.call_args.kwargs['params']['query'], 'Batman 2016')
        self.assertEqual(self.http.get.call_args.kwargs['params'], {'name': 'Batman 2016'})
        self.assertEqual(search_scope('123')[0], list(PROVIDERS))
        self.assertEqual(search_scope('comicvine:123'), (['comicvine'], 'cv:123'))
        self.assertEqual(search_scope('4050-123')[0], ['comicvine'])
        for query in ('foo:123', 'gcd:no', 'metron:../1', 'cv:0'):
            with self.assertRaises(InvalidKeyValue):
                search_scope(query)
        self.assertEqual(self.search(year='0').status_code, 400)

    def test_credentials_safe_in_aggregate_legacy_shape_unchanged(self):
        self.respond([], status_code=100)
        with patch('backend.base.custom_exceptions.LOGGER') as logger:
            result = self.aggregate()
        self.assertEqual(result['providers'][0]['status'], 'auth_required')
        self.assertNotIn(self.settings.comicvine_api_key, json.dumps(result))
        self.assertNotIn(self.settings.comicvine_api_key, str(logger.mock_calls))
        self.assertTrue(self.session.get.call_args.kwargs['private'])
        async def private_transport():
            response = Mock(status=400, url='https://example.invalid/?api_key=transport-secret', headers={})
            response.text = AsyncMock(return_value='transport-secret')
            with patch('backend.implementations.flaresolverr.FlareSolverr') as fs, \
                    patch('aiohttp.ClientSession._request', new=AsyncMock(return_value=response)) as request, \
                    patch('backend.base.helpers.LOGGER') as transport_log:
                fs.return_value.get_ua_cookies.return_value = ('fixture', '')
                async with AsyncSession() as session:
                    await session.get('https://example.invalid/', params={'api_key': 'transport-secret'}, private=True)
                    await session.get('https://example.invalid/nested/', params={'api_key': 'transport-secret'})
                self.assertNotIn('transport-secret', str(transport_log.mock_calls))
                self.assertNotIn('private', request.call_args.kwargs)
                response.text.assert_not_awaited()
        asyncio.run(private_transport())
        self.respond([volume_response()])
        legacy = self.client.get('/api/volumes/search', query_string={'api_key': FAKE_APP_KEY, 'query': 'Batman'})
        self.assertIsInstance(legacy.json['result'], list)
        self.assertNotIn('metadata_source', legacy.json['result'][0])
        self.assertEqual(self.client.get('/api/volumes/search?provider=all&query=x').status_code, 401)

    def test_programming_errors_propagate_known_failures_safe(self):
        with patch.object(PROVIDERS['metron'], 'search_aggregate', new=AsyncMock(side_effect=RuntimeError('programming defect'))):
            with self.assertRaises(RuntimeError):
                self.aggregate()
        with patch.object(PROVIDERS['metron'], 'search_aggregate', new=AsyncMock(side_effect=MetronError('unknown-private-data'))):
            result = self.aggregate()
        self.assertEqual(result['providers'][1]['reason'], 'provider_failure')
        self.assertNotIn('unknown-private-data', json.dumps(result))

    def test_bounded_metron_pages_legacy_unbounded_contract_preserved(self):
        client = MetronClient('synthetic')
        with patch.object(client, 'get', return_value=page([], 'https://metron.cloud/api/series/?page=2', 251)) as get:
            with self.assertRaises(MetronError) as raised:
                client.pages('series/', max_pages=5, max_results=250)
            self.assertEqual(raised.exception.reason, 'search_limit')
            self.assertEqual(get.call_count, 1)
        with patch.object(PROVIDERS['metron'], 'search_aggregate', new=AsyncMock(side_effect=MetronError('search_limit'))):
            self.assertEqual(self.aggregate()['providers'][1]['status'], 'limited')

    def test_known_http_failures_isolated_and_search_does_not_write_domain(self):
        tables = [row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='config'")]
        before = {t: self.db.execute('SELECT * FROM "' + t + '"').fetchall() for t in tables}
        for code, status in ((401, 'auth_required'), (403, 'failed'), (500, 'unavailable')):
            self.http.get.side_effect = None
            self.http.get.return_value = Mock(status_code=code, headers={})
            result = self.aggregate()
            self.assertEqual(result['providers'][1]['status'], status)
            self.assertEqual(result['providers'][0]['result_count'], 1)
            self.assertEqual(result['providers'][2]['result_count'], 1)
        for table in tables:
            self.assertEqual(self.db.execute('SELECT * FROM "' + table + '"').fetchall(), before[table], table)
        # Only GCD's existing attempt ledger is charged by these local searches.
        state = json.loads(self.db.execute("SELECT value FROM config WHERE key='gcd_request_ledger_v1'").fetchone()[0])
        self.assertEqual(len(state['attempts']), 3)

    def test_unknown_provider_invalid_query_and_year_no_http(self):
        for params in ({'provider': 'typo'}, {'query': 'x' * 501}, {'year': 'no'}, {'query': 'unknown:123'}):
            self.assertEqual(self.search(**params).status_code, 400)
        self.session.get.assert_not_called()
        self.http.get.assert_not_called()
        self.assertEqual(self.fake.requests, [])

    def test_result_cap_and_body_cap_are_not_false_complete(self):
        candidate = asyncio.run(get_search_provider('comicvine').search_volumes('Batman'))[0]
        for results, reason in (([replace(candidate, provider_id=str(i + 1)) for i in range(51)], 'search_limit'),
                                ([replace(candidate, description='x' * (8 * 1024 * 1024))], 'response_limit')):
            with patch.object(PROVIDERS['comicvine'], 'search_aggregate', new=AsyncMock(return_value=results)):
                result = self.aggregate()
            self.assertEqual(result['providers'][0]['reason'], reason)
            self.assertEqual(result['providers'][0]['results'], [])
            self.assertEqual(result['status'], 'partial')

    def test_unsupported_capability_no_instantiation_and_total_typed_failure(self):
        class Unsupported:
            def __init__(self):
                raise AssertionError('Do not instantiate unsupported provider')
        with patch.dict(PROVIDERS, {'metron': Unsupported}):
            result = self.aggregate()
        self.assertEqual(result['providers'][1]['reason'], 'unsupported_capability')
        self.http.get.assert_not_called()
        self.settings.metron_api_token = ''
        self.settings.gcd_enabled = False
        self.respond([], status_code=100)
        result = self.search().json['result']
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(any(g['results'] for g in result['providers']))

    def test_delay_order_native_order_and_query_diagnostics(self):
        candidates = [asyncio.run(get_search_provider(key).search_volumes('Batman'))[0] for key in PROVIDERS]
        patches = []
        for (key, provider), candidate, delay in zip(PROVIDERS.items(), candidates, (.01, .02, 0)):
            async def delayed(instance, query, year=None, value=candidate, pause=delay):
                await asyncio.sleep(pause)
                return [replace(value, provider_id='9'), replace(value, provider_id='2')]
            p = patch.object(provider, 'search_aggregate', new=delayed)
            patches.append(p)
            p.start()
            self.addCleanup(p.stop)
        queries = []
        self.db.set_trace_callback(queries.append)
        started = perf_counter()
        result = self.aggregate()
        elapsed = perf_counter() - started
        self.db.set_trace_callback(None)
        self.assertEqual([g['provider'] for g in result['providers']], list(PROVIDERS))
        self.assertTrue(all([r['metadata_source']['id'] for r in g['results']] == ['9', '2'] for g in result['providers']))
        self.assertEqual(sum(q.startswith('SELECT') for q in queries), 1)
        print('Aggregate fixture delay/local diagnostics:', round(elapsed, 4), 'seconds; one identity SELECT; no detail fetch')
        for p in patches:
            p.stop()
        for (key, provider), candidate in zip(PROVIDERS.items(), candidates):
            values = [replace(candidate, provider_id=str(i + 1)) for i in range(provider.search_result_limit)]
            p = patch.object(provider, 'search_aggregate', new=AsyncMock(return_value=values))
            p.start()
            self.addCleanup(p.stop)
        queries.clear()
        self.db.set_trace_callback(queries.append)
        started = perf_counter()
        result = self.aggregate()
        elapsed = perf_counter() - started
        self.db.set_trace_callback(None)
        self.assertEqual(sum(g['result_count'] for g in result['providers']), 550)
        self.assertEqual(sum(q.startswith('SELECT') for q in queries), 2)
        print('Aggregate 550-result diagnostics:', round(elapsed, 4), 'seconds; two identity SELECTs;', len(json.dumps(result)), 'JSON characters')

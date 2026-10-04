"""Offline Newznab/Prowlarr protocol, pure planning, failure and security gates."""

import json
import random
import socket
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import FrozenInstanceError, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter, sleep
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

from flask import Flask

from backend.base.definitions import SpecialVersion
from backend.base.release_candidate import AcquisitionMechanism, LocatorKind
from backend.base.release_evaluation import Compatibility, TargetKind
from backend.base.release_search import (ReleaseSearchRequest,
                                         SearchCapabilities, SearchError,
                                         SearchLimits, SearchPage, SearchState,
                                         SourceConfig, SourceFailure)
from backend.implementations.newznab import (MAX_SEARCH_BYTES, BoundedHTTP,
                                             NewznabSource, SearchBudget,
                                             discover_prowlarr,
                                             parse_capabilities, parse_search)
from backend.implementations.release_candidates import adapt_ddl_result
from backend.implementations.release_explanations import explain_release
from backend.implementations.release_scoring import evaluate_release
from backend.implementations.release_search import (check_source,
                                                    configured_search,
                                                    evaluate_search,
                                                    plan_queries,
                                                    preview_search,
                                                    search_source)
from backend.internals.release_sources import (delete_source,
                                               load_sources, save_source)
from tests.Tbackend.features.release_candidates import ddl
from tests.Tbackend.features.release_scoring import target

CAPS = b'<caps><limits max="100"/><searching><search available="yes" supportedParams="q"/></searching><categories><category id="7000"><subcat id="7030"/></category></categories></caps>'
SECRET = 'fixture-only-private-key'


def item(title='Batman #5 (2016).cbz', guid='release-5', url='http://fixture.test/nzb?apikey=' + SECRET,
         extra='', size='12345', date='Sun, 06 Jun 2010 17:29:23 +0100'):
    return (f'<item><title>{escape(title)}</title>' +
            (f'<guid>{escape(guid)}</guid>' if guid is not None else '') +
            (f'<enclosure url="{escape(url)}" length="{size}" type="application/x-nzb"/>' if url else '') +
            f'<pubDate>{date}</pubDate><n:attr name="category" value="7030"/>{extra}</item>')


def rss(items='', offset=0, total=None):
    total = items.count('<item>') if total is None else total
    return (f'<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>'
            f'<n:response offset="{offset}" total="{total}"/>{items}</channel></rss>').encode()


def config(**kwargs):
    return SourceConfig(**dict({'key': 'fixture', 'name': 'Fixture', 'url': 'http://fixture.test/api',
                               'api_key': SECRET}, **kwargs))


class FixtureTransport:
    def __init__(self, result=None, callback=None):
        self.result = result if result is not None else rss(item())
        self.callback, self.calls = callback, []

    def get(self, conf, suffix, params, maximum):
        self.calls.append((conf.key, suffix, params))
        if self.callback:
            return self.callback(conf, suffix, params)
        return CAPS if params.get('t') == 'caps' else self.result


@contextmanager
def fake_http(callback, *, daemon=False):
    """Disposable loopback only, works inside Docker --network none."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            calls.append((path.path, query, self.headers.get('X-Api-Key')))
            status, body, headers = callback(path.path, query)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if 'Content-Length' not in headers and headers.get('Connection') != 'close':
                self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=daemon)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class QueryPlanningTests(TestCase):
    def test_literal_issue_matrix(self):
        for label in ('1', '01', '1.0', '1.5', '1A', '1.01', '[nn]', 'Annual', 'Special'):
            with self.subTest(label=label):
                plans = plan_queries(target(label))
                self.assertIn(label, plans[0].query)
                self.assertLessEqual(len(plans), 3)
                if label == '1A':
                    self.assertNotIn('1.01', str(plans))

    def test_special_volume_collection_and_vai(self):
        for special, marker in ((SpecialVersion.TPB, 'TPB'), (SpecialVersion.HARD_COVER, 'Hardcover'),
                (SpecialVersion.OMNIBUS, 'Omnibus'), (SpecialVersion.ONE_SHOT, 'One-Shot')):
            self.assertIn(marker, plan_queries(target(kind=TargetKind.COLLECTION, special=special))[0].query)
        self.assertIn('Vol 5', plan_queries(target(special=SpecialVersion.VOLUME_AS_ISSUE))[0].query)
        self.assertEqual('Batman 2016', plan_queries(target(kind=TargetKind.WHOLE_VOLUME))[0].query)

    def test_alias_budget_dedup_and_unicode(self):
        t = target()
        t = replace(t, publication=replace(t.publication, title='進撃の巨人',
                    aliases=tuple(['  進撃の巨人  '] + [f'Alias {i}' for i in range(1000)])))
        plan = plan_queries(t)
        self.assertEqual(len(plan), 3)
        self.assertIn('進撃の巨人', plan[0].query)
        self.assertIn('Alias 0', plan[-1].query)
        self.assertEqual(plan, plan_queries(t))

    def test_invalid_empty_and_long_request(self):
        for query in ('', ' ', 'a' * 513, 'query\nheader'):
            with self.assertRaises(SourceFailure):
                ReleaseSearchRequest(query)

    def test_limits_validation(self):
        for values in ({'queries': 4}, {'requests': 129}, {'sources': 33}, {'page_size': 101}, {'pages': 0}):
            with self.assertRaises(SourceFailure):
                SearchLimits(**values)

    def test_pure_no_io_or_scoring(self):
        t = target()
        with ExitStack() as stack:
            for name in ('socket.socket', 'sqlite3.connect', 'builtins.open',
                         'backend.implementations.release_scoring.evaluate_release'):
                stack.enter_context(patch(name, side_effect=AssertionError('forbidden')))
            plan_queries(t)
            parse_search(rss(item()))


class ProtocolTests(TestCase):
    def test_caps(self):
        caps = parse_capabilities(CAPS)
        self.assertEqual(caps, SearchCapabilities(True, 100, (7000, 7030)))
        self.assertFalse(parse_capabilities(CAPS.replace(b'yes', b'no')).search)

    def test_namespaces_attributes_and_date(self):
        transport = FixtureTransport(rss(item(extra='<n:attr value="500" name="size"/>')))
        c = NewznabSource(config(), transport).search(ReleaseSearchRequest('Batman')).candidates[0]
        self.assertEqual(c.size_bytes, 500)
        self.assertEqual(c.published_at.isoformat(), '2010-06-06T16:29:23+00:00')
        self.assertEqual(c.acquisition.mechanism, AcquisitionMechanism.NZB)
        self.assertIn('newznab-category:7030', c.observations[0].tags)
        self.assertEqual(c.raw_title, 'Batman #5 (2016).cbz')

    def test_namespace_prefix_attribute_order_determinism(self):
        one = rss(item(extra='<n:attr name="size" value="500"/>'))
        two = one.replace(b'n:', b'other:').replace(b'xmlns:n=', b'xmlns:other=').replace(
            b'name="size" value="500"', b'value="500" name="size"')
        self.assertEqual(parse_search(one), parse_search(two))

    def test_unsafe_xml_including_utf16(self):
        for xml in ('<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///private">]><rss><channel>&x;</channel></rss>',
                    '<!DOCTYPE rss SYSTEM "http://external.test/schema"><rss><channel/></rss>'):
            for data in (xml.encode(), xml.encode('utf-16')):
                with self.assertRaises(SourceFailure):
                    parse_search(data)

    def test_depth_and_size(self):
        for data in (b'x' * (MAX_SEARCH_BYTES + 1), b'<a>' * 20 + b'</a>' * 20):
            with self.assertRaises(SourceFailure) as error:
                parse_search(data)
            self.assertEqual(error.exception.code, SearchError.LIMIT)

    def test_malformed_document_not_empty_success(self):
        for data in (b'<rss>', b'<html/>', b'<rss/>', b'not xml'):
            with self.assertRaises(SourceFailure):
                parse_search(data)

    def test_item_isolation_and_identity_size_bounds(self):
        data = rss('<item><title/></item>' + item() + item(guid='x' * 2049))
        rows, count, _, _, errors = parse_search(data)
        self.assertEqual(count, 3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(errors, (0, 2))

    def test_optional_invalid_size_date(self):
        c = NewznabSource(config(), FixtureTransport(rss(item(size='unknown', date='bad date')))).search(
            ReleaseSearchRequest('Batman')).candidates[0]
        self.assertIsNone(c.size_bytes)
        self.assertIsNone(c.published_at)
        self.assertIn('invalid_size', [d.code.value for d in c.diagnostics])

    def test_missing_guid_and_locator(self):
        for url in (None, 'http://fixture.test/nzb'):
            c = NewznabSource(config(), FixtureTransport(rss(item(guid=None, url=url)))).search(
                ReleaseSearchRequest('Batman')).candidates[0]
            self.assertEqual(c.candidate_id is None, url is None)
            self.assertEqual(c.acquisition.kind == LocatorKind.UNAVAILABLE, url is None)

    def test_protocol_errors(self):
        for code, expected in (('100', SearchError.AUTHENTICATION), ('102', SearchError.AUTHENTICATION),
                              ('500', SearchError.RATE_LIMITED), ('203', SearchError.UNSUPPORTED),
                              ('900', SearchError.PROTOCOL)):
            with self.assertRaises(SourceFailure) as e:
                parse_search(f'<error code="{code}" description="{SECRET}"/>'.encode())
            self.assertEqual(e.exception.code, expected)
            self.assertNotIn(SECRET, str(e.exception))

    def test_arbitrary_result_urls_never_requested(self):
        for url in ('http://other.test/nzb?apikey=' + SECRET, 'file:///private',
                    'http://user:password@fixture.test/nzb', 'magnet:stuff'):
            tr = FixtureTransport(rss(item(url=url)))
            c = NewznabSource(config(), tr).search(ReleaseSearchRequest('Batman')).candidates[0]
            self.assertEqual(c.acquisition.kind, LocatorKind.UNAVAILABLE)
            self.assertEqual(len(tr.calls), 1)

    def test_secrets_and_html_remain_data(self):
        title = '<script>alert(1)</script>'
        batch = configured_search(target(), (config(name='<img src=x>'),),
            transport=FixtureTransport(rss(item(title, guid='http://user:pass@host/?token=' + SECRET))))
        dto = json.dumps(preview_search(batch))
        self.assertIn(title, dto)
        for secret in (SECRET, 'resolver_key', 'user:pass', 'apikey=', 'token='):
            self.assertNotIn(secret, dto)
        self.assertNotIn(SECRET, repr(batch))

    def test_resolver_binding_and_expiry_no_fetch(self):
        tr = FixtureTransport()
        source = NewznabSource(config(), tr)
        c = source.search(ReleaseSearchRequest('Batman')).candidates[0]
        reference = source.selected_reference(c)
        self.assertIn(SECRET, reference.url)
        self.assertEqual(reference.guid, 'release-5')
        self.assertNotIn(SECRET, repr(reference))
        self.assertEqual(len(tr.calls), 1)
        for bad in (replace(c, raw_title='tampered'), replace(c, source=config(key='other').source())):
            with self.assertRaises(SourceFailure):
                source.selected_reference(bad)
        with self.assertRaises(SourceFailure):
            NewznabSource(config(key='other'), tr).selected_reference(c)
        source.close()
        with self.assertRaises(SourceFailure):
            source.selected_reference(c)


class SearchIntegrationTests(TestCase):
    def test_exact_wrong_range_pack_and_opaque(self):
        titles = ('Batman #5 (2016).cbz', 'Batman #6 (2016).cbz',
                  'Batman #1-10 (2016).cbz', 'Batman Complete Series (2016)')
        batch = configured_search(target(), (config(),), transport=FixtureTransport(
            rss(''.join(item(t, str(i)) for i, t in enumerate(titles)))))
        by_title = {e.candidate.raw_title: e for e in batch.evaluations}
        self.assertEqual(by_title[titles[0]].state, Compatibility.COMPATIBLE)
        self.assertEqual(by_title[titles[1]].state, Compatibility.REJECTED)
        self.assertLess(by_title[titles[2]].score, by_title[titles[0]].score)
        self.assertEqual(by_title[titles[3]].state, Compatibility.REVIEW)
        self.assertEqual(len(preview_search(batch)['results']), 4)
        opaque = configured_search(target('1A'), (config(),), transport=FixtureTransport(rss(item('Batman #1A (2016).cbz'))))
        self.assertEqual(opaque.evaluations[0].state, Compatibility.COMPATIBLE)

    def test_zero_results_complete(self):
        batch = configured_search(target(), (config(),), transport=FixtureTransport(rss()))
        self.assertEqual(batch.state, SearchState.COMPLETE)
        self.assertFalse(batch.evaluations)

    def test_pagination_dedup_multiple_queries(self):
        def callback(c, suffix, params):
            if params['t'] == 'caps':
                return CAPS
            offset = params['offset']
            return rss(item(guid=str(offset)), offset, 2)
        tr = FixtureTransport(callback=callback)
        batch = configured_search(target(), (config(),), limits=SearchLimits(page_size=1), transport=tr)
        self.assertEqual(len(batch.evaluations), 2)
        self.assertEqual(len(tr.calls), 7)  # One caps, two pages times three queries.
        self.assertEqual(batch.state, SearchState.COMPLETE)

    def test_repeated_page_and_ignored_offset(self):
        tr = FixtureTransport(rss(item(), 0, 10000))
        batch = configured_search(target(), (config(),), transport=tr)
        self.assertEqual(batch.state, SearchState.PARTIAL)
        self.assertEqual(len(batch.evaluations), 1)
        self.assertTrue(any(d.code == SearchError.PAGINATION for d in batch.sources[0].diagnostics))
        self.assertEqual(len(tr.calls), 7)

    def test_result_and_page_caps_partial(self):
        batch = configured_search(target(), (config(),), limits=SearchLimits(source_results=1), transport=FixtureTransport())
        self.assertEqual(batch.state, SearchState.PARTIAL)
        self.assertEqual(len(batch.evaluations), 1)
        batch = configured_search(target(), (config(),), limits=SearchLimits(pages=1), transport=FixtureTransport(rss(item(), total=1000)))
        self.assertEqual(batch.state, SearchState.PARTIAL)

    def test_partial_sources_and_error_isolation(self):
        def callback(c, suffix, params):
            if c.key == 'auth':
                raise SourceFailure(SearchError.AUTHENTICATION)
            if c.key == 'timeout':
                raise SourceFailure(SearchError.TIMEOUT)
            return CAPS if params['t'] == 'caps' else rss(item())
        batch = configured_search(target(), tuple(config(key=k) for k in ('auth', 'success', 'timeout')),
                                  transport=FixtureTransport(callback=callback))
        self.assertEqual(batch.state, SearchState.PARTIAL)
        self.assertEqual(len(batch.evaluations), 1)
        self.assertEqual([r.state for r in batch.sources], [SearchState.FAILED, SearchState.COMPLETE, SearchState.FAILED])

    def test_source_priority_not_score_and_shuffle_stability(self):
        configs = (config(key='first', priority=-10), config(key='second', priority=10))
        a = configured_search(target(), configs, transport=FixtureTransport())
        b = configured_search(target(), configs[::-1], transport=FixtureTransport())
        self.assertEqual(a.evaluations, b.evaluations)
        self.assertEqual(a.evaluations[0].score, a.evaluations[1].score)
        self.assertEqual(a.evaluations[0].source_priority, 10)
        self.assertNotEqual(a.evaluations[0].candidate.candidate_id, a.evaluations[1].candidate.candidate_id)

    def test_disabled_no_traffic_and_unsupported_categories(self):
        tr = FixtureTransport()
        batch = configured_search(target(), (config(enabled=False),), transport=tr)
        self.assertEqual(len(tr.calls), 0)
        self.assertEqual(batch.sources[0].state, SearchState.DISABLED)
        batch = configured_search(target(), (config(categories=(9999,)),), transport=tr)
        self.assertEqual(batch.state, SearchState.FAILED)
        self.assertEqual(len(tr.calls), 1)

    def test_fake_source_abstraction(self):
        c = NewznabSource(config(), FixtureTransport()).search(ReleaseSearchRequest('Batman')).candidates[0]

        class FakeSource:
            source, priority, categories = c.source, 0, ()
            def capabilities(self):
                return SearchCapabilities(categories=(7030,))
            def search(self, request):
                return SearchPage((c,), 1, 0, 1)
            def close(self):
                pass

        with patch('socket.socket', side_effect=AssertionError('network')):
            batch = evaluate_search(target(), (FakeSource(),))
        self.assertEqual(batch.evaluations[0].state, Compatibility.COMPATIBLE)

    def test_systemic_errors_not_hidden(self):
        with self.assertRaises(RuntimeError):
            configured_search(target(), (config(),), transport=FixtureTransport(callback=lambda *a: (_ for _ in ()).throw(RuntimeError())))


class RealHTTPTests(TestCase):
    def test_body_limit_without_content_length(self):
        with fake_http(lambda p, q: (200, b'x' * (512 * 1024 + 1), {'Connection': 'close'})) as (base, calls):
            batch = configured_search(target(), (config(url=base),))
        self.assertEqual(batch.sources[0].diagnostics[0].code, SearchError.LIMIT)
        self.assertEqual(len(calls), 1)

    def test_direct_http_end_to_end_and_query_encoding(self):
        def callback(path, query):
            if query['t'] == ['caps']:
                return 200, CAPS, {}
            return 200, rss(item(url=base + '/nzb?apikey=' + SECRET)), {}
        with fake_http(callback) as (base, calls):
            batch = configured_search(target(), (config(url=base + '/base/api'),))
            self.assertEqual(batch.evaluations[0].state, Compatibility.COMPATIBLE)
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[1][0], '/base/api')
            self.assertEqual(calls[1][1]['q'], ['Batman 5 2016'])
            self.assertEqual(calls[1][1]['apikey'], [SECRET])
            self.assertNotIn(SECRET, json.dumps(preview_search(batch)))

    def test_prowlarr_discovery_proxy_attribution_and_type_filter(self):
        rows = [dict(id=3, name='Usenet fixture', enable=True, supportsSearch=True, protocol='usenet',
                     priority=1, fields=[dict(name='apiKey', value=SECRET)]),
                dict(id=4, name='Torrent', enable=True, supportsSearch=True, protocol='torrent'),
                dict(id=5, name='Disabled', enable=False, supportsSearch=True, protocol='usenet')]
        def callback(path, query):
            if path.endswith('/api/v1/indexer'):
                return 200, json.dumps(rows).encode(), {}
            self.assertIn(path, ('/prowlarr/3/api', '/prowlarr/4/api'))
            if path == '/prowlarr/4/api':
                return 200, CAPS if query['t'] == ['caps'] else rss(), {}
            return 200, CAPS if query['t'] == ['caps'] else rss(item(url=base + '/prowlarr/3/download?apikey=' + SECRET)), {}
        with fake_http(callback) as (base, calls):
            batch = configured_search(target(), (config(mode='prowlarr', url=base + '/prowlarr', priority=7),))
            self.assertEqual(batch.state, SearchState.COMPLETE)
            c = batch.evaluations[0].candidate
            self.assertEqual(c.source.name, 'Usenet fixture')
            self.assertEqual(c.source.via, 'Fixture')
            self.assertEqual(batch.evaluations[0].source_priority, 7)
            self.assertEqual(len(calls), 9)
            self.assertTrue(all(call[2] == SECRET for call in calls))
            self.assertTrue(all('apikey' not in call[1] for call in calls))
            self.assertNotIn(SECRET, json.dumps(preview_search(batch)))

    def test_status_matrix_and_no_retries(self):
        for status, code in ((401, SearchError.AUTHENTICATION), (403, SearchError.AUTHENTICATION),
                             (404, SearchError.UNAVAILABLE), (429, SearchError.RATE_LIMITED),
                             (500, SearchError.UNAVAILABLE), (302, SearchError.UNAVAILABLE)):
            with self.subTest(status=status), fake_http(lambda p, q: (status, SECRET.encode(),
                    {'Retry-After': '120', 'Location': 'http://unrelated.test/?apikey=' + SECRET})) as (base, calls):
                batch = configured_search(target(), (config(url=base),))
                self.assertEqual(batch.state, SearchState.FAILED)
                self.assertEqual(batch.sources[0].diagnostics[0].code, code)
                self.assertEqual(len(calls), 1)
                self.assertNotIn(SECRET, json.dumps(preview_search(batch)))

    def test_timeout_and_request_budget(self):
        def callback(path, query):
            sleep(0.05)
            return 200, CAPS, {}
        limits = SearchLimits(requests=1)
        with fake_http(callback) as (base, calls):
            http = BoundedHTTP(SearchBudget(limits), read_timeout=0.01)
            batch = configured_search(target(), (config(url=base),), transport=http)
            self.assertEqual(batch.sources[0].diagnostics[0].code, SearchError.TIMEOUT)
            self.assertEqual(len(calls), 1)
        with fake_http(lambda p, q: (200, CAPS, {})) as (base, calls):
            batch = configured_search(target(), (config(url=base),), limits=limits)
            self.assertEqual(len(calls), 1)
            self.assertEqual(batch.sources[0].diagnostics[0].code, SearchError.LIMIT)

    def test_oversize_and_compression_rejected(self):
        for headers, expected in (({'Content-Length': str(MAX_SEARCH_BYTES + 1)}, SearchError.LIMIT),
                                  ({'Content-Encoding': 'gzip'}, SearchError.UNSUPPORTED)):
            with fake_http(lambda p, q: (200, b'', headers)) as (base, calls):
                batch = configured_search(target(), (config(url=base),))
                self.assertEqual(batch.sources[0].diagnostics[0].code, expected)

    def test_connection_refusal(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            # Bound but not listening: no race against selecting a free port.
            batch = configured_search(target(), (config(url=f'http://127.0.0.1:{port}'),))
        self.assertEqual(batch.sources[0].diagnostics[0].code, SearchError.UNAVAILABLE)

    def test_cancellation_before_network(self):
        with patch('socket.socket', side_effect=AssertionError('network')):
            batch = configured_search(target(), (config(),), cancelled=lambda: True)
        self.assertEqual(batch.sources[0].diagnostics[0].code, SearchError.CANCELLED)


class ConfigurationTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT)')
        p = patch('backend.internals.release_sources.get_db', side_effect=self.db.cursor)
        p.start()
        self.addCleanup(p.stop)

    def test_roundtrip_mask_update_delete_rollback(self):
        before = self.db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        c = save_source(dict(name='Fixture', url='http://fixture.test/api', api_key=SECRET))
        self.db.commit()
        self.assertEqual(load_sources(), (c,))
        self.assertNotIn(SECRET, json.dumps(c.preview()))
        changed = save_source(dict(name='Rename', url=c.url, api_key=''), c.key)
        self.assertEqual(changed.api_key, SECRET)
        self.assertEqual(changed.namespace, c.namespace)
        self.db.rollback()
        self.assertEqual(load_sources(), (c,))
        delete_source(c.key)
        self.db.commit()
        self.assertEqual(load_sources(), ())
        self.assertEqual(before, self.db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall())

    def test_invalid_configuration_safe(self):
        for values in ({'url': 'http://user:secret@host'}, {'url': 'https://host/?apikey=secret'},
                       {'api_key': ''}, {'url': None}, {'url': []}, {'url': True}, {'priority': 1001}, {'categories': ('7030',)},
                       {'key': 'http://bad'}, {'enabled': 1}):
            with self.assertRaises(SourceFailure) as error:
                config(**values)
            self.assertNotIn(SECRET, str(error.exception))

    def test_secret_independent_identity_url_sensitive(self):
        c = config()
        self.assertEqual(c.namespace, replace(c, api_key='different').namespace)
        self.assertNotEqual(c.namespace, replace(c, url='http://other.test/api').namespace)
        with self.assertRaises(FrozenInstanceError):
            c.priority = 123

    def test_caps_check_not_search(self):
        tr = FixtureTransport()
        checked = check_source(config(), tr)
        self.assertTrue(checked['usable'])
        self.assertEqual(len(tr.calls), 1)
        self.assertEqual(tr.calls[0][2]['t'], 'caps')

    def test_authenticated_api_crud_masks_secret_and_never_networks(self):
        from frontend.api import api
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        client = app.test_client()
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('socket.socket', side_effect=AssertionError('network')):
            settings.return_value.sv.api_key = 'test-application-key'
            self.assertEqual(client.get('/api/release-sources').status_code, 401)
            suffix = '?api_key=test-application-key'
            response = client.post('/api/release-sources' + suffix, json=dict(
                name='<img src=x>', url='http://fixture.test/api', api_key=SECRET))
            self.assertEqual(response.status_code, 200)
            value = response.json['result']
            self.assertNotIn(SECRET, response.get_data(as_text=True))
            key = value['id']
            response = client.get('/api/release-sources' + suffix)
            self.assertEqual(len(response.json['result']), 1)
            self.assertNotIn(SECRET, response.get_data(as_text=True))
            response = client.put('/api/release-sources/' + key + suffix, json=dict(
                name='Changed', url='http://fixture.test/api', api_key=''))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(load_sources()[0].api_key, SECRET)
            bad = client.post('/api/release-sources' + suffix, json=dict(
                name='Bad', url='http://host/?apikey=' + SECRET, api_key=SECRET))
            self.assertEqual(bad.status_code, 400)
            self.assertNotIn(SECRET, bad.get_data(as_text=True))
            self.assertEqual(client.delete('/api/release-sources/' + key + suffix).status_code, 200)
            self.assertEqual(load_sources(), ())

    def test_api_test_is_explicit_post_configured_only(self):
        from frontend.api import api
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        client = app.test_client()
        c = save_source(dict(name='Fixture', url='http://fixture.test/api', api_key=SECRET))
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.implementations.release_search.check_source', return_value={'usable': True}) as check:
            settings.return_value.sv.api_key = 'test-application-key'
            path = '/api/release-sources/' + c.key + '/test?api_key=test-application-key'
            self.assertEqual(client.get(path).status_code, 405)
            self.assertEqual(client.post(path, json={'url': 'http://untrusted.test'}).status_code, 400)
            self.assertEqual(client.post(path, json={}).status_code, 200)
            check.assert_called_once_with(c)

    def test_corrupt_private_settings_fail_not_empty_success(self):
        self.db.execute('INSERT INTO config VALUES (?,?)', ('release_source_v1:bad', 'not json'))
        with self.assertRaises(SourceFailure):
            load_sources()

    def test_settings_reopen_without_schema_change(self):
        with TemporaryDirectory(prefix='release-source-test-') as directory:
            filename = str(Path(directory) / 'settings.sqlite')
            db = sqlite3.connect(filename)
            try:
                db.execute('CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT)')
                with patch('backend.internals.release_sources.get_db', side_effect=db.cursor):
                    saved = save_source(dict(name='Fixture', url='http://fixture.test/api', api_key=SECRET))
                db.commit()
            finally:
                db.close()
            db = sqlite3.connect(filename)
            try:
                with patch('backend.internals.release_sources.get_db', side_effect=db.cursor):
                    self.assertEqual(load_sources(), (saved,))
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                db.close()


class AdditionalSafetyTests(TestCase):
    def test_ddl_newznab_neutral_fact_score_explanation_parity(self):
        title = 'Batman #5 (2016).cbz'
        ddl_candidate = adapt_ddl_result(ddl(title, size=12345))
        nzb_candidate = NewznabSource(config(), FixtureTransport(rss(item(title)))).search(
            ReleaseSearchRequest('Batman')).candidates[0]
        left, right = (evaluate_release(target(), c) for c in (ddl_candidate, nzb_candidate))
        self.assertEqual(left.state, right.state)
        self.assertEqual(left.band, right.band)
        self.assertEqual(left.score, right.score)
        self.assertEqual([(c.rule, c.points) for c in left.components], [(c.rule, c.points) for c in right.components])
        self.assertEqual([(e.key, e.message, e.points) for e in explain_release(left).entries],
                         [(e.key, e.message, e.points) for e in explain_release(right).entries])

    def test_repeated_guid_page_with_claimed_advancing_offset(self):
        def callback(c, suffix, params):
            return CAPS if params['t'] == 'caps' else rss(item(), offset=params['offset'], total=100)
        tr = FixtureTransport(callback=callback)
        batch = configured_search(target(), (config(),), transport=tr)
        self.assertEqual(len(tr.calls), 7)
        self.assertEqual(len(batch.evaluations), 1)
        self.assertEqual(batch.state, SearchState.PARTIAL)

    def test_blank_guid_unavailable_without_locator(self):
        c = NewznabSource(config(), FixtureTransport(rss(item(guid=' ', url=None)))).search(
            ReleaseSearchRequest('Batman')).candidates[0]
        self.assertIsNone(c.candidate_id)

    def test_no_database_files_downloader_or_provider(self):
        t, tr = target(), FixtureTransport()
        with ExitStack() as stack:
            for name in ('sqlite3.connect', 'builtins.open', 'os.stat', 'socket.socket',
                         'backend.features.download_queue.DownloadHandler.add',
                         'backend.internals.db.get_db',
                         'backend.implementations.release_scoring.build_wanted_target'):
                stack.enter_context(patch(name, side_effect=AssertionError('unexpected side effect')))
            batch = configured_search(t, (config(),), transport=tr)
            preview_search(batch)
        self.assertEqual(len(batch.evaluations), 1)

    def test_known_key_echo_in_title_isolated(self):
        batch = configured_search(target(), (config(),), transport=FixtureTransport(rss(
            item(title='secret echo ' + SECRET) + item())))
        self.assertEqual(len(batch.evaluations), 1)
        self.assertEqual(batch.state, SearchState.PARTIAL)
        self.assertNotIn(SECRET, json.dumps(preview_search(batch)))

    def test_torrent_enclosure_not_mapped_to_nzb(self):
        data = rss(item()).replace(b'application/x-nzb', b'application/x-bittorrent')
        rows, _, _, _, errors = parse_search(data)
        self.assertFalse(rows)
        self.assertEqual(errors, (0,))

    def test_secret_independent_search_fingerprint(self):
        a = configured_search(target(), (config(),), transport=FixtureTransport())
        b = configured_search(target(), (config(api_key='rotated'),), transport=FixtureTransport())
        self.assertEqual(a.configuration_fingerprint, b.configuration_fingerprint)
        c = configured_search(target(), (config(priority=1),), transport=FixtureTransport())
        self.assertNotEqual(a.configuration_fingerprint, c.configuration_fingerprint)

    def test_preferred_source_wrong_issue_still_rejected(self):
        def callback(c, suffix, params):
            return CAPS if params['t'] == 'caps' else rss(item('Batman #6 (2016).cbz' if c.priority else 'Batman #5 (2016).cbz'))
        batch = configured_search(target(), (config(key='good'), config(key='bad', priority=1000)),
                                  transport=FixtureTransport(callback=callback))
        self.assertEqual(batch.evaluations[0].state, Compatibility.COMPATIBLE)
        self.assertEqual(batch.evaluations[1].state, Compatibility.REJECTED)
        self.assertIsNone(batch.evaluations[1].score)

    def test_response_order_does_not_change_rank(self):
        rows = [item('Batman #' + str(i) + ' (2016).cbz', str(i)) for i in range(1, 11)]
        original = configured_search(target(), (config(),), transport=FixtureTransport(rss(''.join(rows))))
        random.Random(5).shuffle(rows)
        shuffled = configured_search(target(), (config(),), transport=FixtureTransport(rss(''.join(rows))))
        self.assertEqual(original.evaluations, shuffled.evaluations)

    def test_empty_page_with_claimed_remaining_is_partial(self):
        batch = configured_search(target(), (config(),), transport=FixtureTransport(rss('', total=10)))
        self.assertEqual(batch.state, SearchState.PARTIAL)

    def test_malformed_source_does_not_erase_peers(self):
        def callback(c, suffix, params):
            return CAPS if params['t'] == 'caps' else b'<rss' if c.key == 'bad' else rss(item())
        batch = configured_search(target(), (config(key='good'), config(key='bad')),
                                  transport=FixtureTransport(callback=callback))
        self.assertEqual(batch.state, SearchState.PARTIAL)
        self.assertEqual(len(batch.evaluations), 1)

    def test_prowlarr_duplicates_and_empty_discovery(self):
        for rows in ([], [dict(id=1), dict(id=1)]):
            tr = FixtureTransport(callback=lambda *a: json.dumps(rows).encode())
            batch = configured_search(target(), (config(mode='prowlarr'),), transport=tr)
            self.assertEqual(batch.state, SearchState.FAILED)

    def test_deep_discovery_json_is_invalid_source_input(self):
        tr = FixtureTransport(callback=lambda *a: b'[' * 10000 + b']' * 10000)
        batch = configured_search(target(), (config(mode='prowlarr'),), transport=tr)
        self.assertEqual(batch.state, SearchState.FAILED)
        self.assertEqual(batch.sources[0].diagnostics[0].code, SearchError.INVALID_RESPONSE)

    def test_global_results_bound(self):
        configs = tuple(config(key='source-' + str(i)) for i in range(10))
        batch = configured_search(target(), configs, limits=SearchLimits(total_results=2), transport=FixtureTransport())
        self.assertLessEqual(len(batch.evaluations), 2)
        self.assertEqual(batch.state, SearchState.PARTIAL)

    def test_explicit_capabilities_cache_one_operation(self):
        tr = FixtureTransport()
        source = NewznabSource(config(), tr)
        source.capabilities()
        source.capabilities()
        self.assertEqual(len(tr.calls), 1)
        other = NewznabSource(config(), tr)
        other.capabilities()
        self.assertEqual(len(tr.calls), 2)


class BulkSearchTests(TestCase):
    def test_several_sources_1000_ranked_explained(self):
        tr = FixtureTransport(rss(''.join(item(guid=str(i)) for i in range(100))))
        configs = tuple(config(key='source-' + str(i)) for i in range(10))
        started = perf_counter()
        batch = configured_search(target(), configs, transport=tr)
        dto = preview_search(batch)
        self.assertEqual(len(batch.evaluations), 1000)
        self.assertEqual(len(dto['results']), 1000)
        self.assertEqual(len(tr.calls), 40)
        print(f'10 sources / 1000 normalize+score+explain: {perf_counter() - started:.3f}s, 40 fixture requests')

    def test_100_1000_10000_bounded_parse_normalize(self):
        for count in (100, 1000, 10000):
            started = perf_counter()
            parsed, normalized = 0, 0
            # Production pages are bounded; 10,000 observations use ten fixture pages.
            for offset in range(0, count, 1000):
                size = min(1000, count - offset)
                data = rss(''.join(item(guid=str(i)) for i in range(offset, offset + size)))
                parsed += len(parse_search(data)[0])
                source = NewznabSource(config(), FixtureTransport(data))
                normalized += len(source.search(ReleaseSearchRequest('Batman')).candidates)
                source.close()
            self.assertEqual(parsed, count)
            self.assertEqual(normalized, count)
            print(f'Newznab fixture parse+normalize {count}: {perf_counter() - started:.3f}s')

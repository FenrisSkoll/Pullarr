"""Manual DDL migration: legacy characterization and offline acceptance."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from fixtures.library_import import ImportHarness

from backend.base.definitions import DownloadType, GCDownloadService
from backend.features.direct_downloads import ManualDDL, supported_mirror
from backend.implementations.direct_download_source import (DDLError,
                                                            DDLPageHTTP,
                                                            DDLSourceConfig,
                                                            GetComicsSource,
                                                            page_url)
from backend.implementations.download_preppers.ddl.GetComics import (
    GetComicsPrepper, _extract_list_links)
from backend.implementations.download_transport import PrivateResponse
from tests.Tbackend.features import release_scoring as fixtures
from tests.Tbackend.features.release_search import fake_http


def config(url='https://getcomics.example', id=1):
    return DDLSourceConfig(id, 'Fixture GetComics', url,
        tuple(s.value for s in GCDownloadService))


def search_html(titles):
    return '<html><body>' + ''.join(
        f'<article class="post"><a class="post-category">Comics</a>'
        f'<h1 class="post-title"><a href="/release/{i}">{title}</a></h1>'
        '<div><p>Size : 10 MB</p></div></article>' for i, title in enumerate(titles)) + '</body></html>'


def offerings_html(titles):
    return '<html><body><section class="post-contents"><ul>' + ''.join(
        f'<li>{title}<a href="https://mega.nz/file/{i}#private-key">Mega</a>'
        f'<a href="https://pixeldrain.com/u/{i}">Pixeldrain</a></li>' for i, title in enumerate(titles)) + '</ul></section></body></html>'


class FixtureHTTP:
    def __init__(self, titles=('Batman #5 (2016)',), offerings=('Batman #5 (2016)',)):
        self.search_body = search_html(titles)
        self.page_body = offerings_html(offerings)
        self.calls = []

    def fetch(self, base, url):
        self.calls.append(url)
        return self.search_body if '/page/' in url else self.page_body


class UnifiedDDL(TestCase):
    def setUp(self):
        self.http = FixtureHTTP()
        self.current_target = fixtures.target()
        self.configs = {1: config()}
        self.blocked = set()
        self.dispatch = Mock(return_value=[{'id': 7}])
        self.now = 10
        self.service = ManualDDL(clock=lambda: self.now,
            target_loader=lambda *_: self.current_target,
            sources_loader=lambda: self.configs,
            block_loader=lambda: self.blocked,
            source_factory=lambda c: GetComicsSource(c, self.http), dispatch=self.dispatch)
        self.start_patch = patch('backend.implementations.external_client_manager.ExternalClients.clients', {DownloadType.TORRENT: {}})
        self.start_patch.start()
        self.addCleanup(self.start_patch.stop)

    def search(self):
        self.batch = self.service.search(1, 5)
        self.sid = self.batch['search_id']
        self.rid = self.batch['results'][0]['selection_id']
        return self.batch['results'][0]

    def select(self, **kwargs):
        return self.service.select(self.sid, self.rid, **kwargs)

    def test_no_auto_grab_and_shared_receipt(self):
        result = self.search()
        self.assertEqual(result['explanation']['state'], 'compatible')
        self.assertGreater(result['explanation']['score'], 0)
        self.dispatch.assert_not_called()
        self.assertTrue(all('/page/' in u for u in self.http.calls))
        self.assertNotIn('https://', json.dumps(self.batch))

    def test_exact_selection_dispatch_and_replay(self):
        self.search()
        result = self.select()
        self.assertEqual(result['state'], 'dispatched')
        self.assertEqual(self.select(), result)
        self.dispatch.assert_called_once()
        self.assertEqual(sum('/release/' in u for u in self.http.calls), 1)

    def test_wrong_resolved_issue_stops(self):
        self.search()
        self.http.page_body = offerings_html(['Batman #6 (2016)'])
        with self.assertRaisesRegex(DDLError, 'no_compatible_offering'):
            self.select()
        self.dispatch.assert_not_called()

    def test_multiple_offerings_require_explicit_choice(self):
        self.search()
        self.http.page_body = offerings_html(['Batman #5 (2016)', 'Batman #1-10 (2016)'])
        response = self.select()
        self.assertEqual(response['state'], 'offering_selection_required')
        self.assertEqual(len(response['offerings']), 2)
        self.dispatch.assert_not_called()
        self.select(offering_id=response['offerings'][1]['offering_id'])
        self.dispatch.assert_called_once()

    def test_range_changed_from_exact_requires_confirmation(self):
        self.search()
        self.http.page_body = offerings_html(['Batman #1-10 (2016)'])
        self.assertEqual(self.select()['state'], 'offering_selection_required')
        self.dispatch.assert_not_called()

    def test_force_keeps_rejection_and_confirms_actual_offering(self):
        self.http.search_body = search_html(['Batman #6 (2016)'])
        self.http.page_body = offerings_html(['Batman #6 (2016)'])
        result = self.search()
        self.assertEqual(result['explanation']['state'], 'rejected')
        with self.assertRaisesRegex(DDLError, 'selection_not_eligible'):
            self.select()
        response = self.select(force=True)
        self.assertEqual(response['offerings'][0]['explanation']['state'], 'rejected')
        receipt = self.select(force=True, offering_id=response['offerings'][0]['offering_id'])
        self.assertTrue(receipt['forced'])
        self.assertEqual(receipt['original_state'], 'rejected')

    def test_blocklist_is_not_score(self):
        first = self.search()
        self.blocked.add('https://getcomics.example/release/0')
        second = self.search()
        self.assertEqual(first['explanation'], second['explanation'])
        self.assertTrue(second['blocked'])
        self.assertFalse(second['download_eligible'])
        with self.assertRaisesRegex(DDLError, 'selection_not_eligible'):
            self.select()
        self.assertEqual(self.select(force=True)['state'], 'offering_selection_required')

    def test_blocklist_changed_after_search(self):
        self.search()
        self.blocked.add('https://getcomics.example/release/0')
        with self.assertRaisesRegex(DDLError, 'selection_not_eligible'):
            self.select()
        self.dispatch.assert_not_called()

    def test_expiry_even_for_force(self):
        self.search()
        self.now = 911
        with self.assertRaisesRegex(DDLError, 'selection_expired'):
            self.select(force=True)

    def test_target_ownership_changes_require_research(self):
        self.search()
        issue = replace(self.current_target.catalog[0], owned=True)
        self.current_target = replace(self.current_target, catalog=(issue,))
        with self.assertRaisesRegex(DDLError, 'target_changed'):
            self.select(force=True)

    def test_source_edit_or_delete_invalidates(self):
        self.search()
        self.configs = {1: config('https://other.example')}
        with self.assertRaisesRegex(DDLError, 'source_changed'):
            self.select(force=True)
        self.configs = {}
        with self.assertRaisesRegex(DDLError, 'source_changed'):
            self.select(force=True)

    def test_raw_result_tamper(self):
        self.search()
        self.service.sessions[self.sid].selections[self.rid].raw['link'] += '/other'
        with self.assertRaisesRegex(DDLError, 'selection_mismatch'):
            self.select(force=True)

    def test_evaluation_tamper(self):
        self.search()
        selected = self.service.sessions[self.sid].selections[self.rid]
        selected.evaluation = replace(selected.evaluation, policy_fingerprint='wrong')
        with self.assertRaisesRegex(DDLError, 'selection_mismatch'):
            self.select()

    def test_cross_search_selection(self):
        self.search()
        old = self.rid
        self.search()
        with self.assertRaisesRegex(DDLError, 'selection_expired'):
            self.service.select(self.sid, old)

    def test_unknown_offering(self):
        self.search()
        with self.assertRaisesRegex(DDLError, 'offering_mismatch'):
            self.select(offering_id='other')

    def test_dispatch_failure_does_not_retry(self):
        self.search()
        self.dispatch.side_effect = RuntimeError('injected')
        with self.assertRaises(RuntimeError):
            self.select()
        with self.assertRaisesRegex(DDLError, 'dispatch_already_claimed'):
            self.select()
        self.dispatch.assert_called_once()

    def test_unsupported_mirrors_never_dispatch(self):
        self.search()
        self.http.page_body = '<section class="post-contents"><ul><li>Batman #5 (2016)<a href="file:///secret">Mega</a></li></ul></section>'
        with self.assertRaisesRegex(DDLError, 'no_supported_offering'):
            self.select(force=True)

    def test_literal_issue_matrix(self):
        for label in ('1', '01', '1.0', '1.5', '1A', '1.01', '[nn]', 'Annual', 'Special'):
            with self.subTest(label=label):
                self.current_target = fixtures.target(label)
                self.http.search_body = search_html([f'Batman #{label} (2016)'])
                result = self.search()
                self.assertIn(label, result['explanation']['candidate']['raw_title'])
                self.assertTrue(any(label in a['query'] for a in self.batch['attempts']))

    def test_session_bound(self):
        for _ in range(35):
            self.search()
        self.assertEqual(len(self.service.sessions), 32)

    def test_auth_api_and_payload_authority(self):
        from flask import Flask

        from frontend.api import api

        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        client = app.test_client()
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('frontend.api.Library') as library, \
                patch('frontend.api._unified_search', side_effect=lambda volume, issue: ({'result': self.service.search(volume, issue)}, 200)), \
                patch('backend.features.direct_downloads.MANUAL_DDL', self.service):
            settings.return_value.sv.api_key = 'fixture-application-key'
            library.get_issue.return_value.get_data.return_value.volume_id = 1
            path = '/api/issues/5/release-search'
            self.assertEqual(client.post(path).status_code, 401)
            suffix = '?api_key=fixture-application-key'
            self.assertEqual(client.get(path + suffix).status_code, 405)
            response = client.post(path + suffix)
            self.assertEqual(response.status_code, 200)
            result = response.json['result']
            action = f'/api/direct-download-search/{result["search_id"]}/{result["results"][0]["selection_id"]}' + suffix
            rejected = client.post(action, json={'action': 'download', 'url': 'http://private/?apikey=secret'})
            self.assertEqual(rejected.status_code, 400)
            self.assertNotIn('secret', rejected.get_data(as_text=True))
            self.assertEqual(client.get(action).status_code, 405)
            self.dispatch.assert_not_called()
            accepted = client.post(action, json={'action': 'download', 'force': False})
            self.assertEqual(accepted.status_code, 200)
            self.dispatch.assert_called_once()

    def test_partial_source_failure_preserves_success(self):
        self.configs[2] = config('https://offline.example', id=2)
        original = self.service.source_factory
        failed = Mock()
        failed.search.side_effect = DDLError('source_timeout')
        self.service.source_factory = lambda c: failed if c.id == 2 else original(c)
        result = self.search()
        self.assertEqual(result['explanation']['state'], 'compatible')
        self.assertEqual(self.batch['state'], 'partial')
        self.assertTrue(self.batch['errors'])

    def test_result_limit_and_repeated_pages_are_partial(self):
        self.http.search_body = search_html([f'Batman #5 (2016)' for _ in range(210)])
        self.search()
        self.assertEqual(len(self.batch['results']), 200)
        self.assertEqual(self.batch['state'], 'partial')
        self.http.search_body = search_html(['Batman #5 (2016)']) + '<a class="page-numbers">Next</a>'
        self.search()
        self.assertEqual(len(self.batch['results']), 1)
        self.assertTrue(any(e['code'] == 'repeated_page' for e in self.batch['errors']))

    def test_concurrent_click_claim(self):
        from concurrent.futures import ThreadPoolExecutor

        self.search()
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.select(), range(2)))
        self.assertEqual(values[0], values[1])
        self.dispatch.assert_called_once()

    def test_special_publication_queries_and_shared_evaluations(self):
        from backend.base.definitions import SpecialVersion
        from backend.implementations.release_scoring import evaluate_release
        for special, text in ((SpecialVersion.TPB, 'TPB'), (SpecialVersion.HARD_COVER, 'Hardcover'),
                              (SpecialVersion.OMNIBUS, 'Omnibus'), (SpecialVersion.ONE_SHOT, 'One-Shot'),
                              (SpecialVersion.VOLUME_AS_ISSUE, 'Vol 5')):
            self.current_target = fixtures.target(special=special)
            self.http.search_body = search_html([f'Batman {text} (2016)'])
            self.search()
            selected = self.service.sessions[self.sid].selections[self.rid]
            self.assertEqual(selected.evaluation, evaluate_release(self.current_target, selected.evaluation.candidate))
            self.assertTrue(any(text in a['query'] for a in self.batch['attempts']))

    def test_force_does_not_bypass_mirror_blocklist(self):
        self.search()
        self.blocked.update({'https://mega.nz/file/0#private-key', 'https://pixeldrain.com/u/0'})
        with self.assertRaisesRegex(DDLError, 'no_supported_offering'):
            self.select(force=True)
        self.dispatch.assert_not_called()

    def test_no_legacy_scorer_parser_or_automatic_dispatch(self):
        from contextlib import ExitStack

        with ExitStack() as stack:
            for path in ('backend.implementations.download_preppers.ddl.GetComics.download_group_filter',
                         'backend.implementations.download_preppers.ddl.GetComics.refine_special_version',
                         'backend.implementations.download_preppers.ddl.GetComics.extract_filename_data',
                         'backend.features.search_full.check_search_result_match',
                         'backend.features.search_full.choose_downloads',
                         'backend.features.download_queue.DownloadHandler.add',
                         'backend.features.sab_downloads.submit_selected'):
                stack.enter_context(patch(path, side_effect=AssertionError('Legacy selection or unrelated acquisition')))
            self.search()
            self.dispatch.assert_not_called()
            self.select()
            self.dispatch.assert_called_once()

    def test_page_structured_year_conflict_is_not_silently_refined(self):
        self.search()
        self.http.page_body = ('<section class="post-contents"><p>Batman #5 (2016)<br>'
            'Language : English | Year : 2017</p><div><div class="aio-button-center">'
            '<a href="https://mega.nz/file/x#key">Mega</a></div></div><hr></section>')
        with self.assertRaisesRegex(DDLError, 'no_compatible_offering'):
            self.select()
        self.dispatch.assert_not_called()

    def test_force_cannot_override_unsupported_archive(self):
        self.http.search_body = search_html(['Batman #5 (2016).exe'])
        result = self.search()
        self.assertFalse(result['force_eligible'])
        with self.assertRaisesRegex(DDLError, 'force_unavailable'):
            self.select(force=True)
        self.dispatch.assert_not_called()


class DDLSourceSecurity(TestCase):
    def test_page_binding(self):
        for url in ('https://other.example/x', 'file:///tmp/x', 'https://user:pw@getcomics.example/x',
                    'https://getcomics.example/x?apikey=secret', 'https://getcomics.example/x\n'):
            with self.subTest(url=url), self.assertRaises(DDLError):
                page_url('https://getcomics.example', url)

    def test_redirect_policy(self):
        transport = Mock()
        transport.request.return_value = PrivateResponse(302, b'', 'https://other.example/x')
        with self.assertRaisesRegex(DDLError, 'source_mismatch'):
            DDLPageHTTP(transport).fetch('https://getcomics.example', '/page')
        self.assertEqual(transport.request.call_count, 1)

    def test_mirror_hosts_and_mega_fragment(self):
        c = config()
        self.assertEqual(supported_mirror('mega', 'https://mega.nz/file/x#key', False, c, set()), GCDownloadService.MEGA)
        self.assertIsNone(supported_mirror('mega', 'https://attacker.example/file/x', False, c, set()))
        self.assertIsNone(supported_mirror('main server', 'http://127.0.0.1/private', False, c, set()))

    def test_real_http_search_and_selected_page(self):
        def response(path, query):
            body = search_html(['Batman #5 (2016)', 'Batman #6 (2016)', 'Batman #1-10 (2016)']) if '/page/' in path else offerings_html(['Batman #5 (2016)'])
            return 200, body.encode(), {}
        with fake_http(response) as (url, calls):
            dispatch = Mock(return_value=[])
            service = ManualDDL(target_loader=lambda *_: fixtures.target(),
                sources_loader=lambda: {1: config(url)}, block_loader=lambda: set(), dispatch=dispatch)
            with patch('backend.implementations.external_client_manager.ExternalClients.clients', {DownloadType.TORRENT: {}}):
                batch = service.search(1, 5)
                self.assertEqual([r['explanation']['state'] for r in batch['results']], ['compatible', 'compatible', 'rejected'])
                self.assertTrue(all('/page/' in p for p, _, _ in calls))
                self.assertLessEqual(len(calls), 6)
                service.select(batch['search_id'], batch['results'][0]['selection_id'])
            dispatch.assert_called_once()
            self.assertEqual(sum('/release/' in p for p, _, _ in calls), 1)

    def test_real_http_oversized_missing_and_redirect(self):
        from backend.implementations.direct_download_source import MAX_HTML
        cases = [(200, b'', {'Content-Length': str(MAX_HTML + 1)}),
                 (404, b'not found', {}),
                 (302, b'', {'Location': 'https://other.example/private'})]
        for reply in cases:
            with self.subTest(reply=reply[0]), fake_http(lambda *_: reply) as (url, calls):
                with self.assertRaises(DDLError):
                    DDLPageHTTP().fetch(url, '/selected')
                self.assertEqual(len(calls), 1)

    def test_safe_same_origin_redirect_and_limit(self):
        transport = Mock()
        transport.request.side_effect = [PrivateResponse(302, b'', '/next'), PrivateResponse(200, b'<html>ok</html>')]
        self.assertEqual(DDLPageHTTP(transport).fetch('https://example.invalid', '/first'), '<html>ok</html>')
        transport.request.side_effect = None
        transport.request.return_value = PrivateResponse(302, b'', '/loop')
        with self.assertRaisesRegex(DDLError, 'redirect_limit'):
            DDLPageHTTP(transport).fetch('https://example.invalid', '/first')

    def test_filename_containment(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from backend.implementations.download_clients.base import (
            BaseDirectDownload, safe_download_name)
        from backend.implementations.download_clients.DDL import DDLDownload
        with TemporaryDirectory() as folder:
            download = object.__new__(DDLDownload)
            download._download_folder = folder
            for name in ('../../escape', 'C:\\outside', '\\\\host\\share', '/root/escape', 'CON', 'NUL', '..', 'a:b', '\x00'):
                with self.subTest(name=name):
                    download._filename_body = name
                    output = Path(BaseDirectDownload._build_filename(download, None))
                    self.assertEqual(output.parent, Path(folder))
                    self.assertNotIn(':', output.name)
                    self.assertNotEqual(safe_download_name(name).upper(), 'CON')

    def test_ddl_nzb_semantic_parity(self):
        from backend.base.release_candidate import (AcquisitionMechanism,
                                                    AcquisitionReference,
                                                    LocatorKind, ReleaseSource,
                                                    SourceKind)
        from backend.implementations.release_candidates import adapt_ddl_result
        from backend.implementations.release_explanations import \
            explain_release
        from backend.implementations.release_scoring import evaluate_release
        for title in ('Batman #5 (2016)', 'Batman #6 (2016)', 'Batman #1-10 (2016)', 'Batman Complete (2016)'):
            ddl = adapt_ddl_result({'indexer_id': 1, 'indexer_title': 'GC', 'display_title': title,
                'link': 'https://example.invalid/release', 'size': -1})
            nzb = replace(ddl, source=ReleaseSource(SourceKind.NEWZNAB, 'fixture', 'NZB'),
                acquisition=AcquisitionReference(AcquisitionMechanism.NZB, LocatorKind.SOURCE_RECORD, '0' * 64))
            a, b = (evaluate_release(fixtures.target(), c) for c in (ddl, nzb))
            self.assertEqual((a.state, a.band, a.score), (b.state, b.band, b.score))
            self.assertEqual([(c.rule, c.points) for c in a.components], [(c.rule, c.points) for c in b.components])
            self.assertEqual(explain_release(a).headline, explain_release(b).headline)

    def test_bulk_diagnostics(self):
        from time import perf_counter

        from backend.implementations.release_candidates import adapt_ddl_result
        from backend.implementations.release_explanations import \
            explain_releases
        from backend.implementations.release_scoring import evaluate_releases
        for count in (100, 1000, 10000):
            started = perf_counter()
            with patch('socket.socket', side_effect=AssertionError('No sockets')):
                candidates = tuple(adapt_ddl_result({'indexer_id': 1, 'indexer_title': 'GC',
                    'display_title': 'Batman #5 (2016)', 'link': f'https://example.invalid/{i}', 'size': 100}) for i in range(count))
                evaluated = evaluate_releases(fixtures.target(), candidates)
                explained = explain_releases(evaluated)
            self.assertEqual(len(explained), count)
            print(f'DDL {count} normalize/evaluate/explain: {perf_counter() - started:.3f}s')

    def test_mirror_preferences_and_same_offering_fallback(self):
        from asyncio import run
        from unittest.mock import AsyncMock

        from backend.base.custom_exceptions import DownloadLinkBroken
        from backend.base.definitions import DownloadClientIdentifier
        from backend.implementations.download_preppers.ddl.GetComics import \
            GetComicsPrepper

        group = {'web_sub_title': 'Batman #5 (2016)', 'size': 500000000,
                 'info': {'issue_number': 5.0}, 'links': {
                     GCDownloadService.GETCOMICS: ['https://getcomics.example/file'],
                     GCDownloadService.MEGA: ['https://mega.nz/file/x#key'],
                     GCDownloadService.PIXELDRAIN: ['https://pixeldrain.com/u/x']}}
        prepper = GetComicsPrepper('https://getcomics.example/page', 1, 1, 5)
        preference = [GCDownloadService.GETCOMICS.value, GCDownloadService.MEGA.value,
                      GCDownloadService.PIXELDRAIN.value, GCDownloadService.MEDIAFIRE.value,
                      GCDownloadService.WETRANSFER.value, GCDownloadService.GETCOMICS_TORRENT.value]
        purifier = AsyncMock(side_effect=[DownloadLinkBroken('fixture'), ('https://pixeldrain.com/u/x', DownloadClientIdentifier.PIXELDRAIN)])
        fake_download = Mock()
        with patch('backend.implementations.download_preppers.ddl.GetComics.iter_commit', side_effect=iter), \
                patch('backend.implementations.download_preppers.ddl.GetComics.add_to_blocklist'), \
                patch('backend.implementations.download_preppers.ddl.GetComics.DownloadClients.get_client') as clients:
            clients.return_value.return_value = fake_download
            result = run(prepper.prepare_exact_offering(group, 'Batman article', preference, True,
                {'version': 'ddl-selection/v1'}, purifier))
            self.assertEqual(result, [fake_download])
            self.assertEqual([c.args[0] for c in purifier.call_args_list], [GCDownloadService.MEGA, GCDownloadService.PIXELDRAIN])
            self.assertIsNone(clients.return_value.call_args.kwargs['covered_issues'])
        self.assertEqual(group['info']['issue_number'], 5.0)  # No mutation of source evidence.

    def test_mirror_redirect_cannot_fetch_arbitrary_host(self):
        from backend.base.custom_exceptions import DownloadLinkBroken
        from backend.implementations.direct_download_source import \
            resolve_mirror
        with fake_http(lambda *_: (302, b'', {'Location': 'http://untrusted.invalid/file'})) as (url, calls):
            with self.assertRaises(DownloadLinkBroken):
                resolve_mirror(GCDownloadService.GETCOMICS, url + '/file', lambda link: link.startswith(url + '/'))
            self.assertEqual(len(calls), 1)

    def test_existing_torrent_mirror_requires_client(self):
        link = 'magnet:?xt=urn:btih:' + 'a' * 40
        self.assertIsNone(supported_mirror('torrent', link, False, config(), set()))
        self.assertEqual(supported_mirror('torrent', link, True, config(), set()), GCDownloadService.GETCOMICS_TORRENT)

    def test_cloudflare_solver_is_explicit_and_bounded(self):
        from backend.base.download_job import (DownloadErrorCode,
                                               DownloadFailure)
        transport = Mock()
        transport.request.side_effect = DownloadFailure(DownloadErrorCode.AUTHENTICATION)
        solver = Mock(return_value='<html>cleared</html>')
        self.assertEqual(DDLPageHTTP(transport, solver).fetch('https://getcomics.example', '/page'), '<html>cleared</html>')
        solver.assert_called_once_with('https://getcomics.example', 'https://getcomics.example/page')

    def test_source_html_and_alias_query_determinism(self):
        from backend.implementations.release_search import plan_queries
        target = fixtures.target()
        target = replace(target, publication=replace(target.publication, aliases=('Batman', 'The Batman', 'The Batman')))
        queries = plan_queries(target)
        self.assertLessEqual(len(queries), 3)
        self.assertIn('The Batman', queries[-1].query)
        source = GetComicsSource(config(), FixtureHTTP(['Batman #5 (2016)']))
        first = source.search('Batman', 1)[0]
        source.http.search_body = source.http.search_body.replace('class="post"', "class='post'")
        self.assertEqual(first, source.search('Batman', 1)[0])


class LegacyDDLContract(TestCase):
    def test_visible_match_characterization(self):
        legacy = fixtures.LegacyScoringCharacterization().legacy
        for title, expected in (
            ('Batman #5 (2016)', True), ('Batman #6 (2016)', False),
            ('Batman #1-10 (2016)', False), ('Batman #5 (2017)', True),
            ('Batman #5 (2011)', False),
        ):
            with self.subTest(title=title):
                self.assertEqual(legacy(title)[0]['match'], expected)

    def test_scraping_preserves_subtitle_size_and_mirrors(self):
        soup = BeautifulSoup('<section><ul><li>Batman #5 (2016) 10 MB '
            '<a href="https://mega.nz/file/example">Mega</a>'
            '<a href="https://pixeldrain.com/u/example">Pixeldrain</a>'
            '</li></ul></section>', 'html.parser')
        with patch('backend.implementations.download_preppers.ddl.GetComics.blocklist_contains', return_value=False):
            groups = _extract_list_links(soup, False)
        self.assertEqual(len(groups), 1)
        self.assertIn('Batman #5', groups[0]['web_sub_title'])
        self.assertGreater(groups[0]['size'], 0)
        self.assertEqual(list(groups[0]['links']), [GCDownloadService.MEGA, GCDownloadService.PIXELDRAIN])

    def test_legacy_force_bypasses_group_filter_but_not_link_blocklist(self):
        prepper = GetComicsPrepper('https://example.invalid/page', 1, 1, force_match=True)
        self.assertTrue(prepper.force_match)
        soup = BeautifulSoup('<ul><li>Batman #6 <a href="https://mega.nz/file/x">Mega</a></li></ul>', 'html.parser')
        with patch('backend.implementations.download_preppers.ddl.GetComics.blocklist_contains', return_value=True):
            self.assertEqual(_extract_list_links(soup, False), [])


class DDLWorkerAcceptance(TestCase):
    def test_local_http_dispatch_restart_and_unified_completion(self):
        self.unified_flow()

    def test_selected_five_actual_six_stays_in_staging(self):
        self.unified_flow(wrong=True)

    def unified_flow(self, wrong=False, automation=False, page_review=None, canonical=False):
        from contextlib import ExitStack
        from dataclasses import fields
        from io import BytesIO
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from zipfile import ZipFile

        from flask import Flask, g
        from Tbackend.features.organization_plan import NAMING

        from backend.base.definitions import SpecialVersion
        from backend.features.acquisition_intake import IntakeCoordinator
        from backend.features.download_queue import DownloadHandler
        from backend.implementations.download_client_manager import \
            DownloadClients
        from backend.internals.db import DB_SCHEMA, DBConnection

        DownloadClients.trigger_client_registration()

        comic = BytesIO()
        with ZipFile(comic, 'w') as archive:
            archive.writestr('page.jpg', b'disposable image')
            archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>Batman</Series><Number>{6 if wrong else 5}</Number><Year>2016</Year><Volume>2</Volume></ComicInfo>')
        payload = comic.getvalue()
        state = {'base': ''}
        def reply(path, query):
            if '/page/' in path:
                return 200, search_html(['Batman #5 (2016)']).encode(), {}
            if '/release/' in path:
                titles = ('Batman #6 (2016)',) if page_review == 'changed' else ('Batman #5 (2016)', 'Batman #1-10 (2016)') if page_review else ('Batman #5 (2016)',)
                return 200, ('<section class="post-contents"><ul>' + ''.join(
                    f'<li>{title}<a href="{state["base"]}/file/comic.cbz">Main Server</a></li>' for title in titles)
                    + '</ul></section>').encode(), {}
            return 200, payload, {'Content-Disposition': f'attachment; filename="Batman 00{6 if wrong else 5} (2016).cbz"'}

        with TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            incoming, library = root / 'downloads', root / 'library'
            incoming.mkdir()
            library.mkdir()
            app = Flask(__name__)
            stack.enter_context(app.app_context())
            db = DBConnection(db_file=str(root / 'queue.db'))
            stack.callback(db.close)
            db.executescript(DB_SCHEMA)
            db.execute("INSERT INTO config VALUES('database_version',57)")
            db.executemany('INSERT INTO config VALUES(?,?)', ((f.name, getattr(NAMING, f.name)) for f in fields(NAMING)))
            db.execute('INSERT INTO root_folders VALUES(1,?)', (str(library),))
            db.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,publisher,root_folder,folder,special_version) VALUES(1,101,'Batman',2016,2,'DC',1,?,?)", (str(library / 'Batman'), SpecialVersion.NORMAL.value))
            db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(5,1,205,'5',5)")
            db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(6,1,206,'6',6)")
            if canonical:
                from backend.internals.issue_facts import (mapped_facts,
                                                           write_facts)
                for iid, label, day in db.execute('SELECT id,issue_number,date FROM issues').fetchall():
                    write_facts(db.cursor(), iid, mapped_facts(label, day, 'legacy_mapped'))
            if automation:
                db.execute('UPDATE volumes SET monitored=1')
                db.execute('UPDATE issues SET monitored=(id=5)')
            db.commit()
            # No library mutation under test: just satisfy the legacy queue FK.
            db.execute('PRAGMA foreign_keys=OFF')
            stack.enter_context(patch('backend.features.download_queue.get_db', side_effect=lambda: db.cursor()))
            stack.enter_context(patch('backend.features.post_processing.get_db', side_effect=lambda: db.cursor()))
            stack.enter_context(patch('backend.internals.db.get_db', side_effect=lambda: db.cursor()))
            stack.enter_context(patch('backend.features.post_processing.commit', side_effect=lambda: db.commit()))
            stack.enter_context(patch('backend.internals.db.iter_commit', side_effect=lambda items: iter(items)))
            stack.enter_context(patch('backend.features.download_queue.iter_commit', side_effect=lambda items: iter(items)))
            stack.enter_context(patch('backend.implementations.download_preppers.ddl.GetComics.iter_commit', side_effect=lambda items: iter(items)))
            settings = SimpleNamespace(download_folder=str(incoming), rename_downloaded_files=True,
                concurrent_direct_downloads=0, convert=False)
            stack.enter_context(patch('backend.internals.settings.Settings', return_value=SimpleNamespace(sv=settings)))
            stack.enter_context(patch('backend.implementations.download_clients.base.Settings', return_value=SimpleNamespace(sv=settings)))
            stack.enter_context(patch('backend.features.post_processing.Settings', return_value=SimpleNamespace(sv=settings)))
            volume = Mock()
            volume.vd.folder = str(library)
            stack.enter_context(patch('backend.implementations.download_clients.base.Volume', return_value=volume))
            stack.enter_context(patch('backend.features.post_processing.Volume', return_value=volume))
            stack.enter_context(patch('backend.features.download_queue.Server'))
            for module in ('backend.features.download_queue', 'backend.implementations.download_clients.base'):
                stack.enter_context(patch(module + '.WebSocket'))
            stack.enter_context(patch('backend.features.post_processing.set_detected_extension', side_effect=lambda name: name))
            scan = stack.enter_context(patch('backend.features.post_processing.scan_files'))
            stack.enter_context(patch('backend.features.post_processing.mass_process_files'))
            fs = stack.enter_context(patch('backend.implementations.flaresolverr.FlareSolverr')).return_value
            fs.get_ua_cookies.return_value = ('Kapowarr-Test', '')
            fs.handle_cf_block.return_value = None
            handler = object.__new__(DownloadHandler)
            handler.settings = SimpleNamespace(sv=settings)
            handler.queue = []
            stack.enter_context(patch('backend.features.download_queue.DownloadHandler', return_value=handler))
            stack.enter_context(patch('backend.implementations.external_client_manager.ExternalClients.clients', {DownloadType.TORRENT: {}}))
            with fake_http(reply) as (url, calls):
                state['base'] = url
                service = ManualDDL(target_loader=lambda *_: fixtures.target(),
                    sources_loader=lambda: {1: config(url)}, block_loader=lambda: set())
                batch = service.search(1, 5) if not automation else None
                self.assertFalse(list(incoming.iterdir()))
                if automation:
                    from backend.features.direct_downloads import load_target
                    from backend.features.wanted_automation import \
                        WantedAutomation
                    from backend.features.wanted_search import \
                        UnifiedReleaseSearch
                    from backend.internals.wanted_configuration import \
                        save_automation
                    stack.enter_context(patch('backend.features.direct_downloads.get_db', side_effect=lambda: db.cursor()))
                    stack.enter_context(patch('backend.internals.identification.get_db', side_effect=lambda: db.cursor()))
                    searches = UnifiedReleaseSearch(target_loader=load_target,
                        nzb_loader=lambda: (), ddl_loader=lambda: {1: config(url)},
                        ddl_factory=lambda **kwargs: ManualDDL(**kwargs, block_loader=lambda: set()))
                    stack.callback(searches.close_all)
                    wanted = WantedAutomation(str(root / 'queue.db'), searches=searches)
                    stack.callback(wanted.close)
                    save_automation(wanted.store.db, {'mode': 'grab'})
                    wanted.tick()
                    if page_review:
                        self.assertEqual(wanted.store.db.execute('SELECT state FROM wanted_decisions').fetchone()[0], 'review')
                        self.assertEqual(handler.queue, [])
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM acquisition_intakes').fetchone()[0], 0)
                        request_count = len(calls)
                        wanted.tick()
                        self.assertEqual(len(calls), request_count)
                        scan.assert_not_called()
                        return
                    self.assertEqual(wanted.store.db.execute('SELECT state FROM wanted_decisions').fetchone()[0], 'tracking')
                else:
                    service.select(batch['search_id'], batch['results'][0]['selection_id'])
                self.assertEqual(len(handler.queue), 1)
                volume.get_issue_from_number.assert_not_called()
                volume.get_data.assert_not_called()  # No legacy pre-download naming/refinement.
                queued = handler.queue[0]
                self.assertIsNone(queued.covered_issues)
                self.assertEqual(queued.issue_id, 5)
                self.assertIsNone(queued.as_dict()['download_link'])
                persisted = db.execute('SELECT covered_issues FROM download_queue').fetchone()[0]
                self.assertEqual(json.loads(persisted)['version'], 'ddl-selection/v1')
                db.commit()
                db.close()
                g.cursors.clear()
                db = DBConnection(db_file=str(root / 'queue.db'))
                db.execute('PRAGMA foreign_keys=OFF')
                stack.callback(db.close)
                handler.queue = []
                handler._DownloadHandler__load_downloads()
                self.assertEqual(len(handler.queue), 1)
                restored = handler.queue[0]
                self.assertEqual(restored.selected_release, queued.selected_release)
                self.assertEqual(restored.issue_id, 5)
                handler._DownloadHandler__run_download(restored)
                self.assertEqual(db.execute('SELECT count(*) FROM download_queue').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT success FROM download_history').fetchone()[0], 1)
                scan.assert_not_called()
                self.assertEqual(list(library.iterdir()), [])
                identifier = db.execute('SELECT id FROM acquisition_intakes').fetchone()[0]
                coordinator = IntakeCoordinator(str(root / 'queue.db'), clock=lambda: 100.)
                stack.callback(coordinator.close)
                coordinator.process(identifier)
                coordinator.clock = lambda: 111.
                result = coordinator.process(identifier)
                if automation:
                    wanted.tick()
                    self.assertEqual(wanted.store.db.execute('SELECT state FROM wanted_decisions').fetchone()[0], 'review' if wrong else 'satisfied')
                    self.assertEqual(wanted.store.due(), ())
                if wrong:
                    self.assertEqual(result['state'], 'review', result)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
                    self.assertEqual(next(incoming.rglob('*.cbz')).read_bytes(), payload)
                    scan.assert_not_called()
                    return
                self.assertEqual(result['state'], 'completed', result)
                self.assertEqual(next(library.rglob('*.cbz')).read_bytes(), payload)
                self.assertEqual(db.execute('SELECT issue_id FROM issues_files').fetchone()[0], 5)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
                scan.assert_not_called()


class DDLTargetSnapshot(ImportHarness, TestCase):
    def test_snapshot_count_raw_identity_and_no_provider_traffic(self):
        from backend.features.direct_downloads import load_target

        volume_id = self.add_volume()
        issue_id = self.db.execute('SELECT id FROM issues ORDER BY id').fetchone()[0]
        self.db.execute('UPDATE issues SET issue_number=? WHERE id=?', ('1A', issue_id))
        self.db.execute('UPDATE volumes SET alt_title=? WHERE id=?', ('Explicit Alias', volume_id))
        self.db.commit()
        queries = []
        self.db.set_trace_callback(queries.append)
        with patch('backend.features.direct_downloads.get_db', side_effect=self.db.cursor), \
                patch('backend.internals.identification.get_db', side_effect=self.db.cursor), \
                patch('socket.socket', side_effect=AssertionError('Provider/network forbidden')):
            wanted = load_target(volume_id, issue_id)
        self.db.set_trace_callback(None)
        self.assertEqual(sum(q.lstrip().upper().startswith('SELECT') for q in queries), 6)
        self.assertEqual(wanted.publication.aliases, ('Explicit Alias',))
        self.assertEqual(next(i.raw_number for i in wanted.catalog if i.id == issue_id), '1A')

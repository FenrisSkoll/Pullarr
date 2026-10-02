"""Sitemap evidence, cursor safety, no legacy matching and bounded hint intake."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from Tbackend.features import direct_downloads as ddl, release_scoring as facts
from Tbackend.internals import wanted as persistence

from backend.base.release_evaluation import WantedIssue
from backend.features.wanted_discovery import discover_wanted, discovery_key
from backend.implementations.direct_download_source import (DDLError,
                                                            GetComicsSource)


class DiscoveryTests(TestCase):
    setUp = persistence.WantedPersistenceTests.setUp
    migrate = persistence.WantedPersistenceTests.migrate
    store = persistence.WantedPersistenceTests.store

    @staticmethod
    def target_loader(volume, issue):
        return facts.target(ids=(issue,), catalog=tuple(WantedIssue(i, str(i), year=2016, owned=False) for i in range(1, 7)))

    def discovery(self, candidates, **kwargs):
        self.migrate()
        store = self.store()
        result = discover_wanted(store, sources_loader=lambda: {1: ddl.config()},
            source_factory=lambda config: SimpleNamespace(discover=lambda cutoff: candidates),
            target_loader=self.target_loader, **kwargs)
        return store, result

    def test_compatible_hint_enqueues_literal_local_id_and_repeated_window_does_not_repeat(self):
        store, result = self.discovery((facts.candidate(),))
        self.assertEqual(result, {'state': 'complete', 'queued': 1})
        self.assertEqual([tuple(r) for r in store.db.execute('SELECT issue_id,requested FROM wanted_schedule')], [(5, 2)])
        store.db.execute('DELETE FROM wanted_schedule')
        store.db.execute('UPDATE wanted_discovery SET next_search=0')
        result = discover_wanted(store, sources_loader=lambda: {1: ddl.config()},
            source_factory=lambda _: SimpleNamespace(discover=lambda cutoff: (facts.candidate(),)),
            target_loader=self.target_loader)
        self.assertEqual(result['queued'], 0)

    def test_wrong_year_is_not_a_discovery_match(self):
        store, result = self.discovery((facts.candidate('Batman #5 (2001)'),))
        self.assertEqual(result['queued'], 0)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM wanted_schedule').fetchone()[0], 0)

    def test_owned_and_reserved_excluded_before_matching(self):
        self.migrate()
        store = self.store()
        store.db.execute("INSERT INTO files VALUES(1,'/library/comic.cbz',1)")
        store.db.execute('INSERT INTO issues_files VALUES(1,5,0)')
        result = discover_wanted(store, sources_loader=lambda: {1: ddl.config()},
            source_factory=lambda _: SimpleNamespace(discover=lambda _: (facts.candidate(),)),
            target_loader=self.target_loader)
        self.assertEqual(result['queued'], 0)

    def test_failure_does_not_advance_cursor(self):
        self.migrate()
        store = self.store()
        store.db.execute("INSERT INTO wanted_discovery VALUES(?,123,0,NULL)", (discovery_key(ddl.config()),))
        source = Mock()
        source.discover.side_effect = DDLError('source_timeout')
        result = discover_wanted(store, sources_loader=lambda: {1: ddl.config()}, source_factory=lambda _: source)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(store.db.execute('SELECT cursor FROM wanted_discovery').fetchone()[0], 123)

    def test_actual_sitemap_adapter_preserves_raw_title_before_evaluation(self):
        http = Mock()
        title = 'Batman #5 (2016)'
        http.fetch.return_value = f'<section class="post-contents"><div><ul class="lcp_catlist"><li><a href="https://getcomics.example/release/5">{title}</a>September 28, 2026</li></ul></div></section>'
        config = ddl.config('https://getcomics.example')
        source = GetComicsSource(config, http=http)
        result = source.discover(0)
        self.assertEqual(result[0].raw_title, title)
        self.assertEqual(len(source.records), 1)
        self.assertEqual(http.fetch.call_count, 1)

    def test_malformed_sitemap_is_not_complete_empty(self):
        http = Mock()
        http.fetch.return_value = '<html>maintenance</html>'
        with self.assertRaises(DDLError):
            GetComicsSource(ddl.config(), http=http).discover(0)

    def test_automatic_tasks_no_longer_dispatch_legacy_results(self):
        from backend.features.tasks import (AutoSearchIssue, AutoSearchVolume,
                                            DownloadTask, RssSync)
        self.assertFalse(issubclass(AutoSearchIssue, DownloadTask))
        self.assertFalse(issubclass(AutoSearchVolume, DownloadTask))
        self.assertFalse(issubclass(RssSync, DownloadTask))
        with patch('backend.features.tasks.Volume'), patch('backend.features.tasks.WebSocket'), \
                patch('backend.features.tasks.request_wanted_search') as request:
            self.assertIsNone(AutoSearchIssue(1, 5).run())
            request.assert_called_once_with(1, 5)

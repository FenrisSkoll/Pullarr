"""Deterministic Discover parser/storage/polling acceptance; no Internet."""

import sqlite3
from unittest import TestCase
from unittest.mock import patch
from xml.sax.saxutils import escape

from backend.base.discovery import (FEED, ORIGIN, DiscoveryError, observation,
                                    parse_feed, parse_listing, source_url)
from backend.features.discovery import Discover
from backend.internals.db import DB_SCHEMA, SCHEMA_70, SCHEMA_71
from backend.internals.db_migration import _migrate_discovery
from backend.internals.discovery import DiscoveryStore
from backend.internals.discovery_schema import STATEMENTS


def rss(items):
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>Fixture</title>'+''.join(
        '<item><title>'+escape(title)+'</title><link>'+ORIGIN+'/other-comics/'+str(key)+'/</link>'
        '<guid isPermaLink="false">fixture:'+str(key)+'</guid><pubDate>Wed, 30 Sep 2026 21:18:52 +0000</pubDate>'
        '<category>Other Comics</category><description>&lt;p&gt;Year : 2026 | Size : 37 MB&lt;/p&gt;</description></item>'
        for key,title in items)+'</channel></rss>').encode()


def listing(items):
    return ('<html><body>'+''.join('<article class="post"><h1 class="post-title"><a href="'+ORIGIN+'/other-comics/'+str(key)+'/">'+escape(title)+'</a></h1><a class="post-category">Other Comics</a><p class="post-excerpt">Year : 2026 | Size : 12 MB</p><time datetime="2026-09-30">Today</time></article>' for key,title in items)+'</body></html>').encode()


class Transport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, etag=None, modified=None):
        self.calls.append((url,etag,modified))
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result,dict) else dict(unchanged=False,data=result,etag='fixture-v1',last_modified='Wed, 30 Sep 2026 21:18:52 GMT')


class DiscoverFixture(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA)
        self.addCleanup(self.db.close)
        self.now = 10000
        self.store = DiscoveryStore(self.db.cursor(), lambda:self.now)
        self.tasks = []
        self.http = Transport({FEED:rss([(1,'One #1 (2026)')])})
        self.owner = Discover(transport=self.http,enqueue=self.tasks.append,clock=lambda:self.now)
        p = patch('backend.features.discovery.get_db',side_effect=self.db.cursor)
        p.start(); self.addCleanup(p.stop)


class DiscoverTests(DiscoverFixture):
    def test_feed_source_facts_and_plaintext(self):
        values = parse_feed(rss([(1,'<script>alert(1)</script> #1 (2026)'),(1,'Repeated')]))
        self.assertEqual(len(values),2)
        self.assertEqual(values[0]['size_bytes'],37*1024**2)
        self.assertEqual(values[0]['published_precision'],'instant')
        self.assertEqual(values[0]['title'],'<script>alert(1)</script> #1 (2026)')
        value = observation('One',ORIGIN+'/one/',summary='<p>Year : 2026 | Size : 6.5 GB//1.5 GB</p><script>secret</script>',categories=['Other Comics'])
        self.assertIsNone(value['size_bytes'])
        self.assertNotIn('secret',value['summary'])
        for category in ('News','Sponsored'):
            self.assertEqual(observation('Announcement',ORIGIN+'/one/',categories=[category])['release_kind'],'non_release')

    def test_atom_html_dates_and_unknown_categories(self):
        raw=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>urn:fixture:1</id><title>One</title><link href="https://getcomics.org/one/"/><published>2026-09-30T12:30:00Z</published><category term="Other Comics"/></entry></feed>'
        self.assertEqual(parse_feed(raw)[0]['source_kind'],'atom')
        self.assertEqual(parse_listing(listing([(1,'One')]))[0]['published_precision'],'day')
        self.assertEqual(observation('Unknown',ORIGIN+'/one/')['release_kind'],'uncertain')
        self.assertEqual(parse_feed(rss([])),[])

    def test_xml_security_and_bounds(self):
        attacks = [b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel>&x;</channel></rss>',
            b'<!DOCTYPE rss SYSTEM "file:///private"><rss><channel/></rss>',
            b'<html>Challenge</html>',b'<rss><channel><broken>',
            b'<rss><channel>'+b'<x>'*25+b'</x>'*25+b'</channel></rss>',
            b'<rss><channel><x>'+b'a'*32769+b'</x></channel></rss>',
            b'<rss><channel><include xmlns="http://www.w3.org/2001/XInclude" href="file:///private"/></channel></rss>',
            rss([(i,'One') for i in range(501)]),b'x'*(2*1024*1024+1)]
        for value in attacks:
            with self.subTest(value=value[:60]), self.assertRaises(DiscoveryError):
                parse_feed(value)
        for raw in (b'<html>Just a moment...</html>',b'<article class="post"><h1>changed</h1></article>'):
            with self.assertRaisesRegex(DiscoveryError,'parser_contract_changed'):
                parse_listing(raw)

    def test_fixed_origin(self):
        for url in ('file:///tmp/x','http://getcomics.org/','https://localhost/', 'https://127.0.0.1/',
            'https://[::1]/','https://10.0.0.1/','https://169.254.169.254/','https://getcomics.org.evil.test/',
            'https://getcomics.org:444/','https://user:pass@getcomics.org/','https://getcomics.org/?url=x'):
            with self.subTest(url=url),self.assertRaises(DiscoveryError):
                source_url(url)
        self.assertEqual(source_url('https://www.getcomics.org/a/#comments'),ORIGIN+'/a/')

    def test_exact_dedupe_guid_url_changes_and_retained_ids(self):
        a = parse_listing(listing([(1,'One')]))[0]
        self.store.ingest([a]); initial = self.store.page()['items'][0]
        b = parse_feed(rss([(1,'One'),(2,'One')]))
        self.store.ingest(b)
        self.assertEqual(self.store.status()['retained'],2)
        self.assertEqual(self.store.post(initial['id'])['guid'],'fixture:1')
        b[0].update(title='Changed',url=ORIGIN+'/changed/')
        self.now += 1
        self.store.ingest([b[0]])
        changed = self.store.post(initial['id'])
        self.assertEqual(changed['title'],'Changed')
        self.assertEqual(changed['first_seen'],initial['first_seen'])
        revision = changed['revision']
        self.assertEqual(self.store.ingest([b[0],b[0]])['duplicates'],2)
        self.assertEqual(self.store.post(initial['id'])['revision'],revision)

    def test_poll_conditional_singleflight_defaults_restart(self):
        self.owner.tick();self.assertFalse(self.tasks)
        first=self.owner.submit();second=self.owner.submit()
        self.assertEqual(first['id'],second['id']);self.assertEqual(len(self.tasks),1)
        self.tasks.pop().run()
        self.assertEqual(self.owner.status(first['id'])['state'],'complete')
        self.assertEqual(self.store.status()['retained'],1)
        self.http.responses[FEED]=dict(unchanged=True)
        self.owner.poll()
        self.assertEqual(self.http.calls[-1][1],'fixture-v1')
        self.assertEqual(self.store.status()['receipt']['status'],'unchanged')
        replacement=Discover(transport=self.http,enqueue=self.tasks.append,clock=lambda:self.now)
        replacement.tick();self.assertFalse(self.tasks)
        self.store.settings(revision=1,enabled=True,automatic=True,interval_minutes=30)
        self.now += 4000
        replacement.tick();replacement.tick();self.assertEqual(len(self.tasks),1)
        self.tasks.pop().run()
        replacement.tick();self.assertFalse(self.tasks)

    def test_gap_and_bounded_fallback(self):
        self.owner.poll()
        self.http.responses[FEED]=rss([(2,'Two')])
        for page in range(1,5):
            self.http.responses[ORIGIN+'/' if page==1 else ORIGIN+f'/page/{page}/']=listing([(page+3,'New')])
        result=self.owner.poll()
        self.assertTrue(result['gap']);self.assertEqual(result['pages'],4)
        self.assertEqual(self.store.status()['retained'],6)
        self.http.responses[FEED]=DiscoveryError('invalid_feed')
        self.http.responses[ORIGIN+'/']=listing([(1,'One #1 (2026)')])
        self.assertFalse(self.owner.poll()['gap'])
        self.assertEqual(self.store.status()['transport'],'html')

    def test_failed_poll_preserves_data_and_rate_backoff(self):
        self.owner.poll()
        self.http.responses[FEED]=DiscoveryError('rate_limited',7000)
        with self.assertRaisesRegex(DiscoveryError,'rate_limited'):
            self.owner.poll()
        self.assertEqual(self.store.status()['retained'],1)
        self.assertGreaterEqual(self.store.status()['next_poll'],self.now+7000)
        with self.assertRaisesRegex(DiscoveryError,'rate_limited'):
            self.owner.submit()
        self.assertEqual(len(self.http.calls),2)

    def test_scheduler_capacity_does_not_block_other_runtime_work(self):
        self.store.settings(revision=1,enabled=True,automatic=True,interval_minutes=30)
        self.now+=4000
        with patch.object(self.owner,'submit',side_effect=DiscoveryError('capacity')):
            self.owner.tick()

    def test_local_browsing_no_remote_and_literal_filters(self):
        self.store.ingest(parse_feed(rss([(1,'100% _ Unicode Œ #1 (2026)')])) )
        with patch.object(self.http,'get',side_effect=AssertionError('Network forbidden')):
            self.assertEqual(len(self.owner.page(q='% _')['items']),1)
            self.assertEqual(self.owner.page(state='missing')['items'],[])
            self.assertEqual(self.owner.detail(1)['match'],'unmatched')
        for kwargs in (dict(limit=101),dict(offset=-1),dict(state='fake'),dict(quality='fake')):
            with self.assertRaises(DiscoveryError):
                self.owner.page(**kwargs)


class DiscoveryMigrationTests(TestCase):
    def test_failure_rollback_parity_preservation(self):
        for failure in range(len(STATEMENTS)+1):
            with self.subTest(failure=failure):
                db=sqlite3.connect(':memory:')
                try:
                    db.execute('PRAGMA foreign_keys=ON');db.executescript(SCHEMA_70)
                    db.execute("INSERT INTO config VALUES('database_version','70')")
                    db.commit();before=list(db.iterdump())
                    class Cursor:
                        def execute(self,sql,args=()):
                            if failure<len(STATEMENTS) and sql==STATEMENTS[failure]:
                                raise sqlite3.OperationalError('fixture statement failure')
                            return db.execute(sql,args)
                    with patch('backend.internals.db_migration.get_db',return_value=Cursor()):
                        if failure<len(STATEMENTS):
                            with self.assertRaises(sqlite3.OperationalError):_migrate_discovery()
                            self.assertEqual(before,list(db.iterdump()))
                        else:
                            _migrate_discovery();_migrate_discovery()
                            fresh=sqlite3.connect(':memory:')
                            try:
                                fresh.executescript(SCHEMA_71)
                                query="SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
                                self.assertEqual(db.execute(query).fetchall(),fresh.execute(query).fetchall())
                                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                            finally:fresh.close()
                finally:db.close()

"""Deterministic Torznab parsing and source-independent torrent identity."""
from dataclasses import replace
from unittest import TestCase

from backend.base.release_candidate import acquisition_identity
from backend.base.release_search import (ReleaseSearchRequest,
                                         SourceConfig, SourceFailure)
from backend.implementations.torznab import TorznabSource, parse_torznab


def feed(attributes='', title='Batman 001 (2020) (HD-Digital)', locator='magnet:?xt=urn:btih:' + 'a' * 40):
    return (f'<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item><title>{title}</title>'
            f'<guid>stable-fixture</guid><enclosure url="{locator}" length="999"/>{attributes}</item>'
            '<torznab:response offset="0" total="1"/></channel></rss>').encode()


def attr(name, value):
    return f'<torznab:attr name="{name}" value="{value}"/>'


class TorznabTests(TestCase):
    def test_retained_attributes_and_explicit_size(self):
        attributes = dict(size='123', category='7030', seeders='4', leechers='2', peers='6',
            minimumratio='1.5', minimumseedtime='60.5', seedtype='both',
            downloadvolumefactor='0', uploadvolumefactor='2', password='0', infohash='a' * 40)
        rows, count, offset, total, invalid = parse_torznab(feed(''.join(attr(k, v) for k, v in attributes.items())))
        self.assertEqual((count, offset, total, invalid), (1, 0, 1, ()))
        self.assertEqual(rows[0]['size'], 123)
        facts = dict(rows[0]['facts'])
        self.assertEqual(facts['minimumseedtime'], '61')
        self.assertEqual(facts['infohash_v1'], 'a' * 40)
        self.assertEqual(facts['downloadvolumefactor'], '0')

    def test_conflicting_attrs_and_hashes_rejected(self):
        for attrs in (attr('size','1') + attr('size','2'), attr('seeders','NaN'),
                      attr('infohash','b' * 40), attr('minimumratio','-1')):
            self.assertEqual(parse_torznab(feed(attrs))[-1], (0,))
        rows = parse_torznab(feed(attr('seeders','2') * 2))[0]
        self.assertEqual(dict(rows[0]['facts'])['seeders'], '2')

    def test_unknown_requirements_fail_conservative(self):
        rows = parse_torznab(feed(attr('seedtype', 'new-mode')))[0]
        self.assertEqual(dict(rows[0]['facts'])['seedtype'], 'unknown')

    def test_secure_xml(self):
        for payload in (b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss/>',
                        b'<rss xmlns:x="http://www.w3.org/2001/XInclude"><x:include/></rss>',
                        b'<a>' * 20 + b'</a>' * 20, b'<rss>',
                        feed(title='x' * 65537)):
            with self.subTest(length=len(payload)), self.assertRaises(SourceFailure):
                parse_torznab(payload)

    def test_prowlarr_and_direct_exact_hash_parity(self):
        class Transport:
            def get(self, *args):
                return feed()
        direct = TorznabSource(SourceConfig('direct','Direct','http://indexer/api','fixture-key',mode='torznab'), Transport())
        proxy = TorznabSource(SourceConfig('proxy','Proxy','http://prowlarr','fixture-key',mode='prowlarr'), Transport(), 3, 'Tracker')
        a = direct.search(ReleaseSearchRequest('Batman')).candidates[0]
        b = proxy.search(ReleaseSearchRequest('Batman')).candidates[0]
        self.assertEqual(a.candidate_id, b.candidate_id)
        self.assertEqual(acquisition_identity(a), acquisition_identity(b))
        self.assertNotEqual(acquisition_identity(a), acquisition_identity(replace(a, torrent_facts=(('infohash_v1', 'b' * 40),))))

"""Offline SSRF, XML and provider-contract acceptance. No private IP exception."""

import io
import socket
from unittest import TestCase
from unittest.mock import patch

from TReadingOrders import CBL

from backend.base.reading_orders import MAX_BYTES, ReadingOrderError, parse_cbl
from backend.implementations.reading_order_sources import (CBLFetcher,
                                                           MetronLists,
                                                           normalize_url,
                                                           public_addresses)


class Reply:
    def __init__(self, status=200, data=CBL, headers=None):
        self.status, self.data = status, io.BytesIO(data)
        self.headers = headers or {'Content-Type': 'application/xml'}

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        return self.data.read(size)


class Connection:
    sock = None

    def __init__(self, reply, seen, host, address, timeout):
        self.reply, self.seen = reply, seen
        seen.append(dict(host=host, address=address, timeout=timeout))

    def request(self, method, path, headers):
        self.seen[-1].update(method=method, path=path, headers=headers.copy())

    def getresponse(self):
        return self.reply

    def close(self):
        pass


class SourceTests(TestCase):
    def fetcher(self, replies, resolver=None):
        self.seen = []
        fetcher = CBLFetcher(resolver=resolver or (lambda host: ['93.184.216.34']),
            connection=lambda *args: Connection(replies.pop(0), self.seen, *args))
        self.addCleanup(fetcher.dns.shutdown, wait=False, cancel_futures=True)
        return fetcher

    def test_ssrf_matrix(self):
        for value in ('file:///C:/private.cbl', 'ftp://example.com/a', 'http://example.com/a', 'data:text/xml,x',
                      'https://user:secret@example.com/x', 'https://example.com/x?token=secret', 'C:\\x.cbl', 'https://example.com:8443/x'):
            with self.subTest(value=value), self.assertRaises(ReadingOrderError):
                normalize_url(value)
        for address in ('127.0.0.1', '::1', '10.1.2.3', '172.16.0.1', '192.168.1.1', '169.254.169.254',
                        'fe80::1', 'fc00::1', '224.0.0.1', '0.0.0.0', '::ffff:127.0.0.1'):
            with self.subTest(address=address), self.assertRaises(ReadingOrderError):
                public_addresses([address])
        for host in ('localhost', '2130706433', '0177.0.0.1', '0x7f000001'):
            fetcher = self.fetcher([], lambda value: ['127.0.0.1'])
            with self.subTest(host=host), self.assertRaisesRegex(ReadingOrderError, 'blocked_destination'):
                fetcher.fetch('https://' + host + '/list.cbl')
            self.assertFalse(self.seen)

    def test_pinned_public_destination_and_conditional_headers(self):
        f = self.fetcher([Reply(headers={'Content-Type': 'text/xml', 'ETag': 'fixture-tag', 'Last-Modified': 'fixture-date'}), Reply(304)])
        first = f.fetch('https://EXAMPLE.com:443/list.cbl#fragment')
        self.assertEqual(self.seen[0]['address'], '93.184.216.34')
        self.assertEqual(self.seen[0]['host'], 'example.com')
        self.assertEqual(first['model']['entries'], parse_cbl(CBL)['entries'])
        self.assertTrue(f.fetch('https://example.com/list.cbl', first['etag'], first['last_modified'])['unchanged'])
        self.assertEqual(self.seen[1]['headers']['If-None-Match'], 'fixture-tag')
        self.assertNotIn('Cookie', self.seen[1]['headers'])

    def test_redirect_admission_and_limit(self):
        f = self.fetcher([Reply(302, headers={'Location': 'https://internal.test/a'})],
            lambda host: ['127.0.0.1'] if host == 'internal.test' else ['93.184.216.34'])
        with self.assertRaisesRegex(ReadingOrderError, 'blocked_destination'):
            f.fetch('https://example.com/a')
        self.assertEqual(len(self.seen), 1)
        f = self.fetcher([Reply(302, headers={'Location': '/next'}) for _ in range(4)])
        with self.assertRaisesRegex(ReadingOrderError, 'redirect_limit'):
            f.fetch('https://example.com/a')

    def test_response_bounds_mime_encoding_timeout(self):
        cases = [(Reply(data=b'x'*(MAX_BYTES+1)), 'bounded'),
            (Reply(headers={'Content-Length': str(MAX_BYTES+1)}), 'bounded'),
            (Reply(headers={'Content-Encoding': 'gzip'}), 'unsupported_encoding'),
            (Reply(data=b'<html>login</html>'), 'unsupported_cbl'),
            (Reply(headers={'Content-Type': 'text/html'}), 'unsupported_cbl'),
            (Reply(data=b'bad xml'), 'invalid_xml'), (Reply(500), 'source_http_error')]
        for reply, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ReadingOrderError, reason):
                self.fetcher([reply]).fetch('https://example.com/a')
        with self.assertRaisesRegex(ReadingOrderError, 'source_timeout'):
            self.fetcher([], lambda h: (_ for _ in ()).throw(socket.timeout())).fetch('https://example.com/a')

    def test_utf16_entity_and_element_bounds(self):
        hostile = '<!DOCTYPE ReadingList [<!ENTITY a SYSTEM "file:///secret">]><ReadingList>&a;</ReadingList>'
        with self.assertRaisesRegex(ReadingOrderError, 'unsafe_xml'):
            parse_cbl(hostile.encode('utf-16'))
        with self.assertRaisesRegex(ReadingOrderError, 'bounded'):
            parse_cbl(b'<ReadingList><Books>' + b'<Book/>'*2001 + b'</Books></ReadingList>')
        refs=b''.join(f'<Database Name="cv" Issue="{n+1}"/>'.encode() for n in range(9))
        with self.assertRaisesRegex(ReadingOrderError, 'bounded'):
            parse_cbl(b'<ReadingList><Books><Book>'+refs+b'</Book></Books></ReadingList>')

    def test_raw_file_hosts_plain_text_still_require_cbl(self):
        result=self.fetcher([Reply(headers={'Content-Type':'text/plain; charset=utf-8'})]).fetch('https://example.com/list.cbl')
        self.assertEqual(len(result['model']['entries']),4)
        with self.assertRaisesRegex(ReadingOrderError,'unsupported_cbl'):
            self.fetcher([Reply(data=b'<html>login</html>',headers={'Content-Type':'text/plain'})]).fetch('https://example.com/list.cbl')

    def test_metron_ordered_members_pagination_contract(self):
        def member(order, identity):
            return dict(order=order, issue=dict(id=identity, number='Annual', series=dict(id=10, name='Fixture', volume=1)))
        with patch('backend.implementations.reading_order_sources.MetronClient') as client:
            client.return_value.get.return_value = dict(name='List')
            client.return_value.pages.return_value = [member(2, 5), member(1, 6), member(3, 5)]
            model = MetronLists().fetch('1')['model']
            self.assertEqual([e['refs'][0]['issue_id'] for e in model['entries']], ['6', '5', '5'])
            self.assertEqual(client.return_value.pages.call_args.kwargs, dict(max_pages=40, max_results=2000))
            client.return_value.pages.return_value = [member(1, 5), member(1, 6)]
            with self.assertRaisesRegex(ReadingOrderError, 'unsupported_source_order'):
                MetronLists().fetch('1')

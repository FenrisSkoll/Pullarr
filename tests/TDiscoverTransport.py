"""Pinned DNS/redirect/response bounds, deterministic public test transport."""

from io import BytesIO
from unittest import TestCase

from backend.base.discovery import FEED, MAX_BYTES, DiscoveryError
from backend.implementations.discovery_source import DiscoveryHTTP


class Response:
    def __init__(self,status=200,body=b'<rss><channel/></rss>',**headers):
        self.status,self.body,self.headers=status,BytesIO(body),{'Content-Type':'application/rss+xml',**headers}
    def getheader(self,key,default=None):return self.headers.get(key,default)
    def read1(self,count):return self.body.read(count)


class Connection:
    def __init__(self,response,calls,host,address):
        self.response,self.calls,self.host,self.address=response,calls,host,address
        self.sock=None
    def request(self,method,path,headers):self.calls.append((self.host,self.address,path,dict(headers)))
    def getresponse(self):
        if isinstance(self.response,Exception):raise self.response
        return self.response
    def close(self):pass


class DiscoverTransportTests(TestCase):
    def fetcher(self,responses,addresses=None):
        calls=[]
        iterator=iter(responses)
        http=DiscoveryHTTP(resolver=lambda host:addresses or ['93.184.216.34'],
            connection=lambda host,address,timeout:Connection(next(iterator),calls,host,address))
        self.addCleanup(http.dns.shutdown,wait=True)
        return http,calls

    def test_conditional_pinned_no_cookies_or_proxy_headers(self):
        http,calls=self.fetcher([Response(304)])
        self.assertTrue(http.get(FEED,'fixture-tag','fixture-date')['unchanged'])
        self.assertEqual(calls[0][1],'93.184.216.34')
        self.assertEqual(calls[0][3]['If-None-Match'],'fixture-tag')
        self.assertEqual(calls[0][3]['Accept-Encoding'],'identity')
        self.assertFalse(set(calls[0][3]) & {'Cookie','Authorization','Proxy-Authorization','Referer'})

    def test_dns_private_ipv6_mapped_obfuscated_destination_matrix(self):
        for address in ('127.0.0.1','10.0.0.1','172.16.0.1','192.168.0.1','169.254.169.254',
            '::1','fc00::1','fe80::1','::ffff:127.0.0.1','224.0.0.1','0.0.0.0','240.0.0.1'):
            http,calls=self.fetcher([],addresses=[address])
            with self.subTest(address=address),self.assertRaises(DiscoveryError):http.get(FEED)
            self.assertFalse(calls)

    def test_redirect_revalidation_limit_and_validator_stripping(self):
        for location in ('http://getcomics.org/feed/','https://127.0.0.1/','https://other.example/',
                'file:///private','https://getcomics.org/?token=private'):
            http,calls=self.fetcher([Response(302,**{'Location':location})])
            with self.assertRaises(DiscoveryError):http.get(FEED)
            self.assertEqual(len(calls),1)
        http,calls=self.fetcher([Response(302,**{'Location':'/loop/'}) for _ in range(4)])
        with self.assertRaisesRegex(DiscoveryError,'redirect_limit'):http.get(FEED)
        self.assertEqual(len(calls),4)
        http,calls=self.fetcher([Response(302,**{'Location':'/new/'}),Response()])
        self.assertFalse(http.get(FEED,'tag')['unchanged'])
        self.assertNotIn('If-None-Match',calls[1][3])

    def test_http_failure_timeout_compression_mime_and_byte_bounds(self):
        cases=[(Response(429,**{'Retry-After':'3600'}),'rate_limited'),(Response(503),'source_unavailable'),
            (Response(403),'source_access_blocked'),(TimeoutError(),'source_timeout'),
            (Response(**{'Content-Encoding':'gzip'}),'unsupported_encoding'),
            (Response(**{'Content-Type':'image/png'}),'invalid_content_type'),
            (Response(**{'Content-Length':str(MAX_BYTES+1)}),'oversized_response'),
            (Response(body=b'x'*(MAX_BYTES+1)),'oversized_response'),(Response(304),'source_http_error')]
        for response,code in cases:
            http,_=self.fetcher([response])
            with self.subTest(code=code),self.assertRaisesRegex(DiscoveryError,code):http.get(FEED)
